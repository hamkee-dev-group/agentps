"""Live-process discovery + on-disk session enumeration. Generic; iterates
over the AgentInstance registry and lets each handler do the work."""

from __future__ import annotations

import os
import pwd
from pathlib import Path

from .core import AgentInstance, all_instances, detect_handler, instance_for_pid
from .format import fmt_age
from .proc import (parent_chain, proc_cmdline, proc_cwd, proc_starttime,
                   proc_status, tmux_panes)


def _user_for_uid(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return "?"


def _where_from_chain(pid: int, panes: dict[int, str]) -> str:
    for ppid, pcomm in parent_chain(pid):
        if ppid in panes:
            return f"tmux:{panes[ppid]}"
        if pcomm.startswith("tmux"):
            return "tmux"
        if pcomm in ("screen", "SCREEN"):
            return "screen"
        if pcomm == "sshd":
            return "ssh"
    return "-"


def discover(registry: list[AgentInstance] | None = None,
             loose: bool = False) -> list[dict]:
    """Walk /proc and return one row per live agent process."""
    registry = registry if registry is not None else all_instances()
    panes = tmux_panes()
    rows: list[dict] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)

        handler = detect_handler(pid, registry, loose=loose)
        if handler is None:
            continue

        cwd = proc_cwd(pid)
        start = proc_starttime(pid)
        st = proc_status(pid)
        try:
            uid = int(st.get("Uid", "0").split()[0])
        except ValueError:
            uid = 0
        user = _user_for_uid(uid)

        instance, sess = instance_for_pid(handler, registry, cwd, start)
        agent_name = instance.name if instance else handler.name
        last_used = None
        if sess:
            try:
                last_used = sess.stat().st_mtime
            except OSError:
                pass

        rows.append({
            "agent": agent_name,
            "pid": pid,
            "user": user,
            "cwd": cwd or "?",
            "start": start.isoformat() if start else None,
            "age": fmt_age(start),
            "session": sess.name if sess else None,
            "session_path": str(sess) if sess else None,
            "last_used": last_used,
            "where": _where_from_chain(pid, panes),
            "live_argv": proc_cmdline(pid) or None,
        })
    rows.sort(key=lambda a: (a["agent"], a["pid"]))
    return rows


def enumerate_sessions(registry: list[AgentInstance] | None = None
                       ) -> list[dict]:
    """Every session artifact on disk, regardless of whether a process is
    alive."""
    registry = registry if registry is not None else all_instances()
    out: list[dict] = []
    for inst in registry:
        for s in inst.handler.find_sessions(inst):
            out.append({
                "agent": inst.name,
                **s,
            })
    return out


def _cwd_missing(cwd: str | None) -> bool:
    return not (cwd and cwd != "?" and os.path.isdir(cwd))


def discover_all(registry: list[AgentInstance] | None = None,
                 loose: bool = False) -> list[dict]:
    """Live processes + on-disk sessions, deduped by session_path. Live wins."""
    registry = registry if registry is not None else all_instances()
    live = discover(registry, loose=loose)
    live_paths = {a["session_path"] for a in live if a.get("session_path")}

    rows = list(live)
    for s in enumerate_sessions(registry):
        if s["session_path"] in live_paths:
            continue
        try:
            uid = os.stat(s["session_path"]).st_uid
            user = _user_for_uid(uid)
        except OSError:
            user = "?"
        rows.append({
            "agent": s["agent"],
            "pid": None,
            "user": user,
            "cwd": s["cwd"],
            "start": None,
            "age": "-",
            "session": s["session"],
            "session_path": s["session_path"],
            "last_used": s["last_used"],
            "where": "-",
        })

    for r in rows:
        r["cwd_missing"] = _cwd_missing(r.get("cwd"))

    rows.sort(key=lambda r: (r["cwd_missing"], -(r.get("last_used") or 0)))
    return rows
