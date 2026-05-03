"""Resume, delete, copy-to-clipboard. All registry-driven."""

from __future__ import annotations

import base64
import os
import re
import shlex
import sys
from pathlib import Path

from .core import AgentInstance, all_instances
from .discovery import _cwd_missing, discover, discover_all, enumerate_sessions


# ---------------------------------------------------------------------------
# Session-id helper
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)
_SHORT_HEX_RE = re.compile(r"-([0-9a-f]{8,})\.[a-z]+$", re.I)


def session_id(name: str | None) -> str | None:
    if not name:
        return None
    m = _UUID_RE.search(name)
    if m:
        return m.group(0)
    m = _SHORT_HEX_RE.search(name)
    if m:
        return m.group(1)
    return name


# ---------------------------------------------------------------------------
# Registry lookup helpers
# ---------------------------------------------------------------------------

def _instance_by_name(registry: list[AgentInstance],
                      name: str) -> AgentInstance | None:
    for inst in registry:
        if inst.name == name:
            return inst
    return None


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------

def resume_argv(instance: AgentInstance, sid: str,
                session_path: str = "",
                live_argv: list[str] | None = None) -> list[str]:
    return instance.handler.resume_argv(instance, sid, session_path,
                                        live_argv=live_argv)


def resume_command_str(instance: AgentInstance, sid: str, session_path: str,
                       cwd: str, live_argv: list[str] | None = None) -> str:
    """Single shell line: env prefix + cd + the resume invocation."""
    argv = resume_argv(instance, sid, session_path, live_argv=live_argv)
    cmd = " ".join(shlex.quote(a) for a in argv)
    if instance.env:
        env_str = " ".join(f"{k}={shlex.quote(v)}"
                           for k, v in instance.env.items())
        cmd = f"{env_str} {cmd}"
    return f"cd {shlex.quote(cwd)} && {cmd}"


def copy_to_clipboard(text: str) -> str:
    """OSC52 copy. Wraps in tmux passthrough when running inside tmux. Returns
    a status string for the TUI footer."""
    if not sys.stdout.isatty() and not sys.stderr.isatty():
        return "no tty for clipboard"
    payload = base64.b64encode(text.encode()).decode()
    seq = f"\x1b]52;c;{payload}\x07"
    if os.environ.get("TMUX"):
        seq = f"\x1bPtmux;\x1b{seq}\x1b\\"
    try:
        with open("/dev/tty", "w") as tty:
            tty.write(seq)
            tty.flush()
        return f"copied {len(text)} chars to clipboard"
    except OSError as e:
        return f"copy failed: {e}"


