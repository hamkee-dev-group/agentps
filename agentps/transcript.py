"""Reading agent transcripts from the end. A transcript grows to tens of
megabytes and every refresh re-reads every session, so a title is looked for
from the last line backwards and never further back than TAIL_LIMIT."""

from __future__ import annotations

import os
from typing import Iterator


CHUNK = 64 * 1024
TAIL_LIMIT = 4 * 1024 * 1024


def tail_lines(path: str, limit: int = TAIL_LIMIT) -> Iterator[bytes]:
    """Non-blank lines of the file, last first, from no further back than
    `limit` bytes before its end. Read a chunk at a time, so a reader that
    stops at the first line it wants never touches the rest of the file."""
    try:
        f = open(path, "rb")
    except OSError:
        return
    with f:
        pos = f.seek(0, os.SEEK_END)
        floor = max(0, pos - limit)
        rest = b""
        while pos > floor:
            start = max(floor, pos - CHUNK)
            f.seek(start)
            lines = (f.read(pos - start) + rest).split(b"\n")
            pos = start
            rest = lines[0]
            for line in reversed(lines[1:]):
                if line.strip():
                    yield line
        # What is left is the first line of the file, or a line the limit
        # cut through, which is not handed out half-read.
        if pos == 0 and rest.strip():
            yield rest
