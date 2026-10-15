"""Resume, delete, copy-to-clipboard. All registry-driven."""

from __future__ import annotations

import base64
import os
import re
import shlex
import signal
import subprocess
import sys

from .core import AgentInstance, ResumeUnavailable, all_instances
from .discovery import _cwd_missing, discover, discover_all, enumerate_sessions
from .proc import parent_chain, proc_start_ticks


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


def _path_arg(arg: str) -> str | None:
    """The absolute directory an ID-OR-PATH argument names, or None when it is
    a session id prefix. Recorded cwds are absolute, so `./proj`, `..` and
    `~/proj` are resolved first rather than silently matching nothing."""
    if "/" not in arg and arg not in (".", "..") and not arg.startswith("~"):
        return None
    return os.path.abspath(os.path.expanduser(arg))


def _under(cwd: str | None, base: str) -> bool:
    if not cwd:
        return False
    return cwd == base or cwd.startswith(base.rstrip("/") + "/")


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
    return open_row(s, sid, registry, print_only=print_only)


def _env_prefix(instance: AgentInstance) -> str:
    return "".join(f"{k}={shlex.quote(v)} " for k, v in instance.env.items())


def _tmux_argv(pane: str) -> list[str]:
    inside = bool(os.environ.get("TMUX"))
    return ["tmux", "switch-client" if inside else "attach-session",
            "-t", pane]


def live_open_argv(instance: AgentInstance, row: dict) -> list[str] | None:
    """How to reach a session that is running right now: the agent's own
    attach command, else the tmux pane it runs in. None when neither exists."""
    if not row.get("pid"):
        return None
    argv = instance.handler.open_live_argv(instance, row)
    if argv:
        return argv
    if row.get("pane"):
        return _tmux_argv(row["pane"])
    return None


