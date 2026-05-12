import struct
import sys
import uuid
import queue
import time

import Ice


DEFAULT_PROXY = "catalog:tcp -h localhost -p 10000"
CATALOG_TYPE_ID = "::library::Catalog"
BOOKSTREAM_TYPE_ID = "::library::BookStream"
ICE_OBJECT_TYPE_ID = "::Ice::Object"


class OutBuf:
    def __init__(self):
        self.b = bytearray()

    def write_bool(self, v):
        self.b.append(1 if v else 0)

    def write_byte(self, v):
        self.b.append(v & 0xFF)

    def write_short(self, v):
        self.b += struct.pack("<h", v)

    def write_int(self, v):
        self.b += struct.pack("<i", v)

    def write_size(self, n):
        if n < 255:
            self.b.append(n)
        else:
            self.b.append(255)
            self.b += struct.pack("<I", n)

    def write_string(self, s):
        data = s.encode("utf-8")
        self.write_size(len(data))
        self.b += data

    def write_string_seq(self, seq):
        self.write_size(len(seq))
        for s in seq:
            self.write_string(s)

    def write_proxy(self, prx):
        ident = prx.ice_getIdentity()
        self.write_string(ident.name)
        self.write_string(ident.category)
        if not ident.name:
            return
        facet = prx.ice_getFacet()
        if facet:
            self.write_size(1)
            self.write_string(facet)
        else:
            self.write_size(0)
        self.write_byte(0)
        self.write_bool(False)
        self.write_byte(1)
        self.write_byte(0)
        self.write_byte(1)
        self.write_byte(1)
        eps = prx.ice_getEndpoints()
        self.write_size(len(eps))
        for ep in eps:
            info = ep.getInfo()
            self.write_short(info.type())
            body = OutBuf()
            body.write_string(info.host)
            body.write_int(info.port)
            body.write_int(info.timeout)
            body.write_bool(info.compress)
            body_bytes = bytes(body.b)
            encap_size = 4 + 2 + len(body_bytes)
            self.b += struct.pack("<I", encap_size)
            self.b += b"\x01\x01"
            self.b += body_bytes

    def encapsulation(self):
        body = bytes(self.b)
        size = 4 + 2 + len(body)
        return struct.pack("<I", size) + b"\x01\x01" + body


class InBuf:
    def __init__(self, data, start=6):
        self.b = data
        self.off = start

    def read_bool(self):
        v = self.b[self.off] != 0
        self.off += 1
        return v

    def read_byte(self):
        v = self.b[self.off]
        self.off += 1
        return v

    def read_short(self):
        v = struct.unpack_from("<h", self.b, self.off)[0]
        self.off += 2
        return v

    def read_int(self):
        v = struct.unpack_from("<i", self.b, self.off)[0]
        self.off += 4
        return v

    def read_size(self):
        n = self.b[self.off]
        self.off += 1
        if n == 255:
            n = struct.unpack_from("<I", self.b, self.off)[0]
            self.off += 4
        return n

    def read_string(self):
        n = self.read_size()
        s = bytes(self.b[self.off:self.off + n]).decode("utf-8")
        self.off += n
        return s

    def read_string_seq(self):
        n = self.read_size()
        return [self.read_string() for _ in range(n)]


def empty_encapsulation():
    return struct.pack("<I", 6) + b"\x01\x01"


def read_book(buf):
    return {
        "id": buf.read_int(),
        "title": buf.read_string(),
        "author": buf.read_string(),
        "year": buf.read_int(),
        "tags": buf.read_string_seq(),
    }


def read_add_book_result(buf):
    return {
        "bookId": buf.read_int(),
        "errorCode": buf.read_string(),
        "errorMessage": buf.read_string(),
    }


def read_remove_book_result(buf):
    return {
        "ok": buf.read_bool(),
        "errorCode": buf.read_string(),
        "errorMessage": buf.read_string(),
    }


def read_catalog_stats(buf):
    total = buf.read_int()
    n = buf.read_size()
    by_author = {}
    for _ in range(n):
        k = buf.read_string()
        v = buf.read_int()
        by_author[k] = v
    m = buf.read_size()
    recent = [read_book(buf) for _ in range(m)]
    return {"total": total, "byAuthor": by_author, "recent": recent}


VERBOSE = True


def hex_snippet(data, limit=160):
    h = data.hex()
    return h if len(h) <= limit else h[:limit] + "..."


def invoke(prx, op, in_encaps=None, parse_out=None):
    if in_encaps is None:
        in_encaps = empty_encapsulation()
    if VERBOSE:
        print(f"  -> ice_invoke({op!r}): in={len(in_encaps)}B [{hex_snippet(in_encaps)}]")
    ok, reply = prx.ice_invoke(op, Ice.OperationMode.Normal, in_encaps)
    if VERBOSE:
        print(f"  <- ice_invoke({op!r}): ok={ok} out={len(reply)}B [{hex_snippet(reply)}]")
    if not ok:
        raise RuntimeError(f"ice_invoke({op}): user exception, body={reply.hex()}")
    if parse_out is None:
        return None
    return parse_out(InBuf(reply))


def call_add_book(prx, title, author, year, tags):
    out = OutBuf()
    out.write_string(title)
    out.write_string(author)
    out.write_int(year)
    out.write_string_seq(tags)
    return invoke(prx, "addBook", out.encapsulation(), read_add_book_result)


def call_remove_book(prx, bid):
    out = OutBuf()
    out.write_int(bid)
    return invoke(prx, "removeBook", out.encapsulation(), read_remove_book_result)


def call_summary(prx):
    return invoke(prx, "summary", None, read_catalog_stats)


