"""OpenAI Codex CLI handler.

Layout:  <config_dir>/sessions/YYYY/MM/DD/rollout-<ts>-<sid>.jsonl
         <config_dir>/state_5.sqlite  (threads table — used to replay flags)

Resuming codex needs the original launch flags, which live in the SQLite DB.
We read them on resume and translate back into argv.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Iterator

from ..core import AgentInstance, Handler, HOME, register


_DB_TIMEOUT_MS = 5000
_DB_FILE = "state_5.sqlite"
_PID_SCAN_LIMIT = 200  # max rollouts to inspect when correlating a live PID


def _read_first_meta(rollout: Path) -> dict:
    try:
        with open(rollout, "r", errors="replace") as f:
            head = f.readline()
        if not head:
            return {}
        return json.loads(head)
    except (OSError, json.JSONDecodeError):
        return {}


def _meta_cwd(meta: dict) -> str | None:
    payload = meta.get("payload") or meta
    return payload.get("cwd") or payload.get("working_directory")


def _resume_flags(db: Path, sid: str) -> list[str]:
    if not db.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            con.execute(f"PRAGMA busy_timeout = {_DB_TIMEOUT_MS};")
            row = con.execute(
                "SELECT model, reasoning_effort, approval_mode, sandbox_policy "
                "FROM threads WHERE id = ?",
                (sid,),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error as e:
        print(f"agentps: codex db read failed ({e}); resume flags not replayed",
              file=sys.stderr)
        return []
    if not row:
        return []
    model, effort, approval, sandbox = row
    flags: list[str] = []
    if model:
        flags.extend(["--model", model])
    if approval:
        flags.extend(["--ask-for-approval", approval])
    sandbox_kind = _sandbox_kind(sandbox)
    if sandbox_kind:
        flags.extend(["--sandbox", sandbox_kind])
    if effort:
        flags.extend(["-c", f"reasoning_effort={effort}"])
    return flags


def _sandbox_kind(value: str | None) -> str | None:
    """sandbox_policy is stored as a JSON object like
    {"type":"workspace-write", ...}; the CLI only accepts the bare kind
    (read-only / workspace-write / danger-full-access). Extra fields like
    writable_roots aren't expressible as CLI args, so we drop them — the user
    can re-add them via config.toml or -c if needed."""
    if not value:
        return None
    s = value.strip()
    if s.startswith("{"):
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            return None
        kind = obj.get("type") or obj.get("mode")
        return kind if isinstance(kind, str) else None
    return s


class CodexHandler(Handler):
    name = "codex"

    def default_dirs(self, base: Path | None = None):
        config_dir = base or (HOME / ".codex")
        return config_dir / "sessions", config_dir

    def find_sessions(self, instance: AgentInstance) -> Iterator[dict]:
        sd = instance.sessions_dir
        if not sd.is_dir():
            return
        for p in sd.rglob("rollout-*.jsonl"):
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            cwd = _meta_cwd(_read_first_meta(p)) or "?"
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
        candidates = []
        for p in sd.rglob("rollout-*.jsonl"):
            try:
                candidates.append((p.stat().st_mtime, p))
            except OSError:
                continue
        candidates.sort(reverse=True)
        for _, p in candidates[:_PID_SCAN_LIMIT]:
            if _meta_cwd(_read_first_meta(p)) == cwd:
                return p
        return None

    def resume_argv(self, instance, sid, session_path, live_argv=None):
        flags = _resume_flags(instance.config_dir / _DB_FILE, sid)
        return ["codex", "resume", *flags, sid]

    def delete_session(self, instance, sid, session_path):
        """Drop the threads row first, then the file. If the DB delete fails
        we leave the file rather than orphan a referenced row."""
        db = instance.config_dir / _DB_FILE
        if db.exists():
            con = sqlite3.connect(str(db))
            try:
                con.execute(f"PRAGMA busy_timeout = {_DB_TIMEOUT_MS};")
                con.execute("PRAGMA foreign_keys = ON;")
                con.execute(
                    "DELETE FROM threads WHERE id = ? OR rollout_path = ?",
                    (sid, str(session_path)),
                )
                con.commit()
            finally:
                con.close()
        Path(session_path).unlink()


HANDLER = CodexHandler()
register(HANDLER)
