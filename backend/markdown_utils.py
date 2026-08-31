"""Small Markdown normalization helpers shared by extraction pipelines."""

from __future__ import annotations

import re


def escape_approximate_tildes(markdown: str) -> str:
    """Keep numeric approximation markers from becoming Markdown strike-through.

    Marked/GFM treats paired single tildes as strike-through in some contexts.
    Escape only an unescaped tilde immediately before a numeric value, while
    preserving intentional ``~~deleted~~`` syntax and unrelated tildes.
    """
    normalized: list[str] = []
    in_fence = False
    fence_re = re.compile(r"^\s*(```+|~~~+)\s*")
    value_re = re.compile(r"(?<![\\~])~(?=\s*\d)")
    for line in (markdown or "").splitlines(keepends=True):
        fence = fence_re.match(line)
        if fence:
            in_fence = not in_fence
            normalized.append(line)
            continue
        normalized.append(line if in_fence else value_re.sub(r"\\~", line))
    return "".join(normalized)
