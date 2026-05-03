"""Linux /proc readers and tmux pane discovery. Handler-agnostic."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path


# Tunables — sane defaults; edit here if a deployment needs different values.
_PARENT_MAX_DEPTH = 12
_TMUX_TIMEOUT_S = 2


# ---------------------------------------------------------------------------
# /proc helpers
# ---------------------------------------------------------------------------

def _read(pid: int, name: str):
    try:
        return (Path(f"/proc/{pid}") / name).read_bytes()
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        return None


def proc_cmdline(pid: int) -> list[str]:
    raw = _read(pid, "cmdline")
    if not raw:
        return []
    return [a for a in raw.decode("utf-8", "replace").split("\x00") if a]


def proc_comm(pid: int) -> str:
    raw = _read(pid, "comm")
    return raw.decode("utf-8", "replace").strip() if raw else ""


def proc_cwd(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except (OSError, PermissionError):
        return None


def proc_exe(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except (OSError, PermissionError):
        return None


def proc_status(pid: int) -> dict:
    raw = _read(pid, "status")
    if not raw:
        return {}
    out = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


_BTIME = None
try:
    _CLK_TCK = os.sysconf("SC_CLK_TCK") or 100
except (OSError, ValueError):
    _CLK_TCK = 100  # POSIX default fallback


def _btime() -> int:
    global _BTIME
    if _BTIME is None:
        _BTIME = 0
        try:
            with open("/proc/stat") as f:
                for line in f:
                    if line.startswith("btime "):
                        _BTIME = int(line.split()[1])
                        break
        except OSError:
            pass
    return _BTIME


def proc_starttime(pid: int):
    raw = _read(pid, "stat")
    if not raw:
        return None
    text = raw.decode("utf-8", "replace")
    rparen = text.rfind(")")
    if rparen == -1:
        return None
    fields = text[rparen + 2:].split()
    # After stripping `pid (comm) `, starttime is field index 19
    try:
        ticks = int(fields[19])
    except (IndexError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(_btime() + ticks / _CLK_TCK)
    except (OverflowError, OSError, ValueError, ZeroDivisionError):
        return None


def parent_chain(pid: int, max_depth: int = _PARENT_MAX_DEPTH):
    chain = []
    cur = pid
    for _ in range(max_depth):
        st = proc_status(cur)
        try:
            ppid = int(st.get("PPid", "0"))
        except ValueError:
            break
        if ppid <= 1:
            break
        chain.append((ppid, proc_comm(ppid)))
        cur = ppid
    return chain


# ---------------------------------------------------------------------------
# tmux pane map
# ---------------------------------------------------------------------------

def tmux_panes() -> dict[int, str]:
    """{pid: 'session:window.pane'} for every tmux pane on the host. Empty if
    tmux isn't installed or running."""
    try:
        r = subprocess.run(
            ["tmux", "list-panes", "-a",
             "-F", "#{pane_pid}\t#{session_name}:#{window_index}.#{pane_index}"],
            capture_output=True, text=True, timeout=_TMUX_TIMEOUT_S,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if r.returncode != 0:
        return {}
    out: dict[int, str] = {}
    for line in r.stdout.splitlines():
        if "\t" not in line:
            continue
        ppid, label = line.split("\t", 1)
        try:
            out[int(ppid)] = label
        except ValueError:
            continue
    return out
