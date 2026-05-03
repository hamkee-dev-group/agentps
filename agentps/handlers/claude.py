"""Claude Code handler.

Layout:  <config_dir>/projects/<encoded-cwd>/<sid>.jsonl
         where <encoded-cwd> is cwd with `/` replaced by `-` (lossy).

The first few lines of each .jsonl carry a top-level `cwd` field; we use that
to disambiguate when the encoded dir is ambiguous.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from ..core import AgentInstance, Handler, HOME, register


_HEAD_LINES = 5
_FLAG_HEAD_LINES = 20


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
    detect_substrings = ["claude-code"]

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
            jsonls = list(d.glob("*.jsonl"))
            if not jsonls:
                continue
            cwd = None
            try:
                sample = max(jsonls, key=lambda p: p.stat().st_mtime)
                cwd = _read_recorded_cwd(sample)
            except OSError:
                pass
            if not cwd:
                # Fallback: lossy decode of the encoded dir name.
                cwd = "/" + d.name.lstrip("-").replace("-", "/")
            for jf in jsonls:
                try:
                    m = jf.stat().st_mtime
                except OSError:
                    continue
                yield {
                    "session": jf.name,
                    "session_path": str(jf),
                    "cwd": cwd,
                    "last_used": m,
                }

    def session_for_pid(self, instance, cwd, started):
        if not cwd:
            return None
        encoded = cwd.replace("/", "-")
        pdir = instance.sessions_dir / encoded
        if not pdir.is_dir():
            return None
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
