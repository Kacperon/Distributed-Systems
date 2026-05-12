import os
import struct
import sys
import uuid
import queue
import time

import Ice


SLICE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog.ice")
DEFAULT_PROXY = "catalog:tcp -h localhost -p 10000"


Ice.loadSlice(f"-I. -I{os.path.dirname(SLICE_FILE)} {SLICE_FILE}")
import library


class BookStreamI(library.BookStream):
    def __init__(self):
        self.q = queue.Queue()

    def onNext(self, book, current=None):
        self.q.put(("next", book))

    def onCompleted(self, current=None):
        self.q.put(("done", None))

    def onError(self, code, message, current=None):
        self.q.put(("error", (code, message)))


def call_find_by_author(communicator, adapter, prx, author, limit, timeout=10.0):
    servant = BookStreamI()
    cb_id = Ice.Identity()
    cb_id.name = "stream-" + uuid.uuid4().hex
    cb_id.category = ""
    cb_prx = library.BookStreamPrx.uncheckedCast(adapter.add(servant, cb_id))
    try:
        prx.findByAuthor(library.AuthorQuery(author, limit), cb_prx)
        results = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                kind, val = servant.q.get(timeout=remaining if remaining > 0 else 0.01)
            except queue.Empty:
                raise RuntimeError("findByAuthor: timed out waiting for stream completion")
            if kind == "next":
                results.append(val)
            elif kind == "done":
                return results
            elif kind == "error":
                code, msg = val
                raise RuntimeError(f"findByAuthor stream error: {code}: {msg}")
    finally:
        adapter.remove(cb_id)


def print_book(b, indent=0):
    s = "  " * indent
    tags = list(b.tags) if b.tags else []
    print(f"{s}id={b.id} year={b.year} title={b.title!r} author={b.author!r} tags={tags}")


def print_stats(stats):
    print(f"  total: {stats.total}")
    print(f"  byAuthor:")
    for k, v in sorted(stats.byAuthor.items()):
        print(f"    {k!r}: {v}")
    print(f"  recent ({len(stats.recent)}):")
    for b in stats.recent:
        print_book(b, 2)


def read_int(prompt, default=0):
    s = input(prompt).strip()
    if not s:
        return default
    try:
        return int(s)
    except ValueError:
        print("  invalid int, using 0")
        return 0


def read_csv(prompt):
    s = input(prompt).strip()
    if not s:
        return []
    return [t.strip() for t in s.split(",")]


def op_add_book(prx, **_):
    title = input("title: ").strip()
    author = input("author: ").strip()
    year = read_int("year (int): ")
    tags = read_csv("tags (comma-separated, optional): ")
    req = library.AddBookRequest(title, author, year, tags)
    res = prx.addBook(req)
    if res.errorCode:
        print(f"  ERROR {res.errorCode}: {res.errorMessage}")
    else:
        print(f"  bookId={res.bookId}")


def op_find_by_author(prx, communicator, adapter):
    author = input("author: ").strip()
    limit = read_int("limit (0 = no limit): ")
    results = call_find_by_author(communicator, adapter, prx, author, limit)
    print(f"streamed {len(results)} books:")
    for b in results:
        print_book(b, 1)


def op_summary(prx, **_):
    stats = prx.summary()
    print("response:")
    print_stats(stats)


def op_remove_book(prx, **_):
    bid = read_int("book id (int): ")
    res = prx.removeBook(bid)
    if res.errorCode:
        print(f"  ERROR {res.errorCode}: {res.errorMessage}")
    else:
        print(f"  removed (ok={res.ok})")


def op_ice_ids(prx, **_):
    ids = prx.ice_ids()
    print("ice_ids:")
    for x in ids:
        print(f"  {x}")


def op_ice_ping(prx, **_):
    prx.ice_ping()
    print("ice_ping: OK")


def _read_ice_string(buf, off):
    n = buf[off]
    off += 1
    if n == 255:
        n = struct.unpack_from("<I", buf, off)[0]
        off += 4
    return buf[off:off + n].decode("utf-8"), off + n


