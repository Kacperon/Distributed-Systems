import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "generated"))
sys.path.insert(0, HERE)

from src.tui import ClientApp


class FakeSession:
    def __init__(self):
        self.name = "smoke"
        self.id = "11111111-2222-3333-4444-555555555555"

    def clear(self):
        pass


class NoWorkerClientApp(ClientApp):
    def on_mount(self):
        self._refresh_title()


async def main():
    from textual.widgets import RichLog, Button, Input, SelectionList, RadioButton
    app = NoWorkerClientApp(FakeSession())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#stream_log", RichLog)
        app.query_one("#cats", SelectionList)
        app.query_one("#names", Input)
        app.query_one("#rb_list", RadioButton)
        app.query_one("#rb_stats", RadioButton)
        app.query_one("#gets_log", RichLog)
        app.query_one("#apply", Button)
        app.query_one("#unsub", Button)
        app.query_one("#call", Button)
        print("compose OK: stream_log, cats, names, rb_list, rb_stats, gets_log, apply, unsub, call all present")

        unsub_btn = app.query_one("#unsub", Button)
        ev_unsub = Button.Pressed(unsub_btn)
        await app.on_button_pressed(ev_unsub)
        req = app.outgoing.get_nowait()
        assert req.unsubscribe, "expected unsubscribe=True"
        print(f"  unsub handler -> outgoing.put(unsubscribe=True) OK")

        apply_btn = app.query_one("#apply", Button)
        ev_apply = Button.Pressed(apply_btn)
        await app.on_button_pressed(ev_apply)
        req = app.outgoing.get_nowait()
        assert not req.unsubscribe
        print(f"  apply handler -> outgoing.put(SubscribeRequest cats={list(req.categories)} names={list(req.message_names)}) OK")

        print("TUI smoke OK")


if __name__ == "__main__":
    asyncio.run(main())
