"""Bounded offset pagination (Jira `startAt`/`maxResults`)."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Page[T]:
    items: list[T]
    total: int | None


async def collect_pages[T](
    fetch: Callable[[int, int], Awaitable[Page[T]]], *, page_size: int, max_items: int
) -> tuple[list[T], int | None, bool]:
    """Collect pages until the reported total, an empty page, or `max_items`.

    Returns (items, total, truncated). Advances by what the server actually returned, stops on
    no progress, and never requests more than `max_items` in total.
    """
    items: list[T] = []
    total: int | None = None
    start = 0
    while True:
        remaining = max_items - len(items)
        if remaining <= 0:
            return items, total, total is None or len(items) < total
        page = await fetch(start, min(page_size, remaining))
        if not page.items:
            if page.total is not None:
                total = page.total
            return items, total, False
        items.extend(page.items[:remaining])
        start += len(page.items)
        total = page.total
        if total is not None and start >= total:
            return items, total, False
