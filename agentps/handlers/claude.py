"""Claude Code handler.

Layout:  <config_dir>/projects/<encoded-cwd>/<sid>.jsonl
         where <encoded-cwd> is cwd with every non-alphanumeric character
         replaced by `-` (lossy: `/home/u/.cfg` -> `-home-u--cfg`).

The first few lines of each .jsonl carry a top-level `cwd` field; we use that
to disambiguate when the encoded dir is ambiguous.

Live processes: <config_dir>/sessions/<pid>.json is claude's own registry of
running sessions (sessionId, cwd, kind, background job id, procStart). It is
exact, so it is read before any guessing. Background sessions (`claude --bg`,
or agents launched from another session) run under claude's daemon; they are
opened with `claude attach <id>` and stopped with `claude stop <id>`, since
`claude --resume` refuses a session that is still running.

Per-session data beside the transcript, all keyed by the full session id:
<projects>/<enc>/<sid>/ (subagents, tool results), file-history/<sid>,
session-env/<sid>, tasks/<sid>, todos/<sid>*.json, and for a background
session jobs/<id>/.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Iterator

from ..core import AgentInstance, Handler, HOME, register
from ..proc import proc_start_ticks


_HEAD_LINES = 5
_FLAG_HEAD_LINES = 20
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")
_SID_RE = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def _encode_cwd(cwd: str) -> str:
    return _NON_ALNUM.sub("-", cwd)


def _argv_value(argv: list[str], flag: str) -> str | None:
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(flag + "="):
            return a[len(flag) + 1:]
    return None


def _read_recorded_cwd(jsonl_path: Path) -> str | None:
    try:
        with open(jsonl_path, "r", errors="replace") as f:
            for _ in range(_HEAD_LINES):
                line = f.readline()
                if not line:
                    return None
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = obj.get("cwd")
                if cwd:
                    return cwd
    except OSError:
        return None
    return None


def _resume_flags(jsonl_path: str) -> list[str]:
    """Translate persisted permissionMode back into CLI flags."""
    if not jsonl_path:
        return []
    try:
        with open(jsonl_path, "r", errors="replace") as f:
            for _ in range(_FLAG_HEAD_LINES):
                line = f.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                pm = obj.get("permissionMode")
                if not pm:
                    continue
                if pm == "bypassPermissions":
                    return ["--dangerously-skip-permissions"]
                return ["--permission-mode", pm]
    except OSError:
        pass
    return []


class ClaudeHandler(Handler):
    name = "claude"
    # The native installer runs ~/.local/share/claude/versions/<ver>, whose
    # comm is the bare version number; daemon-hosted sessions run it directly.
    detect_substrings = ["claude-code", "/claude/versions/"]

    def default_dirs(self, base: Path | None = None):
        config_dir = base or (HOME / ".claude")
        return config_dir / "projects", config_dir

    def find_sessions(self, instance: AgentInstance) -> Iterator[dict]:
        pdir = instance.sessions_dir
        if not pdir.is_dir():
            return
        for d in pdir.iterdir():
            if not d.is_dir():
                continue
            # Lossy decode of the encoded dir name, used only where a session
            # does not record its own cwd. `/home/u/my-proj` and `/home/u/my/proj`
            # encode identically, so one session's cwd cannot speak for its
            # siblings — each file is read for its own.
            fallback = "/" + d.name.lstrip("-").replace("-", "/")
            for jf in d.glob("*.jsonl"):
                try:
                    m = jf.stat().st_mtime
                except OSError:
                    continue
                yield {
                    "session": jf.name,
                    "session_path": str(jf),
                    "cwd": _read_recorded_cwd(jf) or fallback,
                    "last_used": m,
                }

    def is_agent_process(self, argv):
        # The background daemon and its pre-spawned spare hold no session.
        if len(argv) > 1 and argv[1] == "daemon":
            return False
        return "--bg-spare" not in argv

    def _registry(self, instance, pid) -> dict:
        """claude's entry for this pid, if it is about this very process. The
        file outlives a crash, so the recorded start time must match or a
        reused pid would inherit a dead session."""
        try:
            entry = json.loads(
                (instance.config_dir / "sessions" / f"{pid}.json").read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(entry, dict):
            return {}
        if str(entry.get("procStart")) != str(proc_start_ticks(pid)):
            return {}
        return entry

    def _transcript(self, instance, sid, cwd=None) -> Path | None:
        if not _SID_RE.fullmatch(sid or ""):
            return None
        if cwd:
            p = instance.sessions_dir / _encode_cwd(cwd) / f"{sid}.jsonl"
            if p.exists():
                return p
        if instance.sessions_dir.is_dir():
            found = next(instance.sessions_dir.glob(f"*/{sid}.jsonl"), None)
            if found:
                return found
        # Not written yet (no message sent); still this session, not another.
        if cwd:
            return instance.sessions_dir / _encode_cwd(cwd) / f"{sid}.jsonl"
        return None

    def session_from_process(self, instance, pid, argv):
        """claude's registry first, then `--session-id` on the command line
        (headless runs and the pty host of a background session pass it). Both
        are exact; the freshest-file guess would hand every agent in one
        directory the same session."""
        entry = self._registry(instance, pid)
        if entry.get("sessionId"):
            return self._transcript(instance, entry["sessionId"],
                                    entry.get("cwd"))
        return self._transcript(instance, _argv_value(argv, "--session-id"))

    def live_details(self, instance, pid):
        entry = self._registry(instance, pid)
        out = {}
        if entry.get("kind") == "bg" and entry.get("jobId"):
            out["bg_id"] = str(entry["jobId"])
        if entry.get("name"):
            out["name"] = str(entry["name"])
        return out

    def open_live_argv(self, instance, row):
        if row.get("bg_id"):
            return ["claude", "attach", row["bg_id"]]
        return None

    def stop_argv(self, instance, row):
        # A signal leaves the daemon to discover a dead worker; `claude stop`
        # keeps the conversation resumable and the job record consistent.
        if row.get("bg_id"):
            return ["claude", "stop", row["bg_id"]]
        return None

    def delete_session(self, instance, sid, session_path):
        p = Path(session_path)
        full = p.stem
        p.unlink()
        if not _SID_RE.fullmatch(full):
            return
        cfg = instance.config_dir
        for d in (p.with_suffix(""), cfg / "file-history" / full,
                  cfg / "session-env" / full, cfg / "tasks" / full):
            shutil.rmtree(d, ignore_errors=True)
        for f in (cfg / "todos").glob(f"{full}*.json"):
            f.unlink(missing_ok=True)
        self._delete_job(cfg / "jobs", full)

    def _delete_job(self, jobs: Path, full: str) -> None:
        """The background job record, only when it belongs to this session
        and owns no worktree: a worktree may hold uncommitted work, and
        `claude rm` is the tool that knows when removing one is safe."""
        job = jobs / full[:8]
        try:
            state = json.loads((job / "state.json").read_text())
        except (OSError, ValueError):
            return
        if not isinstance(state, dict) or state.get("sessionId") != full:
            return
        if state.get("worktree") or state.get("bgIsolation") not in (None, "none"):
            return
        shutil.rmtree(job, ignore_errors=True)

    def session_for_pid(self, instance, cwd, started):
        if not cwd:
            return None
        pdir = instance.sessions_dir / _encode_cwd(cwd)
        if not pdir.is_dir():
            # Very long paths are truncated and hashed by claude; match on the
            # cwd each session records instead.
            return super().session_for_pid(instance, cwd, started)
        candidates = []
        for p in pdir.glob("*.jsonl"):
            try:
                candidates.append((p.stat().st_mtime, p))
            except OSError:
                continue
        if not candidates:
            return None
        candidates.sort(reverse=True)
        for _, path in candidates:
            if _read_recorded_cwd(path) == cwd:
                return path
        # No file recorded the live cwd — return the freshest as a best guess.
        return candidates[0][1]

    def resume_argv(self, instance, sid, session_path, live_argv=None):
        return ["claude", "--resume", sid] + _resume_flags(session_path)


HANDLER = ClaudeHandler()
register(HANDLER)
