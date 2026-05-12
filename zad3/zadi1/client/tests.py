import sys
import time

import Ice

import main
from main import (
    DEFAULT_PROXY,
    CATALOG_TYPE_ID,
    call_add_book,
    call_remove_book,
    call_summary,
    call_find_by_author,
)


PASS = 0
FAIL = 0
FAILS = []


def check(name, ok, detail=""):
    global PASS, FAIL
    status = "PASS" if ok else "FAIL"
    suffix = f" -- {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    if ok:
        PASS += 1
    else:
        FAIL += 1
        FAILS.append(name)


def expect_error(name, fn, expected_code):
    try:
        res = fn()
        got = res.get("errorCode") if isinstance(res, dict) else None
        check(name, got == expected_code, f"got errorCode={got!r}")
    except RuntimeError as e:
        check(name, expected_code in str(e), f"runtime error: {e}")
    except Ice.LocalException as e:
        check(name, False, f"local exception: {type(e).__name__}: {e}")


def run():
    main.VERBOSE = False
    proxy_str = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PROXY
    print(f"connecting via proxy: {proxy_str!r}")

    communicator = Ice.initialize(sys.argv)
    try:
        base = communicator.stringToProxy(proxy_str)
        base.ice_ping()

        ids = base.ice_ids()
        check("ice_ids contains library::Catalog", "::library::Catalog" in ids, f"ids={ids}")
        check("ice_ids contains Ice::Object", "::Ice::Object" in ids)
        check(
            "proxy responds to ice_id with library::Catalog",
            base.ice_id() == CATALOG_TYPE_ID,
            f"ice_id()={base.ice_id()!r}",
        )
        check("ice_isA(library::Catalog) is True", base.ice_isA(CATALOG_TYPE_ID))
        check("ice_isA(library::NoSuchType) is False", not base.ice_isA("::library::NoSuchType"))

        prx = base

        adapter = communicator.createObjectAdapterWithEndpoints("TestCallbackAdapter", "tcp -h 127.0.0.1")
        adapter.activate()

        suffix = f"-test-{int(time.time() * 1000)}"
        author = f"Test Author{suffix}"

        ids_added = []
        titles = [f"Alpha{suffix}", f"Beta{suffix}", f"Gamma{suffix}"]
        for t in titles:
            res = call_add_book(prx, t, author, 2026, ["tag1", "tag2"])
            check(f"AddBook {t!r} succeeded", res["errorCode"] == "" and res["bookId"] > 0)
            ids_added.append(res["bookId"])
        check(
            "AddBook x3 returned distinct positive ids",
            len(set(ids_added)) == 3 and all(i > 0 for i in ids_added),
            f"ids={ids_added}",
        )

        expect_error(
            "AddBook duplicate -> ALREADY_EXISTS",
            lambda: call_add_book(prx, titles[0], author, 2026, []),
            "ALREADY_EXISTS",
        )
        expect_error(
            "AddBook empty title -> INVALID_ARGUMENT",
            lambda: call_add_book(prx, "", "x", 1, []),
            "INVALID_ARGUMENT",
        )
        expect_error(
            "AddBook empty author -> INVALID_ARGUMENT",
            lambda: call_add_book(prx, "x", "", 1, []),
            "INVALID_ARGUMENT",
        )
        expect_error(
            "AddBook negative year -> INVALID_ARGUMENT",
            lambda: call_add_book(prx, f"x{suffix}", f"y{suffix}", -1, []),
            "INVALID_ARGUMENT",
        )

        found = call_find_by_author(adapter, prx, author, 0)
        check(
            "FindByAuthor returns all 3 (streamed via callback)",
            len(found) == 3 and {b["title"] for b in found} == set(titles),
            f"got titles={[b['title'] for b in found]}",
        )
        check(
            "FindByAuthor returned books carry tags",
            all(list(b["tags"]) == ["tag1", "tag2"] for b in found),
        )

        found_one = call_find_by_author(adapter, prx, author, 1)
        check("FindByAuthor with limit=1 returns 1", len(found_one) == 1)

        found_none = call_find_by_author(adapter, prx, f"ZZZ-no-such-author{suffix}", 0)
        check("FindByAuthor with no match returns empty", len(found_none) == 0)

        try:
            call_find_by_author(adapter, prx, "", 0)
            check("FindByAuthor empty author -> stream error", False)
        except RuntimeError as e:
            check("FindByAuthor empty author -> stream error", "INVALID_ARGUMENT" in str(e), f"msg={e}")

        stats = call_summary(prx)
        check(
            "Summary contains test author with count >= 3",
            stats["byAuthor"].get(author, 0) >= 3,
            f"byAuthor[{author!r}]={stats['byAuthor'].get(author, 0)}",
        )
        check("Summary total >= 3", stats["total"] >= 3, f"total={stats['total']}")
        check("Summary recent capped at 5", len(stats["recent"]) <= 5, f"len={len(stats['recent'])}")

        for i in ids_added:
            res = call_remove_book(prx, i)
            check(f"RemoveBook id={i} succeeded", res["ok"] and res["errorCode"] == "")

        expect_error(
            "RemoveBook unknown id -> NOT_FOUND",
            lambda: call_remove_book(prx, 10_000_000),
            "NOT_FOUND",
        )

        found_after = call_find_by_author(adapter, prx, author, 0)
        check("FindByAuthor after cleanup returns 0", len(found_after) == 0)

        suffix2 = f"{suffix}-ext"
        extra_ids = []

        zb = call_add_book(prx, f"ZeroYear{suffix2}", f"ZeroAuthor{suffix2}", 0, [])
        check("AddBook year=0 boundary accepted", zb["errorCode"] == "" and zb["bookId"] > 0)
        extra_ids.append(zb["bookId"])

        nt = call_add_book(prx, f"NoTags{suffix2}", f"NoTagsAuthor{suffix2}", 2026, [])
        extra_ids.append(nt["bookId"])
        got_nt = call_find_by_author(adapter, prx, f"NoTagsAuthor{suffix2}", 0)
        check(
            "AddBook with no tags -> empty tags preserved end-to-end",
            len(got_nt) == 1 and list(got_nt[0]["tags"]) == [],
        )

        long_title_pl = f"Pan Tadeusz czyli ostatni zajazd na Litwie - {suffix2}"
        polish_author = f"Mickiewicz{suffix2}"
        pb = call_add_book(prx, long_title_pl, polish_author, 1834, ["polski", "epopeja"])
        extra_ids.append(pb["bookId"])
        got_pl = call_find_by_author(adapter, prx, polish_author, 0)
        check(
            "long-title round-trip with multiple tags",
            len(got_pl) == 1 and got_pl[0]["title"] == long_title_pl and list(got_pl[0]["tags"]) == ["polski", "epopeja"],
        )

        long_title = "X" * 5000 + suffix2
        lb = call_add_book(prx, long_title, f"LongAuthor{suffix2}", 2026, [])
        extra_ids.append(lb["bookId"])
        got_long = call_find_by_author(adapter, prx, f"LongAuthor{suffix2}", 0)
        check("Long (5KB) title round-trip intact", len(got_long) == 1 and got_long[0]["title"] == long_title)

        ci_author = f"MixedCase{suffix2}"
        cb = call_add_book(prx, f"CI{suffix2}", ci_author, 2026, [])
        extra_ids.append(cb["bookId"])
        got_lower = call_find_by_author(adapter, prx, ci_author.lower(), 0)
        got_upper = call_find_by_author(adapter, prx, ci_author.upper(), 0)
        check("FindByAuthor case-insensitive (lower)", len(got_lower) == 1 and got_lower[0]["id"] == cb["bookId"])
        check("FindByAuthor case-insensitive (upper)", len(got_upper) == 1 and got_upper[0]["id"] == cb["bookId"])

        got_sub = call_find_by_author(adapter, prx, f"ixedCase{suffix2}", 0)
        check("FindByAuthor matches substring (not just prefix)", any(b["id"] == cb["bookId"] for b in got_sub))

        bulk_author = f"Bulk{suffix2}"
        bulk_ids = []
        N = 50
        for i in range(N):
            r = call_add_book(prx, f"Bulk-{i:03d}{suffix2}", bulk_author, 2000 + i, [])
            bulk_ids.append(r["bookId"])
        extra_ids.extend(bulk_ids)
        got_bulk = call_find_by_author(adapter, prx, bulk_author, 0)
        check(f"Large stream returns all {N} books", len(got_bulk) == N)
        got_bulk_lim = call_find_by_author(adapter, prx, bulk_author, 10)
        check("Large stream limit=10 returns 10", len(got_bulk_lim) == 10)
        got_bulk_neg = call_find_by_author(adapter, prx, bulk_author, -5)
        check("Large stream limit=-5 returns all (no-limit semantics)", len(got_bulk_neg) == N)

        high_year = call_add_book(prx, f"Y3000{suffix2}", f"FutureAuthor{suffix2}", 3000, [])
        extra_ids.append(high_year["bookId"])
        got_future = call_find_by_author(adapter, prx, f"FutureAuthor{suffix2}", 0)
        check("AddBook large year=3000 accepted and preserved", len(got_future) == 1 and got_future[0]["year"] == 3000)

        stats_before = call_summary(prx)
        expected_authors = {
            f"ZeroAuthor{suffix2}",
            f"NoTagsAuthor{suffix2}",
            polish_author,
            f"LongAuthor{suffix2}",
            ci_author,
            bulk_author,
            f"FutureAuthor{suffix2}",
        }
        check(
            "Summary byAuthor contains all extended authors",
            expected_authors.issubset(stats_before["byAuthor"].keys()),
            f"missing: {expected_authors - set(stats_before['byAuthor'].keys())}",
        )
        check(
            f"Summary byAuthor count for bulk author == {N}",
            stats_before["byAuthor"].get(bulk_author) == N,
        )
        check("Summary recent capped at 5 entries", len(stats_before["recent"]) == 5, f"len={len(stats_before['recent'])}")

        for bid in extra_ids:
            call_remove_book(prx, bid)
        stats_after = call_summary(prx)
        check(
            "After cleanup none of our extended authors remain",
            not (expected_authors & set(stats_after["byAuthor"].keys())),
            f"leaked: {expected_authors & set(stats_after['byAuthor'].keys())}",
        )

        adapter.deactivate()
    finally:
        communicator.destroy()

    print("")
    print("=" * 40)
    print(f"results: {PASS} PASS, {FAIL} FAIL")
    if FAILS:
        for n in FAILS:
            print(f"  FAIL: {n}")
        sys.exit(1)


if __name__ == "__main__":
    run()
