"""Gemini CLI handler.

Layout:  <config_dir>/tmp/<project>/.project_root              (cwd, one line)
         <config_dir>/tmp/<project>/chats/session-<ts>-<sid>.jsonl

Multiple .jsonl files can carry the same `sessionId` inside (gemini snapshots
the session per-resume). We dedupe by inner sessionId, keeping the freshest
file.

Resume:  gemini -r <index>      (1-based, position within unique sessions
                                 in the project, newest-first)

Yolo / approval-mode aren't persisted in the session file — they're purely
launch flags. If a live process is running we read its cmdline and replay
those flags; for cold sessions we don't.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from ..core import AgentInstance, Handler, HOME, register


def _read_project_root(project_dir: Path) -> str | None:
    f = project_dir / ".project_root"
    try:
        return f.read_text().strip() or None
    except OSError:
        return None


def _read_session_id(jsonl: Path) -> str | None:
    try:
        with open(jsonl, "r", errors="replace") as f:
            head = f.readline()
        if not head:
            return None
        return json.loads(head).get("sessionId")
    except (OSError, json.JSONDecodeError):
        return None


def _project_chats(project_dir: Path) -> list[Path]:
    """All session jsonls under <project>/chats/, mtime-descending."""
    chats = project_dir / "chats"
    if not chats.is_dir():
        return []
    files = list(chats.glob("session-*.jsonl"))
    try:
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        pass
    return files


def _unique_sessions(project_dir: Path) -> list[tuple[str, Path]]:
    """[(sessionId, freshest_file)] for the project, newest-first."""
    seen: dict[str, Path] = {}
    for p in _project_chats(project_dir):
        sid = _read_session_id(p)
        if sid and sid not in seen:
            seen[sid] = p
    return list(seen.items())


# Launch-time flags worth preserving across resume. Boolean flags listed in
# _BOOL_FLAGS; value flags in _VALUE_FLAGS (each takes one positional arg).
_BOOL_FLAGS = {"-y", "--yolo", "--skip-trust", "-s", "--sandbox"}
_VALUE_FLAGS = {"--approval-mode", "--policy", "--admin-policy",
                "--include-directories", "-m", "--model"}


def _preserve_flags(live_argv: list[str]) -> list[str]:
    """Pick the launch flags that should carry over on resume."""
    out: list[str] = []
    i = 0
    while i < len(live_argv):
        tok = live_argv[i]
        if tok in _BOOL_FLAGS:
            out.append(tok)
            i += 1
            continue
        if tok in _VALUE_FLAGS and i + 1 < len(live_argv):
            out.extend([tok, live_argv[i + 1]])
            i += 2
            continue
        i += 1
    return out


class GeminiHandler(Handler):
    name = "gemini"
    detect_substrings = ["gemini-cli"]

    def default_dirs(self, base: Path | None = None):
        config_dir = base or (HOME / ".gemini")
        return config_dir / "tmp", config_dir

    def find_sessions(self, instance: AgentInstance) -> Iterator[dict]:
        sd = instance.sessions_dir
        if not sd.is_dir():
            return
        for project_dir in sd.iterdir():
            if not project_dir.is_dir():
                continue
            cwd = _read_project_root(project_dir) or "?"
            for sid, p in _unique_sessions(project_dir):
                try:
                    m = p.stat().st_mtime
                except OSError:
                    continue
                yield {
                    "session": p.name,
                    "session_path": str(p),
                    "cwd": cwd,
                    "last_used": m,
                }

    def session_for_pid(self, instance, cwd, started):
        if not cwd:
            return None
        sd = instance.sessions_dir
        if not sd.is_dir():
            return None
        for project_dir in sd.iterdir():
            if not project_dir.is_dir():
                continue
            if _read_project_root(project_dir) != cwd:
                continue
            unique = _unique_sessions(project_dir)
            return unique[0][1] if unique else None
        return None

    def resume_argv(self, instance, sid, session_path, live_argv=None):
        argv = ["gemini"]
        if session_path:
            p = Path(session_path)
            unique = _unique_sessions(p.parent.parent)
            target_sid = _read_session_id(p)
            for i, (s, _) in enumerate(unique, start=1):
                if s == target_sid:
                    argv.extend(["-r", str(i)])
                    break
            else:
                argv.extend(["--resume", "latest"])
        else:
            argv.extend(["--resume", "latest"])
        if live_argv:
            argv.extend(_preserve_flags(live_argv))
        return argv


HANDLER = GeminiHandler()
register(HANDLER)
