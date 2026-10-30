"""Helpers: splitting long replies into Discord-sized messages."""
from __future__ import annotations

import config

_CLOSE = "\n```"  # appended to a chunk that ends inside a code block


def _wrap(line: str, width: int) -> list[str]:
    """Hard-wrap one over-long line, preferring to break at a space."""
    width = max(width, 20)
    if len(line) <= width:
        return [line]
    pieces: list[str] = []
    while len(line) > width:
        cut = line.rfind(" ", 0, width)
        if cut < width // 2:
            cut = width
        pieces.append(line[:cut])
        line = line[cut:].lstrip(" ") if cut != width else line[cut:]
    pieces.append(line)
    return pieces


def split_message(text: str, limit: int = config.MAX_MESSAGE_LEN) -> list[str]:
    """Split ``text`` into chunks of at most ``limit`` characters.

    Splits on line boundaries where possible. If a split lands inside a
    markdown code block, the chunk is closed with ``` and the next chunk
    reopens the block with the same language tag.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    lines: list[str] = []
    size = 0  # length of "\n".join(lines)
    added = 0  # real lines in this chunk (excluding the reopened fence)
    fence: str | None = None  # opening fence line while inside a code block

    def flush() -> None:
        nonlocal lines, size, added
        if added:
            body = "\n".join(lines)
            if fence:
                body += _CLOSE
            chunks.append(body)
        lines = [fence] if fence else []
        size = len(fence) if fence else 0
        added = 0

    for raw in text.split("\n"):
        is_fence_line = raw.strip().startswith("```")
        room = limit - len(_CLOSE) - 1 - ((len(fence) + 1) if fence else 0)
        for piece in _wrap(raw, room):
            extra = len(piece) + (1 if lines else 0)
            if added and size + extra + len(_CLOSE) > limit:
                flush()
                extra = len(piece) + (1 if lines else 0)
            lines.append(piece)
            size += extra
            added += 1
        if is_fence_line:
            fence = None if fence else raw.strip()

    flush()
    return [c for c in chunks if c.strip()]
