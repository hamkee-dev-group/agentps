"""Reading agent transcripts from the end. A transcript grows to tens of
megabytes and every refresh re-reads every session, so a title is looked for
from the last line backwards and never further back than TAIL_LIMIT."""

from __future__ import annotations

import os
from typing import Iterator


CHUNK = 64 * 1024
TAIL_LIMIT = 4 * 1024 * 1024
_CACHE_MAX = 20000
_cache: dict[str, tuple[tuple[int, int], dict]] = {}


def file_facts(path: str, read) -> dict:
    """read(path) -> dict, cached until the file's size or mtime changes. A
    caller that polls (a TUI, or a program importing agentps) then reads only
    the transcripts that moved since its last look, not every one each time."""
    try:
        st = os.stat(path)
    except OSError:
        return read(path)
    key = (st.st_size, st.st_mtime_ns)
    hit = _cache.get(path)
    if hit and hit[0] == key:
        return hit[1]
    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    facts = read(path)
    _cache[path] = (key, facts)
    return facts


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
