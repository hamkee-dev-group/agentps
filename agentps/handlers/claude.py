"""Claude Code handler.

Layout:  <config_dir>/projects/<encoded-cwd>/<sid>.jsonl
         where <encoded-cwd> is cwd with every non-alphanumeric character
         replaced by `-` (lossy: `/home/u/.cfg` -> `-home-u--cfg`).

The first few lines of each .jsonl carry a top-level `cwd` field; we use that
to disambiguate when the encoded dir is ambiguous.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterator

from ..core import AgentInstance, Handler, HOME, register


_HEAD_LINES = 5
_FLAG_HEAD_LINES = 20
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")
_SID_RE = re.compile(r"[0-9A-Fa-f-]{8,}")


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

    def session_from_argv(self, instance, argv):
        """`--session-id` names the file exactly. Headless runs, daemon-hosted
        sessions and wrappers pass it, often several to one directory, where
        the freshest-file guess would hand them all the same session."""
        sid = _argv_value(argv, "--session-id")
        if not sid or not _SID_RE.fullmatch(sid):
            return None
        if not instance.sessions_dir.is_dir():
            return None
        return next(instance.sessions_dir.glob(f"*/{sid}.jsonl"), None)

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
