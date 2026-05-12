import os
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


OPERATIONS = [
    ("addBook", "AddBookRequest", "AddBookResult"),
    ("findByAuthor", "AuthorQuery + BookStream*", "void (stream via callback)"),
    ("summary", "void", "CatalogStats"),
    ("removeBook", "int", "RemoveBookResult"),
]


def menu_loop(communicator, adapter, prx):
    while True:
        print("\navailable operations:")
        for i, (name, in_t, out_t) in enumerate(OPERATIONS, 1):
            print(f"  {i}) {name}    in={in_t}    out={out_t}")
        print("  5) ice_ids  (introspection)")
        print("  6) ice_ping (introspection)")
        print("  q) quit")
        try:
            choice = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return
        if choice in ("q", "quit", "exit"):
            return
        try:
            if choice == "1":
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
            elif choice == "2":
                author = input("author: ").strip()
                limit = read_int("limit (0 = no limit): ")
                results = call_find_by_author(communicator, adapter, prx, author, limit)
                print(f"streamed {len(results)} books:")
                for b in results:
                    print_book(b, 1)
            elif choice == "3":
                stats = prx.summary()
                print("response:")
                print_stats(stats)
            elif choice == "4":
                bid = read_int("book id (int): ")
                res = prx.removeBook(bid)
                if res.errorCode:
                    print(f"  ERROR {res.errorCode}: {res.errorMessage}")
                else:
                    print(f"  removed (ok={res.ok})")
            elif choice == "5":
                ids = prx.ice_ids()
                print("ice_ids:")
                for x in ids:
                    print(f"  {x}")
            elif choice == "6":
                prx.ice_ping()
                print("ice_ping: OK")
            else:
                print("invalid choice")
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
