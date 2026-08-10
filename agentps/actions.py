"""Resume, delete, copy-to-clipboard. All registry-driven."""

from __future__ import annotations

import base64
import os
import re
import shlex
import signal
import sys

from .core import AgentInstance, ResumeUnavailable, all_instances
from .discovery import _cwd_missing, discover, discover_all, enumerate_sessions
from .proc import parent_chain


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


def _instance_by_name(registry: list[AgentInstance],
                      name: str) -> AgentInstance | None:
    for inst in registry:
        if inst.name == name:
            return inst
    return None


def resume_argv(instance: AgentInstance, sid: str,
                session_path: str = "",
                live_argv: list[str] | None = None) -> list[str]:
    return instance.handler.resume_argv(instance, sid, session_path,
                                        live_argv=live_argv)


def resume_command_str(instance: AgentInstance, sid: str, session_path: str,
                       cwd: str, live_argv: list[str] | None = None) -> str:
    """Single shell line: env prefix + cd + the resume invocation.

    For handlers that resume by position, the direct command would rot as soon
    as the ordering changes, so the line delegates back to `agentps resume`,
    which re-resolves the position when it actually runs. The argv is still
    built here to fail early if the session is already unreachable."""
    argv = resume_argv(instance, sid, session_path, live_argv=live_argv)
    if instance.handler.resume_by_position:
        return f"agentps resume {shlex.quote(sid)}"
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


def resume(prefix: str, print_only: bool = False,
           registry: list[AgentInstance] | None = None) -> int:
    registry = registry if registry is not None else all_instances()
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
    try:
        argv = resume_argv(instance, sid, session_path, live_argv=live_argv)
        if print_only:
            line = resume_command_str(instance, sid, session_path, cwd,
                                      live_argv=live_argv)
    except ResumeUnavailable as e:
        print(f"agentps: {e}", file=sys.stderr)
        return 1

    if print_only:
        print(f"({line})")
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


def _self_pids() -> set[int]:
    """This process and its ancestors. Killing them would take down the shell
    or the very agent session the command was typed into."""
    pids = {os.getpid()}
    pids.update(ppid for ppid, _ in parent_chain(os.getpid()))
    return pids


def _live_matches(prefixes, registry):
    """Live rows whose session id starts with one of the prefixes, or whose cwd
    is at or under a path argument. Returns (rows, error).

    An id prefix must identify one session: signalling is destructive, so a
    short prefix that happens to match two agents is refused rather than
    applied to both. A path argument is explicitly plural."""
    live = discover(registry)
    out: dict[int, dict] = {}
    for arg in prefixes:
        if "/" in arg:
            base = arg.rstrip("/")
            if not base:
                return [], "refusing path '/' — name a directory"
            hits = [r for r in live
                    if r["cwd"] == base or r["cwd"].startswith(base + "/")]
            if not hits:
                return [], f"no live agent under path {arg!r}"
            out.update({r["pid"]: r for r in hits})
            continue

        hits = [r for r in live
                if (session_id(r.get("session") or "") or "").startswith(arg)]
        if not hits:
            return [], f"no live agent matching {arg!r}"
        distinct = {r.get("session_path") or f"pid:{r['pid']}" for r in hits}
        if len(distinct) > 1:
            lines = [f"prefix {arg!r} is ambiguous:"]
            for r in hits:
                lines.append(f"  {short_id(r)}  {r['agent']:10}  {r['cwd']}")
            return [], "\n".join(lines)
        out.update({r["pid"]: r for r in hits})
    return list(out.values()), None


def kill(prefixes, force: bool = False, yes: bool = False,
         registry: list[AgentInstance] | None = None) -> int:
    """SIGTERM (or SIGKILL with --force) every process behind the matched
    sessions."""
    registry = registry if registry is not None else all_instances()
    rows, err = _live_matches(prefixes, registry)
    if err:
        print(err, file=sys.stderr)
        return 1

    mine = _self_pids()
    targets: list[tuple[dict, list[int]]] = []
    for r in rows:
        pids = [p for p in (r.get("pids") or [r["pid"]]) if p not in mine]
        if not pids:
            print(f"refusing: {short_id(r)} is the session running this "
                  f"command — exit it instead.", file=sys.stderr)
            return 1
        targets.append((r, pids))

    total = sum(len(p) for _, p in targets)
    sig = signal.SIGKILL if force else signal.SIGTERM
    print(f"Will send {sig.name} to {total} process(es) in "
          f"{len(targets)} session(s).")
    for r, pids in targets:
        print(f"  {short_id(r)}  {r['agent']:10}  {r['cwd']}  "
              f"pid {','.join(str(p) for p in pids)}")
    if not yes:
        try:
            if input(f"Send {sig.name}? [y/N]: ").strip().lower() != "y":
                print("aborted.")
                return 0
        except (EOFError, KeyboardInterrupt):
            print()
            print("aborted.")
            return 0

    signalled, gone, errors = signal_pids(
        [p for _, pids in targets for p in pids], sig)
    for e in errors:
        print(e, file=sys.stderr)
    note = f", {gone} had already exited" if gone else ""
    print(f"signalled {signalled} process(es){note}.")
    return 0 if not errors else 1


def short_id(row) -> str:
    return session_id(row.get("session") or "") or f"pid:{row.get('pid')}"


def signal_pids(pids, sig) -> tuple[int, int, list[str]]:
    """(signalled, already_gone, errors). A process that exits between listing
    and signalling is not a failure — the state the caller wanted is the state
    it got — so it is counted separately and never fails the command."""
    sent = 0
    gone = 0
    errors = []
    for pid in pids:
        try:
            os.kill(pid, sig)
            sent += 1
        except ProcessLookupError:
            gone += 1
        except PermissionError:
            errors.append(f"pid {pid}: not permitted")
        except OSError as e:
            errors.append(f"pid {pid}: {e}")
    return sent, gone, errors


def attach(prefix: str, registry: list[AgentInstance] | None = None) -> int:
    """Jump to the tmux pane an agent is running in."""
    registry = registry if registry is not None else all_instances()
    rows, err = _live_matches([prefix], registry)
    if err:
        print(err, file=sys.stderr)
        return 1
    panes = [r for r in rows if r.get("pane")]
    if not panes:
        where = ", ".join(sorted({r.get("where") or "-" for r in rows}))
        print(f"no tmux pane for {prefix!r} (running under: {where})",
              file=sys.stderr)
        return 1
    if len({r["pane"] for r in panes}) > 1:
        print(f"{prefix!r} matches several panes:", file=sys.stderr)
        for r in panes:
            print(f"  {r['pane']}  {r['agent']:10}  {r['cwd']}",
                  file=sys.stderr)
        return 1

    target = panes[0]["pane"]
    inside = bool(os.environ.get("TMUX"))
    argv = ["tmux", "switch-client" if inside else "attach-session",
            "-t", target]
    try:
        os.execvp(argv[0], argv)
    except FileNotFoundError:
        print("tmux not found", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"failed to attach: {e}", file=sys.stderr)
        return 1
    return 0


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
           dupes: bool = False,
           registry: list[AgentInstance] | None = None) -> int:
    registry = registry if registry is not None else all_instances()
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
