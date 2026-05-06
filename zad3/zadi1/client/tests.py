import sys
import time

import grpc

from main import (
    DEFAULT_TARGET,
    SERVICE_NAME,
    list_services,
    load_pool,
    discover_methods,
    call_unary,
    call_server_stream,
)
from grpc_reflection.v1alpha import reflection_pb2_grpc


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


def expect_rpc_code(name, fn, expected_code):
    try:
        fn()
        check(name, False, f"expected {expected_code}, no error raised")
    except grpc.RpcError as e:
        check(name, e.code() == expected_code, f"got {e.code()} details={e.details()!r}")


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TARGET
    print(f"connecting to {target}")
    channel = grpc.insecure_channel(target)
    refl_stub = reflection_pb2_grpc.ServerReflectionStub(channel)

    services = [s for s in list_services(refl_stub) if not s.startswith("grpc.reflection.")]
    pool = load_pool(refl_stub, services)
    methods = discover_methods(pool, services)

    check("reflection: catalog.Catalog discovered", SERVICE_NAME in services)
    check("reflection: 4 catalog methods", sum(1 for k in methods if k[0] == SERVICE_NAME) == 4)
    expected_methods = {"AddBook", "FindByAuthor", "Summary", "RemoveBook"}
    found_methods = {k[1] for k in methods if k[0] == SERVICE_NAME}
    check("reflection: method names match", found_methods == expected_methods)

    add_m = methods[(SERVICE_NAME, "AddBook")]
    find_m = methods[(SERVICE_NAME, "FindByAuthor")]
    summary_m = methods[(SERVICE_NAME, "Summary")]
    remove_m = methods[(SERVICE_NAME, "RemoveBook")]

    check(
        "reflection: FindByAuthor is server-streaming",
        find_m.server_streaming and not find_m.client_streaming,
    )
    check(
        "reflection: AddBook is unary",
        not add_m.server_streaming and not add_m.client_streaming,
    )
    check(
        "reflection: Summary input is google.protobuf.Empty",
        summary_m.input_desc.full_name == "google.protobuf.Empty",
    )

    suffix = f"-test-{int(time.time() * 1000)}"
    author = f"Test Author{suffix}"

    ids = []
    titles = [f"Alpha{suffix}", f"Beta{suffix}", f"Gamma{suffix}"]
    for t in titles:
        req = add_m.input_class(title=t, author=author, year=2026, tags=["tag1", "tag2"])
        resp = call_unary(channel, add_m, req)
        ids.append(resp.id)
    check(
        "AddBook x3 returned distinct positive ids",
        len(set(ids)) == 3 and all(i > 0 for i in ids),
        f"ids={ids}",
    )

    expect_rpc_code(
        "AddBook duplicate -> ALREADY_EXISTS",
        lambda: call_unary(channel, add_m, add_m.input_class(title=titles[0], author=author, year=2026)),
        grpc.StatusCode.ALREADY_EXISTS,
    )
    expect_rpc_code(
        "AddBook empty title -> INVALID_ARGUMENT",
        lambda: call_unary(channel, add_m, add_m.input_class(title="", author="x", year=1)),
        grpc.StatusCode.INVALID_ARGUMENT,
    )
    expect_rpc_code(
        "AddBook empty author -> INVALID_ARGUMENT",
        lambda: call_unary(channel, add_m, add_m.input_class(title="x", author="", year=1)),
        grpc.StatusCode.INVALID_ARGUMENT,
    )
    expect_rpc_code(
        "AddBook negative year -> INVALID_ARGUMENT",
        lambda: call_unary(channel, add_m, add_m.input_class(title=f"x{suffix}", author=f"y{suffix}", year=-1)),
        grpc.StatusCode.INVALID_ARGUMENT,
    )

    found = list(call_server_stream(channel, find_m, find_m.input_class(author=author, limit=0)))
    check(
        "FindByAuthor returns all 3 (server streaming)",
        len(found) == 3 and {b.title for b in found} == set(titles),
        f"got titles={[b.title for b in found]}",
    )
    check(
        "FindByAuthor returned books carry repeated tags",
        all(list(b.tags) == ["tag1", "tag2"] for b in found),
    )

    found_one = list(call_server_stream(channel, find_m, find_m.input_class(author=author, limit=1)))
    check("FindByAuthor with limit=1 returns 1", len(found_one) == 1)

    found_none = list(call_server_stream(channel, find_m, find_m.input_class(author=f"ZZZ-no-such-author{suffix}", limit=0)))
    check("FindByAuthor with no match returns empty stream", len(found_none) == 0)

    expect_rpc_code(
        "FindByAuthor empty author -> INVALID_ARGUMENT",
        lambda: list(call_server_stream(channel, find_m, find_m.input_class(author="", limit=0))),
        grpc.StatusCode.INVALID_ARGUMENT,
    )

    stats = call_unary(channel, summary_m, summary_m.input_class())
    check(
        "Summary contains test author with count >= 3",
        stats.by_author.get(author, 0) >= 3,
        f"by_author[{author!r}]={stats.by_author.get(author, 0)}",
    )
    check("Summary total >= 3", stats.total >= 3, f"total={stats.total}")
    check("Summary recent is a list (max 5)", len(stats.recent) <= 5, f"len={len(stats.recent)}")

    for i in ids:
        call_unary(channel, remove_m, remove_m.input_class(id=i))

    expect_rpc_code(
        "RemoveBook unknown id -> NOT_FOUND",
        lambda: call_unary(channel, remove_m, remove_m.input_class(id=10_000_000)),
        grpc.StatusCode.NOT_FOUND,
    )

    found_after = list(call_server_stream(channel, find_m, find_m.input_class(author=author, limit=0)))
    check("FindByAuthor after cleanup returns 0", len(found_after) == 0)

    suffix2 = f"{suffix}-ext"
    extra_ids = []

    zb = call_unary(channel, add_m, add_m.input_class(title=f"ZeroYear{suffix2}", author=f"ZeroAuthor{suffix2}", year=0))
    check("AddBook year=0 boundary accepted", zb.id > 0)
    extra_ids.append(zb.id)

    nt = call_unary(channel, add_m, add_m.input_class(title=f"NoTags{suffix2}", author=f"NoTagsAuthor{suffix2}", year=2026))
    extra_ids.append(nt.id)
    got_nt = list(call_server_stream(channel, find_m, find_m.input_class(author=f"NoTagsAuthor{suffix2}", limit=0)))
    check(
        "AddBook with no tags -> empty tags preserved end-to-end",
        len(got_nt) == 1 and list(got_nt[0].tags) == [],
    )

    long_polish_title = f"Pan Tadeusz czyli ostatni zajazd na Litwie - {suffix2}"
    polish_author = f"Mickiewicz{suffix2}"
    pb = call_unary(
        channel, add_m,
        add_m.input_class(title=long_polish_title, author=polish_author, year=1834, tags=["polski", "epopeja"]),
    )
    extra_ids.append(pb.id)
    got_pl = list(call_server_stream(channel, find_m, find_m.input_class(author=polish_author, limit=0)))
    check(
        "long-title round-trip with multiple tags",
        len(got_pl) == 1 and got_pl[0].title == long_polish_title and list(got_pl[0].tags) == ["polski", "epopeja"],
    )

    long_title = "X" * 5000 + suffix2
    lb = call_unary(channel, add_m, add_m.input_class(title=long_title, author=f"LongAuthor{suffix2}", year=2026))
    extra_ids.append(lb.id)
    got_long = list(call_server_stream(channel, find_m, find_m.input_class(author=f"LongAuthor{suffix2}", limit=0)))
    check("Long (5KB) title round-trip intact", len(got_long) == 1 and got_long[0].title == long_title)

    ci_author = f"MixedCase{suffix2}"
    cb = call_unary(channel, add_m, add_m.input_class(title=f"CI{suffix2}", author=ci_author, year=2026))
    extra_ids.append(cb.id)
    got_lower = list(call_server_stream(channel, find_m, find_m.input_class(author=ci_author.lower(), limit=0)))
    got_upper = list(call_server_stream(channel, find_m, find_m.input_class(author=ci_author.upper(), limit=0)))
    check("FindByAuthor case-insensitive (lower)", len(got_lower) == 1 and got_lower[0].id == cb.id)
    check("FindByAuthor case-insensitive (upper)", len(got_upper) == 1 and got_upper[0].id == cb.id)

    got_sub = list(call_server_stream(channel, find_m, find_m.input_class(author=f"ixedCase{suffix2}", limit=0)))
    check("FindByAuthor matches substring (not just prefix)", any(b.id == cb.id for b in got_sub))

    bulk_author = f"Bulk{suffix2}"
    bulk_ids = []
    N = 50
    for i in range(N):
        r = call_unary(
            channel, add_m,
            add_m.input_class(title=f"Bulk-{i:03d}{suffix2}", author=bulk_author, year=2000 + i),
        )
        bulk_ids.append(r.id)
    extra_ids.extend(bulk_ids)
    got_bulk = list(call_server_stream(channel, find_m, find_m.input_class(author=bulk_author, limit=0)))
    check(f"Large stream returns all {N} books", len(got_bulk) == N)
    got_bulk_lim = list(call_server_stream(channel, find_m, find_m.input_class(author=bulk_author, limit=10)))
    check("Large stream limit=10 returns 10", len(got_bulk_lim) == 10)
    got_bulk_neg = list(call_server_stream(channel, find_m, find_m.input_class(author=bulk_author, limit=-5)))
    check("Large stream limit=-5 returns all (no-limit semantics)", len(got_bulk_neg) == N)

    high_year = call_unary(
        channel, add_m,
        add_m.input_class(title=f"Y3000{suffix2}", author=f"FutureAuthor{suffix2}", year=3000),
    )
    extra_ids.append(high_year.id)
    got_future = list(call_server_stream(channel, find_m, find_m.input_class(author=f"FutureAuthor{suffix2}", limit=0)))
    check("AddBook large year=3000 accepted and preserved", len(got_future) == 1 and got_future[0].year == 3000)

    stats_before = call_unary(channel, summary_m, summary_m.input_class())
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
        "Summary by_author contains all extended authors",
        expected_authors.issubset(stats_before.by_author.keys()),
        f"missing: {expected_authors - set(stats_before.by_author.keys())}",
    )
    check(
        f"Summary by_author count for bulk author == {N}",
        stats_before.by_author.get(bulk_author) == N,
    )
    check("Summary recent capped at 5 entries", len(stats_before.recent) == 5, f"len={len(stats_before.recent)}")

    for bid in extra_ids:
        call_unary(channel, remove_m, remove_m.input_class(id=bid))
    stats_after = call_unary(channel, summary_m, summary_m.input_class())
    check(
        "After cleanup none of our extended authors remain",
        not (expected_authors & set(stats_after.by_author.keys())),
        f"leaked: {expected_authors & set(stats_after.by_author.keys())}",
    )

    channel.close()

    print("")
    print("=" * 40)
    print(f"results: {PASS} PASS, {FAIL} FAIL")
    if FAILS:
        for n in FAILS:
            print(f"  FAIL: {n}")
        sys.exit(1)


if __name__ == "__main__":
    main()
