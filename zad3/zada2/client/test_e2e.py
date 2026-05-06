import asyncio
import os
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "generated"))
sys.path.insert(0, HERE)

import grpc
import can_pb2
import can_pb2_grpc

TARGET = "localhost:50051"
CHAN_OPTS = [
    ("grpc.keepalive_time_ms", 20000),
    ("grpc.keepalive_timeout_ms", 10000),
    ("grpc.keepalive_permit_without_calls", 1),
    ("grpc.http2.max_pings_without_data", 0),
]


def header(t):
    print(f"\n=== {t} ===")


async def channel():
    return grpc.aio.insecure_channel(TARGET, options=CHAN_OPTS)


async def test_list_and_stats():
    header("test 1: ListMessages + GetStats (success + NOT_FOUND)")
    async with grpc.aio.insecure_channel(TARGET, options=CHAN_OPTS) as ch:
        stub = can_pb2_grpc.CanServiceStub(ch)
        r = await stub.ListMessages(can_pb2.ListMessagesRequest())
        assert len(r.messages) >= 20, f"expected >=20 messages, got {len(r.messages)}"
        cats = {m.category for m in r.messages}
        assert len(cats) == 10, f"expected 10 distinct categories, got {len(cats)}"
        print(f"  ListMessages -> {len(r.messages)} messages, {len(cats)} categories OK")

        r2 = await stub.GetStats(can_pb2.StatsRequest(message_name="BMSMasterStatus"))
        assert r2.last_seen_ms >= 0
        print(f"  GetStats(BMSMasterStatus) -> last_seen_ms={r2.last_seen_ms}, total={r2.total_count} OK")

        try:
            await stub.GetStats(can_pb2.StatsRequest(message_name="DoesNotExist"))
            assert False, "expected NOT_FOUND"
        except grpc.aio.AioRpcError as e:
            assert e.code() == grpc.StatusCode.NOT_FOUND
            print(f"  GetStats(DoesNotExist) -> NOT_FOUND OK")

        try:
            await stub.GetStats(can_pb2.StatsRequest(message_name=""))
            assert False, "expected INVALID_ARGUMENT"
        except grpc.aio.AioRpcError as e:
            assert e.code() == grpc.StatusCode.INVALID_ARGUMENT
            print(f"  GetStats(\"\") -> INVALID_ARGUMENT OK")


async def test_subscribe_no_session_id():
    header("test 2: Subscribe without session-id -> INVALID_ARGUMENT")
    async with grpc.aio.insecure_channel(TARGET, options=CHAN_OPTS) as ch:
        stub = can_pb2_grpc.CanServiceStub(ch)

        async def empty():
            if False:
                yield
            return

        call = stub.Subscribe(empty())
        try:
            async for _ in call:
                pass
            assert False, "expected INVALID_ARGUMENT"
        except grpc.aio.AioRpcError as e:
            assert e.code() == grpc.StatusCode.INVALID_ARGUMENT, f"got {e.code()}"
            print(f"  no session-id -> INVALID_ARGUMENT OK")


async def test_filter_by_category():
    header("test 3: filter by category (BMS, ENGINE only)")
    sid = str(uuid.uuid4())
    async with grpc.aio.insecure_channel(TARGET, options=CHAN_OPTS) as ch:
        stub = can_pb2_grpc.CanServiceStub(ch)
        out = asyncio.Queue()
        await out.put(can_pb2.SubscribeRequest(categories=[can_pb2.BMS, can_pb2.ENGINE]))

        async def reqs():
            while True:
                r = await out.get()
                if r is None:
                    return
                yield r

        call = stub.Subscribe(reqs(), metadata=(("session-id", sid),))
        seen_cats = set()
        seen = 0
        while seen < 4:
            resp = await asyncio.wait_for(call.read(), timeout=60)
            if resp.WhichOneof("payload") == "update":
                u = resp.update
                seen_cats.add(u.category)
                assert u.category in (can_pb2.BMS, can_pb2.ENGINE), \
                    f"got unexpected category {u.category}"
                seen += 1
        await out.put(None)
        call.cancel()
        print(f"  saw {seen} updates, categories: {[can_pb2.MessageCategory.Name(c) for c in seen_cats]} OK")


