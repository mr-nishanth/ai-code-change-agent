from ai_code_change_agent.integrations.jira.pagination import Page, collect_pages


def _fetcher(total: int | None, server_cap: int = 1000, report_total: bool = True):  # type: ignore[no-untyped-def]
    calls: list[tuple[int, int]] = []
    data = list(range(total if total is not None else 25))

    async def fetch(start: int, size: int) -> Page[int]:
        calls.append((start, size))
        chunk = data[start : start + min(size, server_cap)]
        return Page(items=chunk, total=len(data) if report_total else None)

    return fetch, calls


async def test_collects_all_pages() -> None:
    fetch, calls = _fetcher(25)
    items, total, truncated = await collect_pages(fetch, page_size=10, max_items=100)
    assert items == list(range(25))
    assert (total, truncated) == (25, False)
    assert calls == [(0, 10), (10, 10), (20, 10)]


async def test_truncates_at_max_items() -> None:
    fetch, calls = _fetcher(25)
    items, total, truncated = await collect_pages(fetch, page_size=10, max_items=15)
    assert items == list(range(15))
    assert (total, truncated) == (25, True)
    assert calls == [(0, 10), (10, 5)]


async def test_exact_boundary_is_not_truncated() -> None:
    fetch, _ = _fetcher(20)
    items, _, truncated = await collect_pages(fetch, page_size=10, max_items=20)
    assert len(items) == 20
    assert truncated is False


async def test_server_returns_fewer_than_requested() -> None:
    fetch, calls = _fetcher(25, server_cap=4)
    items, _, truncated = await collect_pages(fetch, page_size=10, max_items=100)
    assert items == list(range(25))
    assert truncated is False
    assert calls[1][0] == 4


async def test_empty_first_page() -> None:
    async def fetch(_: int, __: int) -> Page[int]:
        return Page(items=[], total=0)

    assert await collect_pages(fetch, page_size=10, max_items=10) == ([], 0, False)


async def test_unknown_total_stops_on_empty_page() -> None:
    fetch, _ = _fetcher(25, report_total=False)
    items, total, truncated = await collect_pages(fetch, page_size=10, max_items=100)
    assert len(items) == 25
    assert (total, truncated) == (None, False)


async def test_unknown_total_hitting_cap_is_flagged_truncated() -> None:
    fetch, _ = _fetcher(25, report_total=False)
    items, _, truncated = await collect_pages(fetch, page_size=10, max_items=20)
    assert len(items) == 20
    assert truncated is True


async def test_no_progress_cannot_loop_forever() -> None:
    calls = 0

    async def fetch(_: int, __: int) -> Page[int]:
        nonlocal calls
        calls += 1
        return Page(items=[] if calls > 1 else [1], total=999)

    items, _, _ = await collect_pages(fetch, page_size=10, max_items=100)
    assert items == [1]
    assert calls == 2