def call_find_by_author(adapter, prx, author, limit, timeout=10.0):
    q = queue.Queue()
    servant = BookStreamBlobject(q)
    cb_id = Ice.Identity()
    cb_id.name = "stream-" + uuid.uuid4().hex
    cb_id.category = ""
    cb_prx = adapter.add(servant, cb_id)
    try:
        out = OutBuf()
        out.write_string(author)
        out.write_int(limit)
        out.write_proxy(cb_prx)
        invoke(prx, "findByAuthor", out.encapsulation(), None)
        results = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                kind, val = q.get(timeout=remaining if remaining > 0 else 0.01)
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


class BookStreamBlobject(Ice.Blobject):
    def __init__(self, q):
        self.q = q

    def ice_invoke(self, in_encaps, current):
        op = current.operation
        if op == "onNext":
            book = read_book(InBuf(in_encaps))
            self.q.put(("next", book))
            return (True, empty_encapsulation())
        if op == "onCompleted":
            self.q.put(("done", None))
            return (True, empty_encapsulation())
        if op == "onError":
            buf = InBuf(in_encaps)
            code = buf.read_string()
            msg = buf.read_string()
            self.q.put(("error", (code, msg)))
            return (True, empty_encapsulation())
        if op == "ice_isA":
            type_id = InBuf(in_encaps).read_string()
            out = OutBuf()
            out.write_bool(type_id in (BOOKSTREAM_TYPE_ID, ICE_OBJECT_TYPE_ID))
            return (True, out.encapsulation())
        if op == "ice_id":
            out = OutBuf()
            out.write_string(BOOKSTREAM_TYPE_ID)
            return (True, out.encapsulation())
        if op == "ice_ids":
            out = OutBuf()
            out.write_string_seq([BOOKSTREAM_TYPE_ID, ICE_OBJECT_TYPE_ID])
            return (True, out.encapsulation())
        if op == "ice_ping":
            return (True, empty_encapsulation())
        raise Ice.OperationNotExistException()


def print_book(b, indent=0):
    s = "  " * indent
    print(f"{s}id={b['id']} year={b['year']} title={b['title']!r} author={b['author']!r} tags={list(b['tags'])}")


def print_stats(stats):
    print(f"  total: {stats['total']}")
    print(f"  byAuthor:")
    for k, v in sorted(stats["byAuthor"].items()):
        print(f"    {k!r}: {v}")
    print(f"  recent ({len(stats['recent'])}):")
    for b in stats["recent"]:
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
    res = call_add_book(prx, title, author, year, tags)
    if res["errorCode"]:
        print(f"  ERROR {res['errorCode']}: {res['errorMessage']}")
    else:
        print(f"  bookId={res['bookId']}")


def op_find_by_author(prx, adapter, **_):
    author = input("author: ").strip()
    limit = read_int("limit (0 = no limit): ")
    results = call_find_by_author(adapter, prx, author, limit)
    print(f"streamed {len(results)} books:")
    for b in results:
        print_book(b, 1)


def op_summary(prx, **_):
    stats = call_summary(prx)
    print("response:")
    print_stats(stats)


def op_remove_book(prx, **_):
    bid = read_int("book id (int): ")
    res = call_remove_book(prx, bid)
    if res["errorCode"]:
        print(f"  ERROR {res['errorCode']}: {res['errorMessage']}")
    else:
        print(f"  removed (ok={res['ok']})")


OPS = [
    ("addBook", "AddBookRequest{string,string,int,seq<string>}", "AddBookResult{int,string,string}", op_add_book),
    ("findByAuthor", "AuthorQuery{string,int} + BookStream*", "void (stream via callback)", op_find_by_author),
    ("summary", "void", "CatalogStats{int,dict<string,int>,seq<Book>}", op_summary),
    ("removeBook", "int", "RemoveBookResult{bool,string,string}", op_remove_book),
]


def menu_loop(adapter, prx):
    while True:
        print("\noperations:")
        for i, (name, in_t, out_t, _) in enumerate(OPS, 1):
            print(f"  {i}) {name}    in={in_t}    out={out_t}")
        print("  p) ice_ping")
        print("  i) ice_ids")
        print("  q) quit")
        try:
            choice = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return
        if choice in ("q", "quit", "exit"):
            return
        if choice == "p":
            try:
                prx.ice_ping()
                print("ice_ping: OK")
            except Ice.LocalException as e:
                print(f"ice_ping failed: {e}")
            continue
        if choice == "i":
            try:
                for x in prx.ice_ids():
                    print(f"  {x}")
            except Ice.LocalException as e:
                print(f"ice_ids failed: {e}")
            continue
        try:
            idx = int(choice) - 1
        except ValueError:
            print("invalid choice")
            continue
        if not 0 <= idx < len(OPS):
            print("invalid choice")
            continue
        name, _, _, fn = OPS[idx]
        try:
            fn(prx, adapter=adapter)
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
        try:
            if not base.ice_isA(CATALOG_TYPE_ID):
                print(f"proxy does not implement {CATALOG_TYPE_ID}")
                sys.exit(1)
            print(f"ice_isA({CATALOG_TYPE_ID}): True")
        except Ice.LocalException as e:
            print(f"ice_isA failed: {e}")
            sys.exit(1)
        adapter = communicator.createObjectAdapterWithEndpoints("CallbackAdapter", "tcp -h 127.0.0.1")
        adapter.activate()
        try:
            menu_loop(adapter, base)
        finally:
            adapter.deactivate()
    finally:
        communicator.destroy()


if __name__ == "__main__":
    main()