async def test_filter_by_name():
    header("test 4: filter by exact name (BMSMasterStatus only)")
    sid = str(uuid.uuid4())
    async with grpc.aio.insecure_channel(TARGET, options=CHAN_OPTS) as ch:
        stub = can_pb2_grpc.CanServiceStub(ch)
        out = asyncio.Queue()
        await out.put(can_pb2.SubscribeRequest(message_names=["BMSMasterStatus"]))

        async def reqs():
            while True:
                r = await out.get()
                if r is None:
                    return
                yield r

        call = stub.Subscribe(reqs(), metadata=(("session-id", sid),))
        seen = 0
        while seen < 1:
            resp = await asyncio.wait_for(call.read(), timeout=90)
            if resp.WhichOneof("payload") == "update":
                u = resp.update
                assert u.message_name == "BMSMasterStatus", f"got {u.message_name}"
                seen += 1
        await out.put(None)
        call.cancel()
        print(f"  saw {seen} update(s), all BMSMasterStatus OK")


async def test_change_filter_midstream():
    header("test 5: change filter mid-stream (BMS -> CHARGER)")
    sid = str(uuid.uuid4())
    async with grpc.aio.insecure_channel(TARGET, options=CHAN_OPTS) as ch:
        stub = can_pb2_grpc.CanServiceStub(ch)
        out = asyncio.Queue()
        await out.put(can_pb2.SubscribeRequest(categories=[can_pb2.BMS]))

        async def reqs():
            while True:
                r = await out.get()
                if r is None:
                    return
                yield r

        call = stub.Subscribe(reqs(), metadata=(("session-id", sid),))
        phase1_cats, phase2_cats = set(), set()
        phase = 1
        seen = 0
        while True:
            resp = await asyncio.wait_for(call.read(), timeout=90)
            if resp.WhichOneof("payload") != "update":
                continue
            cat = resp.update.category
            if phase == 1:
                phase1_cats.add(cat)
                assert cat == can_pb2.BMS
                seen += 1
                if seen >= 2:
                    await out.put(can_pb2.SubscribeRequest(categories=[can_pb2.CHARGER]))
                    phase = 2
                    seen = 0
            else:
                if cat == can_pb2.BMS:
                    continue
                phase2_cats.add(cat)
                assert cat == can_pb2.CHARGER
                seen += 1
                if seen >= 1:
                    break
        await out.put(None)
        call.cancel()
        print(f"  phase1 {[can_pb2.MessageCategory.Name(c) for c in phase1_cats]} -> phase2 {[can_pb2.MessageCategory.Name(c) for c in phase2_cats]} OK")


