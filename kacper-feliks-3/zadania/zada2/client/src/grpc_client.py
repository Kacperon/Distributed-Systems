import asyncio
import grpc

import can_pb2
import can_pb2_grpc

import config

STATUS_CONNECTED = "connected"
STATUS_RECONNECTING = "reconnecting"
STATUS_EXPIRED = "expired"
KIND_RESPONSE = "response"


class CanClient:
    def __init__(self, session_id, incoming, outgoing):
        self.session_id = session_id
        self.incoming = incoming
        self.outgoing = outgoing
        self.current_filter = None
        self._first_fail_at = None
        self._backoff_idx = 0
        self.target = f"{config.HOST}:{config.PORT}"
        self._channel_options = [
            ("grpc.keepalive_time_ms", config.KEEPALIVE_MS),
            ("grpc.keepalive_timeout_ms", config.KEEPALIVE_TIMEOUT_MS),
            ("grpc.keepalive_permit_without_calls", 1),
            ("grpc.http2.max_pings_without_data", 0),
            ("grpc.max_connection_idle_ms", 300000),
            ("grpc.max_connection_age_ms", 600000),
        ]

    async def list_messages(self):
        async with grpc.aio.insecure_channel(self.target, options=self._channel_options) as ch:
            stub = can_pb2_grpc.CanServiceStub(ch)
            return await stub.ListMessages(can_pb2.ListMessagesRequest(), timeout=5)

    async def get_stats(self, name):
        async with grpc.aio.insecure_channel(self.target, options=self._channel_options) as ch:
            stub = can_pb2_grpc.CanServiceStub(ch)
            return await stub.GetStats(can_pb2.StatsRequest(message_name=name), timeout=5)

    async def run_subscribe_loop(self):
        while True:
            try:
                await self._one_session()
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                now = asyncio.get_event_loop().time()
                if self._first_fail_at is None:
                    self._first_fail_at = now
                if now - self._first_fail_at > config.RECONNECT_TOTAL_S:
                    await self.incoming.put((STATUS_EXPIRED, str(e)))
                    return
                await self.incoming.put((STATUS_RECONNECTING, str(e)))
                idx = min(self._backoff_idx, len(config.RECONNECT_BACKOFF) - 1)
                self._backoff_idx += 1
                await asyncio.sleep(config.RECONNECT_BACKOFF[idx])

    async def _one_session(self):
        async with grpc.aio.insecure_channel(self.target, options=self._channel_options) as ch:
            stub = can_pb2_grpc.CanServiceStub(ch)
            metadata = (("session-id", self.session_id),)

            async def request_iter():
                if self.current_filter is not None:
                    yield self.current_filter
                while True:
                    req = await self.outgoing.get()
                    if req is None:
                        return
                    if req.unsubscribe:
                        self.current_filter = None
                    else:
                        self.current_filter = req
                    yield req

            call = stub.Subscribe(request_iter(), metadata=metadata)
            await self.incoming.put((STATUS_CONNECTED, None))
            self._first_fail_at = None
            self._backoff_idx = 0
            async for resp in call:
                await self.incoming.put((KIND_RESPONSE, resp))