def resume(prefix: str, print_only: bool = False) -> int:
    registry = all_instances()
    # Use discover_all so live rows carry their cmdline (live_argv), which
    # gemini needs to replay -y / --approval-mode etc. Live rows sort first
    # because session_path collisions are deduped by discover_all already.
    sessions = discover_all(registry)
    matches = []
    for s in sessions:
        sid = session_id(s.get("session"))
        if sid and sid.startswith(prefix):
            matches.append((sid, s))
    if not matches:
        print(f"no session matching {prefix!r}", file=sys.stderr)
        return 1
    if len({sid for sid, _ in matches}) > 1:
        print(f"prefix {prefix!r} is ambiguous:", file=sys.stderr)
        for sid, s in matches:
            print(f"  {sid}  {s['agent']:10}  {s['cwd']}", file=sys.stderr)
        return 1
    sid, s = matches[0]
    instance = _instance_by_name(registry, s["agent"])
    if instance is None:
        print(f"no handler for agent {s['agent']!r}", file=sys.stderr)
        return 1

    cwd = s["cwd"] if s["cwd"] != "?" else "."
    session_path = s.get("session_path") or ""
    live_argv = s.get("live_argv")
    argv = resume_argv(instance, sid, session_path, live_argv=live_argv)

    if print_only:
        print(f"({resume_command_str(instance, sid, session_path, cwd, live_argv=live_argv)})")
        return 0

    try:
        os.chdir(cwd)
    except OSError as e:
        print(f"cannot cd to {cwd}: {e}", file=sys.stderr)
        return 1
    env = os.environ.copy()
    env.update(instance.env)
    try:
        os.execvpe(argv[0], argv, env)
    except FileNotFoundError:
        print(f"command not found: {argv[0]}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"failed to launch {argv[0]}: {e}", file=sys.stderr)
        return 1
    return 0  # unreachable on success


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

def _resolve_dupes(registry: list[AgentInstance]):
    """Sessions whose id appears in multiple instances. Returns the *losers*."""
    groups: dict[str, list[dict]] = {}
    for s in enumerate_sessions(registry):
        sid = session_id(s["session"])
        if not sid:
            continue
        groups.setdefault(sid, []).append(s)

    def score(s):
        try:
            size = os.path.getsize(s["session_path"])
        except OSError:
            size = 0
        mtime = s.get("last_used") or 0
        # Negate the api flag so vanilla (False) sorts higher than -api (True).
        is_api = 1 if s["agent"].endswith("-api") else 0
        return (size, mtime, -is_api)

    losers = []
    for sid, sessions in groups.items():
        if len(sessions) < 2:
            continue
        ranked = sorted(sessions, key=score, reverse=True)
        for s in ranked[1:]:
            losers.append((sid, s))
    return losers


def _resolve_prefixes(args, registry):
    """Resolve each arg to one or more sessions. Arg with `/` is a path; else a
    UUID prefix. Returns (targets, error_msg). Targets deduped by
    session_path."""
    sessions = enumerate_sessions(registry)
    out: dict[str, tuple[str, dict]] = {}
    for arg in args:
        if "/" in arg:
            base = arg.rstrip("/")
            if not base:
                return [], (
                    "refusing path '/' — that would target every session. "
                    "Use --orphans/--dupes or a more specific path."
                )
            matched = False
            for s in sessions:
                cwd = s["cwd"]
                if cwd == base or cwd.startswith(base + "/"):
                    sid = session_id(s["session"])
                    if sid:
                        out[s["session_path"]] = (sid, s)
                        matched = True
            if not matched:
                return [], f"no sessions under path {arg!r}"
            continue

        matches = []
        for s in sessions:
            sid = session_id(s["session"])
            if sid and sid.startswith(arg):
                matches.append((sid, s))
        unique = {sid for sid, _ in matches}
        if not unique:
            return [], f"no session matching {arg!r}"
        if len(unique) > 1:
            lines = [f"prefix {arg!r} is ambiguous:"]
            for sid, s in matches:
                lines.append(f"  {sid}  {s['agent']:10}  {s['cwd']}")
            return [], "\n".join(lines)
        sid, s = matches[0]
        out[s["session_path"]] = (sid, s)
    return list(out.values()), None


def perform_delete(targets, registry):
    """Run handler.delete_session for each target. Returns (removed, errors)."""
    removed = 0
    errors = []
    for sid, s in targets:
        instance = _instance_by_name(registry, s["agent"])
        if instance is None:
            errors.append(f"no handler for agent {s['agent']!r}; skipping {sid}")
            continue
        try:
            instance.handler.delete_session(instance, sid, s["session_path"])
            removed += 1
        except Exception as e:                       # noqa: BLE001
            errors.append(f"failed to remove {s['session_path']}: {e}")
    return removed, errors


def delete(prefixes, force: bool = False, orphans: bool = False,
           dupes: bool = False) -> int:
    registry = all_instances()
    note = None
    if dupes:
        targets = _resolve_dupes(registry)
        if not targets:
            print("no duplicate sessions found.")
            return 0
        note = "Keeping the largest copy (tiebreak: newer mtime, then vanilla over -api)."
    elif orphans:
        targets = []
        for s in enumerate_sessions(registry):
            if not _cwd_missing(s["cwd"]):
                continue
            sid = session_id(s["session"])
            if sid:
                targets.append((sid, s))
        if not targets:
            print("no sessions with missing cwd.")
            return 0
    else:
        targets, err = _resolve_prefixes(prefixes, registry)
        if err:
            print(err, file=sys.stderr)
            return 1

    live_paths = {a["session_path"] for a in discover(registry)
                  if a.get("session_path")}
    for sid, s in targets:
        if s["session_path"] in live_paths:
            print(f"refusing: {sid} is live (process running) — stop the agent first.",
                  file=sys.stderr)
            return 1

    if note:
        print(note)
    print(f"Will delete {len(targets)} session(s).")
    if not force:
        try:
            ans = input(f"Delete {len(targets)} session(s)? [y/N]: ")
        except (EOFError, KeyboardInterrupt):
            print()
            print("aborted.")
            return 0
        if ans.strip().lower() != "y":
            print("aborted.")
            return 0

    removed, errors = perform_delete(targets, registry)
    for e in errors:
        print(e, file=sys.stderr)
    print(f"removed {removed} session(s).")
    return 0 if not errors else 1