def _exec(argv: list[str], env_extra: dict[str, str],
          cwd: str | None = None) -> int:
    if cwd is not None:
        try:
            os.chdir(cwd)
        except OSError as e:
            print(f"cannot cd to {cwd}: {e}", file=sys.stderr)
            return 1
    env = os.environ.copy()
    env.update(env_extra)
    try:
        os.execvpe(argv[0], argv, env)
    except FileNotFoundError:
        print(f"command not found: {argv[0]}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"failed to launch {argv[0]}: {e}", file=sys.stderr)
        return 1
    return 0  # unreachable on success


def open_row(row: dict, sid: str, registry: list[AgentInstance],
             print_only: bool = False) -> int:
    """Open one session: join it if it is running, else resume it from its
    recorded cwd. A running session is never resumed a second time when there
    is a way to join it — two processes writing one session corrupt it, and
    some agents refuse outright (claude for a background session)."""
    instance = _instance_by_name(registry, row["agent"])
    if instance is None:
        print(f"no handler for agent {row['agent']!r}", file=sys.stderr)
        return 1

    live = live_open_argv(instance, row)
    if live:
        if print_only:
            print(_env_prefix(instance)
                  + " ".join(shlex.quote(a) for a in live))
            return 0
        return _exec(live, instance.env)
    if row.get("pid"):
        print(f"agentps: {sid} is already running (pid {row['pid']}, "
              f"{row.get('where') or '-'}); resuming opens a second process "
              f"on it.", file=sys.stderr)

    cwd = row["cwd"] if row["cwd"] != "?" else "."
    session_path = row.get("session_path") or ""
    live_argv = row.get("live_argv")
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
    return _exec(argv, instance.env, cwd)


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
        base = _path_arg(arg)
        if base is not None:
            if base == "/":
                return [], "refusing path '/' — name a directory"
            hits = [r for r in live if _under(r["cwd"], base)]
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

    sig = signal.SIGKILL if force else signal.SIGTERM
    plan = stop_plan(targets, registry, use_agent=not force)
    print(f"Will stop {len(plan)} session(s).")
    for r, pids, argv in plan:
        how = (" ".join(argv) if argv
               else f"{sig.name} pid {','.join(str(p) for p in pids)}")
        print(f"  {short_id(r)}  {r['agent']:10}  {r['cwd']}  {how}")
    if not yes:
        try:
            if input("Proceed? [y/N]: ").strip().lower() != "y":
                print("aborted.")
                return 0
        except (EOFError, KeyboardInterrupt):
            print()
            print("aborted.")
            return 0

    stopped, signalled, gone, errors = run_stop_plan(plan, sig)
    for e in errors:
        print(e, file=sys.stderr)
    parts = []
    if stopped:
        parts.append(f"stopped {stopped} session(s) via the agent")
    parts.append(f"signalled {signalled} process(es)")
    if gone:
        parts.append(f"{gone} had already exited")
    print(", ".join(parts) + ".")
    return 0 if not errors else 1


def stop_plan(targets, registry, use_agent: bool = True):
    """[(row, pids, argv-or-None)]: the agent's own stop command where it has
    one (and use_agent), else the pids to signal."""
    plan = []
    for r, pids in targets:
        argv = None
        if use_agent:
            inst = _instance_by_name(registry, r["agent"])
            if inst is not None:
                argv = inst.handler.stop_argv(inst, r)
        plan.append((r, pids, argv))
    return plan


_STOP_TIMEOUT_S = 30


def run_stop_plan(plan, sig) -> tuple[int, int, int, list[str]]:
    """(stopped_via_agent, signalled, already_gone, errors)."""
    stopped = 0
    errors: list[str] = []
    signal_rows = []
    for r, pids, argv in plan:
        if not argv:
            signal_rows.append((r, pids))
            continue
        try:
            res = subprocess.run(argv, capture_output=True, text=True,
                                 timeout=_STOP_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as e:
            errors.append(f"{' '.join(argv)}: {e}")
            continue
        if res.returncode == 0:
            stopped += 1
        else:
            msg = (res.stderr or res.stdout).strip().splitlines()
            errors.append(f"{' '.join(argv)}: "
                          f"{msg[-1] if msg else f'exit {res.returncode}'}")
    signalled, gone, sig_errors = signal_pids(
        [p for _, pids in signal_rows for p in pids], sig,
        expected_ticks=row_ticks(r for r, _ in signal_rows))
    return stopped, signalled, gone, errors + sig_errors


def short_id(row) -> str:
    return session_id(row.get("session") or "") or f"pid:{row.get('pid')}"


def row_ticks(rows) -> dict[int, int | None]:
    """{pid: start ticks} recorded when the rows were discovered."""
    out: dict[int, int | None] = {}
    for r in rows:
        out.update(r.get("pid_ticks") or {})
    return out


def signal_pids(pids, sig, expected_ticks: dict | None = None
                ) -> tuple[int, int, list[str]]:
    """(signalled, already_gone, errors). A process that exits between listing
    and signalling is not a failure — the state the caller wanted is the state
    it got — so it is counted separately and never fails the command.

    `expected_ticks` holds each PID's start time as seen at listing. Time passes
    at the confirmation prompt (and between TUI refreshes), long enough for the
    agent to exit and its PID to go to an unrelated process, so a PID whose
    start time no longer matches is treated as gone and left alone."""
    sent = 0
    gone = 0
    errors = []
    for pid in pids:
        want = (expected_ticks or {}).get(pid)
        if want is not None and proc_start_ticks(pid) != want:
            gone += 1
            continue
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
    """Jump to the tmux pane an agent is running in, or join it through the
    agent's own attach command (claude background sessions have no pane)."""
    registry = registry if registry is not None else all_instances()
    rows, err = _live_matches([prefix], registry)
    if err:
        print(err, file=sys.stderr)
        return 1
    panes = [r for r in rows if r.get("pane")]
    if not panes and len(rows) == 1:
        inst = _instance_by_name(registry, rows[0]["agent"])
        argv = inst and inst.handler.open_live_argv(inst, rows[0])
        if argv:
            return _exec(argv, inst.env)
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

    return _exec(_tmux_argv(panes[0]["pane"]), {})


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
    """Resolve each arg to one or more sessions. A path (see _path_arg) or a
    UUID prefix. Returns (targets, error_msg). Targets deduped by
    session_path."""
    sessions = enumerate_sessions(registry)
    out: dict[str, tuple[str, dict]] = {}
    for arg in args:
        base = _path_arg(arg)
        if base is not None:
            if base == "/":
                return [], (
                    "refusing path '/' — that would target every session. "
                    "Use --orphans/--dupes or a more specific path."
                )
            matched = False
            for s in sessions:
                if _under(s["cwd"], base):
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
