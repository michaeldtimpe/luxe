"""String utilities: slugify, truncate, title-case helpers."""

import re

_WHITESPACE = re.compile(r"\s+")
_NON_SLUG = re.compile(r"[^a-z0-9-]+")
_DASH_RUN = re.compile(r"-{2,}")


def slugify(text: str) -> str:
    """Convert text to a URL-safe slug.

    Lowercase, replace whitespace with hyphens, drop characters outside
    [a-z0-9-], collapse repeated hyphens.
    """
    s = text.lower()
    s = _WHITESPACE.sub("-", s)
    s = _NON_SLUG.sub("", s)
    s = _DASH_RUN.sub("-", s)
    return s


def truncate(text: str, max_len: int, suffix: str = "…") -> str:
    """Return text shortened to at most max_len characters, with suffix
    appended when truncation happens."""
    if len(text) <= max_len:
        return text
    if max_len <= len(suffix):
        return suffix[:max_len]
    return text[: max_len - len(suffix)] + suffix


def title_case(text: str) -> str:
    """Title-case a sentence, preserving small words that shouldn't
    be capitalised except at the start."""
    small = {"a", "an", "the", "and", "or", "but", "of", "in", "on", "to", "for"}
    words = text.split()
    out: list[str] = []
    for i, w in enumerate(words):
        if i > 0 and w.lower() in small:
            out.append(w.lower())
        else:
            out.append(w.capitalize())
    return " ".join(out)
