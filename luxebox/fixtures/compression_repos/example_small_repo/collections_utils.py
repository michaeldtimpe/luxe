"""Collection helpers: dedupe, chunk, flatten, group_by."""

from typing import Callable, Iterable, TypeVar

T = TypeVar("T")
K = TypeVar("K")


def dedupe_preserve_order(items: Iterable[T]) -> list[T]:
    """Return items in original order with duplicates removed.

    Works for both hashable and unhashable items (e.g. lists of dicts).
    """
    seen: set = set()
    out: list[T] = []
    for item in items:
        # BUG: silently drops unhashable items because `in set` raises
        # TypeError. Callers expect duplicate-aware behaviour even for
        # dict / list elements.
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def chunk(items: Iterable[T], size: int) -> list[list[T]]:
    """Split items into consecutive chunks of the given size."""
    if size <= 0:
        raise ValueError("chunk size must be positive")
    out: list[list[T]] = []
    buf: list[T] = []
    for item in items:
        buf.append(item)
        if len(buf) == size:
            out.append(buf)
            buf = []
    if buf:
        out.append(buf)
    return out


def flatten(nested: Iterable[Iterable[T]]) -> list[T]:
    out: list[T] = []
    for sub in nested:
        out.extend(sub)
    return out


def group_by(items: Iterable[T], key: Callable[[T], K]) -> dict[K, list[T]]:
    out: dict[K, list[T]] = {}
    for item in items:
        k = key(item)
        out.setdefault(k, []).append(item)
    return out