async def test_unsubscribe_no_disconnect():
    header("test 6: unsubscribe without disconnect, then resubscribe")
    sid = str(uuid.uuid4())
    async with grpc.aio.insecure_channel(TARGET, options=CHAN_OPTS) as ch:
        stub = can_pb2_grpc.CanServiceStub(ch)
        out = asyncio.Queue()
        await out.put(can_pb2.SubscribeRequest(categories=[can_pb2.LIGHTS]))

        async def reqs():
            while True:
                r = await out.get()
                if r is None:
                    return
                yield r

        call = stub.Subscribe(reqs(), metadata=(("session-id", sid),))

        inbox = asyncio.Queue()

        async def pump():
            try:
                async for resp in call:
                    await inbox.put(resp)
            except Exception as e:
                await inbox.put(e)
            await inbox.put(None)

        pump_task = asyncio.create_task(pump())

        seen_lights = 0
        while seen_lights < 1:
            resp = await asyncio.wait_for(inbox.get(), timeout=120)
            if isinstance(resp, Exception):
                raise resp
            if resp is None:
                raise RuntimeError("stream ended unexpectedly")
            if resp.WhichOneof("payload") == "update":
                seen_lights += 1
        print(f"  phase1 received {seen_lights} LIGHTS update(s)")

        await out.put(can_pb2.SubscribeRequest(unsubscribe=True))
        await asyncio.sleep(0.5)
        unexpected = 0
        deadline = asyncio.get_event_loop().time() + 3
        while asyncio.get_event_loop().time() < deadline:
            remaining = deadline - asyncio.get_event_loop().time()
            try:
                resp = await asyncio.wait_for(inbox.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if isinstance(resp, Exception) or resp is None:
                raise RuntimeError(f"stream ended during unsubscribe: {resp}")
            if resp.WhichOneof("payload") == "update":
                unexpected += 1
        assert unexpected == 0, f"got {unexpected} updates after unsubscribe"
        print(f"  phase2 no updates for 3s after unsubscribe OK")

        await out.put(can_pb2.SubscribeRequest(categories=[can_pb2.SENSORS]))
        seen_sensors = 0
        while seen_sensors < 1:
            resp = await asyncio.wait_for(inbox.get(), timeout=120)
            if isinstance(resp, Exception):
                raise resp
            if resp is None:
                raise RuntimeError("stream ended unexpectedly")
            if resp.WhichOneof("payload") == "update":
                assert resp.update.category == can_pb2.SENSORS
                seen_sensors += 1
        print(f"  phase3 received {seen_sensors} SENSORS update(s) after resubscribe OK")
        await out.put(None)
        call.cancel()
        pump_task.cancel()
        try:
            await pump_task
        except (asyncio.CancelledError, Exception):
            pass


async def _client_run(sid, cats, want, results, label):
    async with grpc.aio.insecure_channel(TARGET, options=CHAN_OPTS) as ch:
        stub = can_pb2_grpc.CanServiceStub(ch)
        out = asyncio.Queue()
        await out.put(can_pb2.SubscribeRequest(categories=cats))

        async def reqs():
            while True:
                r = await out.get()
                if r is None:
                    return
                yield r

        call = stub.Subscribe(reqs(), metadata=(("session-id", sid),))
        seen = []
        while len(seen) < want:
            resp = await asyncio.wait_for(call.read(), timeout=120)
            if resp.WhichOneof("payload") == "update":
                seen.append(resp.update.category)
        await out.put(None)
        call.cancel()
        results[label] = seen


async def test_multiple_clients():
    header("test 7: two concurrent clients with different filters")
    results = {}
    sid_a = str(uuid.uuid4())
    sid_b = str(uuid.uuid4())
    await asyncio.gather(
        _client_run(sid_a, [can_pb2.BMS], 2, results, "A_BMS"),
        _client_run(sid_b, [can_pb2.POWER], 2, results, "B_POWER"),
    )
    a_cats = set(results["A_BMS"])
    b_cats = set(results["B_POWER"])
    assert a_cats == {can_pb2.BMS}, f"client A saw {a_cats}"
    assert b_cats == {can_pb2.POWER}, f"client B saw {b_cats}"
    print(f"  client A (BMS): {len(results['A_BMS'])} updates, all BMS OK")
    print(f"  client B (POWER): {len(results['B_POWER'])} updates, all POWER OK")


async def test_reconnect_with_buffer():
    header("test 8: disconnect, server buffers updates, reconnect drains buffer")
    sid = str(uuid.uuid4())

    ch1 = grpc.aio.insecure_channel(TARGET, options=CHAN_OPTS)
    stub1 = can_pb2_grpc.CanServiceStub(ch1)
    out1 = asyncio.Queue()
    await out1.put(can_pb2.SubscribeRequest(categories=[can_pb2.DASHBOARD, can_pb2.PEDALS]))

    async def reqs1():
        while True:
            r = await out1.get()
            if r is None:
                return
            yield r

    call1 = stub1.Subscribe(reqs1(), metadata=(("session-id", sid),))
    seen_phase1 = 0
    while seen_phase1 < 1:
        resp = await asyncio.wait_for(call1.read(), timeout=120)
        if resp.WhichOneof("payload") == "update":
            seen_phase1 += 1
    print(f"  phase1 received {seen_phase1} update(s), now disconnecting abruptly")
    call1.cancel()
    await ch1.close()

    await asyncio.sleep(8)

    async with grpc.aio.insecure_channel(TARGET, options=CHAN_OPTS) as ch2:
        stub2 = can_pb2_grpc.CanServiceStub(ch2)
        out2 = asyncio.Queue()

        async def reqs2():
            while True:
                r = await out2.get()
                if r is None:
                    return
                yield r

        call2 = stub2.Subscribe(reqs2(), metadata=(("session-id", sid),))
        got_resumed = False
        snapshot_size = 0
        seen_post = 0
        while seen_post < 1:
            resp = await asyncio.wait_for(call2.read(), timeout=120)
            which = resp.WhichOneof("payload")
            if which == "session_info":
                assert resp.session_info.resumed
                got_resumed = True
            elif which == "snapshot":
                snapshot_size = len(resp.snapshot.entries)
                for u in resp.snapshot.entries:
                    assert u.category in (can_pb2.DASHBOARD, can_pb2.PEDALS), \
                        f"snapshot has wrong category {u.category}"
            elif which == "update":
                seen_post += 1
        await out2.put(None)
        call2.cancel()
        assert got_resumed, "expected SessionInfo(resumed=true)"
        assert snapshot_size > 0, "expected non-empty snapshot from buffer"
        print(f"  phase2 resumed=true, buffered={snapshot_size} entries (DASHBOARD/PEDALS), live updates resumed OK")


async def test_filter_isolation():
    header("test 9: same generator, different filters reach correct subscribers")
    sid_a = str(uuid.uuid4())
    sid_b = str(uuid.uuid4())
    sid_c = str(uuid.uuid4())
    results = {}
    await asyncio.gather(
        _client_run(sid_a, [can_pb2.BMS, can_pb2.ENGINE], 3, results, "A"),
        _client_run(sid_b, [can_pb2.BMS], 2, results, "B"),
        _client_run(sid_c, [can_pb2.ENGINE], 2, results, "C"),
    )
    assert set(results["A"]).issubset({can_pb2.BMS, can_pb2.ENGINE})
    assert set(results["B"]) == {can_pb2.BMS}
    assert set(results["C"]) == {can_pb2.ENGINE}
    print(f"  client A {len(results['A'])} updates from BMS+ENGINE OK")
    print(f"  client B {len(results['B'])} updates from BMS only OK")
    print(f"  client C {len(results['C'])} updates from ENGINE only OK")


async def test_rapid_reconnect():
    header("test 10: rapid reconnect with same session-id (race condition)")
    sid = str(uuid.uuid4())
    ch1 = grpc.aio.insecure_channel(TARGET, options=CHAN_OPTS)
    stub1 = can_pb2_grpc.CanServiceStub(ch1)
    out1 = asyncio.Queue()
    await out1.put(can_pb2.SubscribeRequest(categories=[can_pb2.LIGHTS]))

    async def reqs1():
        while True:
            r = await out1.get()
            if r is None:
                return
            yield r

    call1 = stub1.Subscribe(reqs1(), metadata=(("session-id", sid),))
    inbox1 = asyncio.Queue()
    saw_aborted = asyncio.Event()

    async def pump1():
        try:
            async for resp in call1:
                await inbox1.put(resp)
        except grpc.aio.AioRpcError as e:
            if e.code() == grpc.StatusCode.ABORTED:
                saw_aborted.set()
        except Exception:
            pass

    pump_task = asyncio.create_task(pump1())
    await asyncio.sleep(0.3)

    async with grpc.aio.insecure_channel(TARGET, options=CHAN_OPTS) as ch2:
        stub2 = can_pb2_grpc.CanServiceStub(ch2)
        out2 = asyncio.Queue()
        await out2.put(can_pb2.SubscribeRequest(categories=[can_pb2.SENSORS]))

        async def reqs2():
            while True:
                r = await out2.get()
                if r is None:
                    return
                yield r

        call2 = stub2.Subscribe(reqs2(), metadata=(("session-id", sid),))
        seen = 0
        async for resp in call2:
            if resp.WhichOneof("payload") == "update":
                assert resp.update.category == can_pb2.SENSORS
                seen += 1
                if seen >= 1:
                    break
        await out2.put(None)
        call2.cancel()

    await asyncio.wait_for(saw_aborted.wait(), timeout=5)
    pump_task.cancel()
    try:
        await pump_task
    except (asyncio.CancelledError, Exception):
        pass
    await ch1.close()
    print(f"  old session ABORTED on takeover, new session got {seen} SENSORS update(s) OK")


async def test_combined_filter():
    header("test 11: combined filter (category AND name)")
    sid = str(uuid.uuid4())
    async with grpc.aio.insecure_channel(TARGET, options=CHAN_OPTS) as ch:
        stub = can_pb2_grpc.CanServiceStub(ch)
        out = asyncio.Queue()
        await out.put(can_pb2.SubscribeRequest(
            categories=[can_pb2.BMS],
            message_names=["BMSMasterStatus", "BMSCellVoltages"],
        ))

        async def reqs():
            while True:
                r = await out.get()
                if r is None:
                    return
                yield r

        call = stub.Subscribe(reqs(), metadata=(("session-id", sid),))
        seen = 0
        names_seen = set()
        while seen < 2:
            resp = await asyncio.wait_for(call.read(), timeout=120)
            if resp.WhichOneof("payload") == "update":
                u = resp.update
                assert u.category == can_pb2.BMS, f"got non-BMS: {u.category}"
                assert u.message_name in ("BMSMasterStatus", "BMSCellVoltages"), \
                    f"got unexpected name: {u.message_name}"
                names_seen.add(u.message_name)
                seen += 1
        await out.put(None)
        call.cancel()
        print(f"  saw {seen} updates, names: {sorted(names_seen)}, all BMS+listed OK")


async def test_session_ttl_expires():
    header("test 12: session TTL expires after disconnect (requires TTL=8s server)")
    sid = str(uuid.uuid4())

    ch1 = grpc.aio.insecure_channel(TARGET, options=CHAN_OPTS)
    stub1 = can_pb2_grpc.CanServiceStub(ch1)
    out1 = asyncio.Queue()
    await out1.put(can_pb2.SubscribeRequest(categories=[can_pb2.LIGHTS]))

    async def reqs1():
        while True:
            r = await out1.get()
            if r is None:
                return
            yield r

    call1 = stub1.Subscribe(reqs1(), metadata=(("session-id", sid),))

    inbox = asyncio.Queue()

    async def pump():
        try:
            async for resp in call1:
                await inbox.put(resp)
        except Exception:
            pass

    pump_task = asyncio.create_task(pump())
    await asyncio.sleep(2)
    call1.cancel()
    pump_task.cancel()
    try:
        await pump_task
    except (asyncio.CancelledError, Exception):
        pass
    await ch1.close()
    print(f"  phase1 disconnected, waiting >TTL for purger to drop session")

    await asyncio.sleep(15)

    async with grpc.aio.insecure_channel(TARGET, options=CHAN_OPTS) as ch2:
        stub2 = can_pb2_grpc.CanServiceStub(ch2)
        out2 = asyncio.Queue()
        await out2.put(can_pb2.SubscribeRequest(categories=[can_pb2.SENSORS]))

        async def reqs2():
            while True:
                r = await out2.get()
                if r is None:
                    return
                yield r

        call2 = stub2.Subscribe(reqs2(), metadata=(("session-id", sid),))
        got_session_info = False
        seen_update = False
        deadline = asyncio.get_event_loop().time() + 60
        while not seen_update and asyncio.get_event_loop().time() < deadline:
            try:
                resp = await asyncio.wait_for(
                    call2.read(),
                    timeout=deadline - asyncio.get_event_loop().time(),
                )
            except asyncio.TimeoutError:
                break
            which = resp.WhichOneof("payload")
            if which == "session_info":
                got_session_info = True
            elif which == "update":
                seen_update = True
        await out2.put(None)
        call2.cancel()
        assert not got_session_info, \
            "expected NO SessionInfo (session should have expired and be created fresh)"
        assert seen_update, "expected at least one fresh update from new session"
        print(f"  phase2 reconnect with same id -> resumed=false (no SessionInfo), fresh updates OK")


async def test_server_restart_forgets_session():
    header("test 13: server restart forgets sessions (manual prerequisite)")
    print(f"  SKIP: requires manual server kill+restart between phases.")
    print(f"  Demo path: kill server, restart, reconnect with old session-id -> resumed=false.")


async def main():
    await test_list_and_stats()
    await test_subscribe_no_session_id()
    await test_filter_by_category()
    await test_filter_by_name()
    await test_change_filter_midstream()
    await test_unsubscribe_no_disconnect()
    await test_multiple_clients()
    await test_reconnect_with_buffer()
    await test_filter_isolation()
    await test_rapid_reconnect()
    await test_combined_filter()
    print("\nALL TESTS PASSED (1-11)")
    print("test 12 (TTL expire) needs server with config-test.properties (TTL=8s)")
    print("test 13 (server restart) is manual demo only")


async def main_ttl():
    await test_session_ttl_expires()
    print("\nTTL TEST PASSED")


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) > 1 and _sys.argv[1] == "ttl":
        asyncio.run(main_ttl())
    else:
        asyncio.run(main())