def op_remove_book_invoke(prx, **_):
    bid = read_int("book id (int): ")

    payload = struct.pack("<i", bid)
    encap_size = 4 + 2 + len(payload)
    in_bytes = struct.pack("<I", encap_size) + b"\x01\x01" + payload
    print(f"  marshaled in-params: {len(in_bytes)} bytes  ({in_bytes.hex()})")
    print(f"    layout: [size:4={encap_size}] [encoding:2=1.1] [int32:4={bid}]")

    ok, reply = prx.ice_invoke("removeBook", Ice.OperationMode.Normal, in_bytes)
    print(f"  ice_invoke returned ok={ok}, reply: {len(reply)} bytes  ({reply.hex()})")

    off = 6
    res_ok = bool(reply[off])
    off += 1
    err_code, off = _read_ice_string(reply, off)
    err_msg, off = _read_ice_string(reply, off)
    print(f"    unmarshaled: bool={res_ok}, errorCode={err_code!r}, errorMessage={err_msg!r}")

    if err_code:
        print(f"  ERROR {err_code}: {err_msg}  [via ice_invoke + manual struct]")
    else:
        print(f"  removed (ok={res_ok})  [via ice_invoke + manual struct]")


BUSINESS_OP_HANDLERS = {
    "addBook": ("AddBookRequest", "AddBookResult", op_add_book),
    "findByAuthor": ("AuthorQuery + BookStream*", "void (stream via callback)", op_find_by_author),
    "summary": ("void", "CatalogStats", op_summary),
    "removeBook": ("int", "RemoveBookResult", op_remove_book),
}

INTROSPECTION_OPS = [
    ("ice_ids", "void", "StringSeq", op_ice_ids),
    ("ice_ping", "void", "void", op_ice_ping),
]

DYNAMIC_INVOCATION_OPS = [
    ("removeBook[ice_invoke]", "int (manual OutputStream)", "RemoveBookResult (manual InputStream)", op_remove_book_invoke),
]


def discover_business_ops(proxy_cls):
    skip_exact = {"checkedCast", "uncheckedCast"}
    skip_prefix = ("ice_", "begin_", "end_", "_")
    out = []
    for n in sorted(dir(proxy_cls)):
        if n in skip_exact:
            continue
        if n.endswith("Async"):
            continue
        if any(n.startswith(p) for p in skip_prefix):
            continue
        if callable(getattr(proxy_cls, n)):
            out.append(n)
    return out


def menu_loop(communicator, adapter, prx):
    discovered = discover_business_ops(library.CatalogPrx)
    print(f"discovered business ops on library.CatalogPrx: {discovered}")

    entries = []
    for name in discovered:
        spec = BUSINESS_OP_HANDLERS.get(name)
        if spec is None:
            entries.append((name, "?", "?", None))
        else:
            in_t, out_t, fn = spec
            entries.append((name, in_t, out_t, fn))
    for name, in_t, out_t, fn in INTROSPECTION_OPS:
        entries.append((name, in_t, out_t, fn))
    for name, in_t, out_t, fn in DYNAMIC_INVOCATION_OPS:
        entries.append((name, in_t, out_t, fn))

    dispatch = {str(i + 1): e for i, e in enumerate(entries)}

    while True:
        print("\navailable operations:")
        for i, (name, in_t, out_t, _) in enumerate(entries, 1):
            print(f"  {i}) {name}    in={in_t}    out={out_t}")
        print("  q) quit")
        try:
            choice = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return
        if choice in ("q", "quit", "exit"):
            return
        entry = dispatch.get(choice)
        if entry is None:
            print("invalid choice")
            continue
        name, _, _, fn = entry
        if fn is None:
            print(f"  no handler wired for op {name!r}")
            continue
        try:
            fn(prx, communicator=communicator, adapter=adapter)
        except Ice.LocalException as e:
            print(f"ice error: {type(e).__name__}: {e}")
        except RuntimeError as e:
            print(f"error: {e}")


def main():
    proxy_str = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PROXY
    print(f"connecting via proxy: {proxy_str!r}")
    communicator = Ice.initialize(sys.argv)
    try:
        base = communicator.stringToProxy(proxy_str)
        if base is None:
            print("could not parse proxy string")
            sys.exit(1)
        try:
            base.ice_ping()
        except Ice.LocalException as e:
            print(f"could not reach server: {type(e).__name__}: {e}")
            sys.exit(1)
        try:
            type_ids = base.ice_ids()
            print(f"discovered ice_ids: {type_ids}")
        except Ice.LocalException as e:
            print(f"ice_ids failed: {e}")
        prx = library.CatalogPrx.checkedCast(base)
        if prx is None:
            print("proxy does not expose ::library::Catalog")
            sys.exit(1)
        adapter = communicator.createObjectAdapterWithEndpoints("CallbackAdapter", "tcp -h 127.0.0.1")
        adapter.activate()
        try:
            menu_loop(communicator, adapter, prx)
        finally:
            adapter.deactivate()
    finally:
        communicator.destroy()


if __name__ == "__main__":
    main()
