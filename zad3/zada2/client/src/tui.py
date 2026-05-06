import asyncio
from datetime import datetime

import grpc
from textual.app import App
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Header,
    Footer,
    RichLog,
    Input,
    Button,
    SelectionList,
    RadioSet,
    RadioButton,
    Static,
)
from textual.widgets.selection_list import Selection

import can_pb2

import config
from src.grpc_client import (
    CanClient,
    STATUS_CONNECTED,
    STATUS_RECONNECTING,
    STATUS_EXPIRED,
    KIND_RESPONSE,
)


class ClientApp(App):
    CSS = """
    Screen { layout: vertical; }
    #main { height: 1fr; }
    #left { width: 50%; height: 1fr; }
    #right { width: 50%; height: 1fr; }
    .panel { border: round white; padding: 1; height: 1fr; }
    .title { text-style: bold; }
    SelectionList { height: 12; }
    #gets_log { height: 1fr; }
    #stream_log { height: 1fr; }
    Button { margin-right: 1; }
    """

    BINDINGS = [("ctrl+q", "clean_exit", "clean exit")]

    def __init__(self, session):
        super().__init__()
        self.session = session
        self.outgoing = asyncio.Queue()
        self.incoming = asyncio.Queue()
        self.client = CanClient(session.id, self.incoming, self.outgoing)
        self.status = "starting"
        self._worker = None
        self._reader = None

    def compose(self):
        yield Header()
        yield Horizontal(
            Vertical(
                Static("Subscription", classes="title"),
                SelectionList(
                    *[Selection(c, c) for c in config.CATEGORIES],
                    id="cats",
                ),
                Input(placeholder="message names (comma sep, optional)", id="names"),
                Horizontal(
                    Button("Apply", id="apply"),
                    Button("Unsubscribe", id="unsub"),
                ),
                Static("Unary calls", classes="title"),
                RadioSet(
                    RadioButton("ListMessages", value=True, id="rb_list"),
                    RadioButton("GetStats", id="rb_stats"),
                    id="kind",
                ),
                Input(placeholder="message name (for GetStats)", id="stat_name"),
                Horizontal(
                    Button("Call", id="call"),
                    Button("Clear", id="gets_clear"),
                ),
                RichLog(id="gets_log"),
                id="left",
                classes="panel",
            ),
            Vertical(
                Static("Stream", classes="title"),
                Button("Clear", id="stream_clear"),
                RichLog(id="stream_log"),
                id="right",
                classes="panel",
            ),
            id="main",
        )
        yield Footer()

    def on_mount(self):
        self._refresh_title()
        self._worker = asyncio.create_task(self.client.run_subscribe_loop())
        self._reader = asyncio.create_task(self._consume_incoming())

    def _refresh_title(self):
        self.title = f"{self.session.name} [{self.session.id[:8]}] {self.status}"

    async def _consume_incoming(self):
        log = self.query_one("#stream_log", RichLog)
        while True:
            kind, payload = await self.incoming.get()
            now = datetime.now().strftime("%H:%M:%S")
            if kind == STATUS_CONNECTED:
                self.status = "connected"
                self._refresh_title()
                log.write(f"[{now}] CONNECTED")
            elif kind == STATUS_RECONNECTING:
                self.status = "reconnecting"
                self._refresh_title()
                log.write(f"[{now}] RECONNECTING ({payload})")
            elif kind == STATUS_EXPIRED:
                self.status = "expired"
                self._refresh_title()
                log.write(f"[{now}] SESSION EXPIRED")
            elif kind == KIND_RESPONSE:
                self._render_response(log, payload, now)

    def _render_response(self, log, resp, now):
        which = resp.WhichOneof("payload")
        if which == "update":
            u = resp.update
            sigs = ", ".join(f"{s.name}={s.value:.2f}{s.unit}" for s in u.signals)
            log.write(f"[{now}] {u.message_name}: {sigs}")
        elif which == "snapshot":
            entries = resp.snapshot.entries
            log.write(f"[{now}] SNAPSHOT ({len(entries)} entries)")
            for u in entries:
                sigs = ", ".join(f"{s.name}={s.value:.2f}{s.unit}" for s in u.signals)
                log.write(f"  {u.message_name}: {sigs}")
        elif which == "session_info":
            si = resp.session_info
            log.write(f"[{now}] RESUMED dropped={si.dropped_count}")

    async def on_button_pressed(self, event):
        bid = event.button.id
        if bid == "apply":
            cats = self.query_one("#cats", SelectionList).selected
            names_raw = self.query_one("#names", Input).value
            names = [n.strip() for n in names_raw.split(",") if n.strip()]
            cat_enums = [getattr(can_pb2, c) for c in cats]
            req = can_pb2.SubscribeRequest(
                categories=cat_enums,
                message_names=names,
                unsubscribe=False,
            )
            await self.outgoing.put(req)
        elif bid == "unsub":
            req = can_pb2.SubscribeRequest(unsubscribe=True)
            await self.outgoing.put(req)
        elif bid == "call":
            await self._do_unary()
        elif bid == "gets_clear":
            self.query_one("#gets_log", RichLog).clear()
        elif bid == "stream_clear":
            self.query_one("#stream_log", RichLog).clear()

    async def _do_unary(self):
        log = self.query_one("#gets_log", RichLog)
        rb_list = self.query_one("#rb_list", RadioButton)
        try:
            if rb_list.value:
                resp = await self.client.list_messages()
                for m in resp.messages:
                    log.write(f"{m.name} ({can_pb2.MessageCategory.Name(m.category)})")
            else:
                name = self.query_one("#stat_name", Input).value.strip()
                if not name:
                    log.write("ERROR: stat_name is empty")
                    return
                resp = await self.client.get_stats(name)
                log.write(f"{name}: last_seen_ms={resp.last_seen_ms}, total={resp.total_count}")
        except grpc.aio.AioRpcError as e:
            log.write(f"ERROR: {e.code().name} {e.details()}")
        except Exception as e:
            log.write(f"ERROR: {e}")

    async def action_clean_exit(self):
        try:
            await self.outgoing.put(can_pb2.SubscribeRequest(unsubscribe=True))
            await self.outgoing.put(None)
            if self._worker is not None:
                await asyncio.wait_for(self._worker, timeout=2)
        except Exception:
            pass
        if self._reader is not None:
            self._reader.cancel()
        self.session.clear()
        self.exit()
