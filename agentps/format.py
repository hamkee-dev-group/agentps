"""Tiny formatting helpers used by both the CLI table and the TUI. No
upstream dependencies — anyone can import this."""

from __future__ import annotations

import re
import sys
from datetime import datetime


def _encoding_supports(s: str) -> bool:
    enc = sys.stdout.encoding or "ascii"
    try:
        s.encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


if _encoding_supports("…─↑↓▼▲"):
    ELLIPSIS = "…"
    BAR = "─"
    ARROWS = "↑↓"
    SORT_DESC = "▼"
    SORT_ASC = "▲"
else:
    ELLIPSIS = "..."
    BAR = "-"
    ARROWS = "Up/Dn"
    SORT_DESC = "v"
    SORT_ASC = "^"


# Overridable by config loader at startup.
DATE_FMT = "%m-%d-%Y"


def fmt_age(start) -> str:
    if not start:
        return "?"
    s = (datetime.now() - start).total_seconds()
    if s < 60:
        return f"{int(s)}s"
    if s < 3600:
        return f"{int(s // 60)}m"
    if s < 86400:
        return f"{int(s // 3600)}h"
    return f"{int(s // 86400)}d"


def fmt_date(ts) -> str:
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts).strftime(DATE_FMT)


def shorten(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return ELLIPSIS + s[-(n - len(ELLIPSIS)):]


_UUID_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)
# Trailing short-hex used by Gemini (`session-…-ba2f65ec.jsonl`).
_SHORT_HEX_RE = re.compile(r"-([0-9a-f]{8,})\.[a-z]+$", re.I)


def short_session(name) -> str:
    if not name:
        return "-"
    m = _UUID_RE.search(name)
    if m:
        return m.group(0)[:8]
    m = _SHORT_HEX_RE.search(name)
    if m:
        return m.group(1)[:8]
    stripped = name[:-6] if name.endswith(".jsonl") else name
    return stripped[:12]
