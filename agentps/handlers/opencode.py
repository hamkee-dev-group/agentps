"""opencode handler.

Layout:  <config_dir>/opencode.db   (SQLite — drizzle schema, WAL mode)
         no per-session files; everything lives in the DB.

The default config_dir is ~/.local/share/opencode. Opencode keeps the DB in
WAL mode and other opencode processes may be writing to it while agentps
runs, so we always open with mode=ro. We never write to the DB ourselves —
delete is delegated to `opencode session delete`, which serializes through
opencode's own locking.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Iterator

from ..core import AgentInstance, Handler, HOME, register


_DB_FILE = "opencode.db"
_DB_TIMEOUT_MS = 5000

# Launch-only flags worth replaying when a live process is running. These
# aren't persisted in the session row, so cold resumes can't replay them.
_BOOL_FLAGS = {"--dangerously-skip-permissions", "--pure", "--print-logs"}


def _connect_ro(db: Path) -> sqlite3.Connection | None:
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error as e:
        print(f"agentps: opencode db open failed ({e})", file=sys.stderr)
        return None
    try:
        con.execute(f"PRAGMA busy_timeout = {_DB_TIMEOUT_MS};")
    except sqlite3.Error:
        pass
    return con


def _parse_model(model_json: str | None) -> str | None:
    """session.model is a JSON blob like {"providerID":"x","id":"y","variant":"z"}.
    Return "provider/id" or None. Variant is dropped — the top-level CLI only
    exposes --model, and variant is already in the persisted row that opencode
    re-reads on resume."""
    if not model_json:
        return None
    try:
        obj = json.loads(model_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    pid = obj.get("providerID")
    mid = obj.get("id")
    if isinstance(pid, str) and isinstance(mid, str):
        return f"{pid}/{mid}"
    return None


def _preserve_live_flags(live_argv: list[str] | None) -> list[str]:
    if not live_argv:
        return []
    return [a for a in live_argv if a in _BOOL_FLAGS]


def _synth_path(config_dir: Path, sid: str) -> str:
    """Synthetic per-session id. opencode has no per-session file, so we mint
    one to serve as a stable key for dedup and display. The path does not
    exist on disk."""
    return str(config_dir / "sessions" / sid)


class OpenCodeHandler(Handler):
    name = "opencode"

    def default_dirs(self, base: Path | None = None):
        config_dir = base or (HOME / ".local" / "share" / "opencode")
        return config_dir, config_dir

    def find_sessions(self, instance: AgentInstance) -> Iterator[dict]:
        db = instance.config_dir / _DB_FILE
        con = _connect_ro(db)
        if con is None:
            return
        try:
            rows = con.execute(
                "SELECT id, directory, title, time_updated "
                "FROM session WHERE time_archived IS NULL"
            ).fetchall()
        except sqlite3.Error as e:
            print(f"agentps: opencode db read failed ({e})", file=sys.stderr)
            return
        finally:
            con.close()
        for sid, directory, title, t_ms in rows:
            try:
                last_used = (t_ms or 0) / 1000.0
            except (TypeError, ValueError):
                last_used = 0.0
            yield {
                # session_id() (in actions.py) recovers the sid from this
                # field. Other handlers embed the sid in a filename whose hex
                # pattern session_id() recognizes; opencode sids ("ses_…") do
                # not match those regexes, so we pass the bare sid through and
                # let session_id() fall through to returning it verbatim.
                "session": sid,
                "session_path": _synth_path(instance.config_dir, sid),
                "cwd": directory or "?",
                "last_used": last_used,
            }

    def session_for_pid(self, instance, cwd, started):
        if not cwd:
            return None
        db = instance.config_dir / _DB_FILE
        con = _connect_ro(db)
        if con is None:
            return None
        try:
            row = con.execute(
                "SELECT id FROM session "
                "WHERE directory = ? AND time_archived IS NULL "
                "ORDER BY time_updated DESC LIMIT 1",
                (cwd,),
            ).fetchone()
        except sqlite3.Error:
            return None
        finally:
            con.close()
        if not row:
            return None
        return Path(_synth_path(instance.config_dir, row[0]))

    def resume_argv(self, instance, sid, session_path, live_argv=None):
        argv = ["opencode", "--session", sid]
        db = instance.config_dir / _DB_FILE
        con = _connect_ro(db)
        model_arg: str | None = None
        agent: str | None = None
        if con is not None:
            try:
                row = con.execute(
                    "SELECT model, agent FROM session WHERE id = ?", (sid,)
                ).fetchone()
                if row:
                    model_arg = _parse_model(row[0])
                    if isinstance(row[1], str) and row[1]:
                        agent = row[1]
            except sqlite3.Error:
                pass
            finally:
                con.close()
        if model_arg:
            argv.extend(["--model", model_arg])
        if agent:
            argv.extend(["--agent", agent])
        argv.extend(_preserve_live_flags(live_argv))
        return argv

    def delete_session(self, instance, sid, session_path):
        """Direct DELETE on the session row. Foreign-key cascade handles
        message/part/todo/session_message/session_share rows. We rely on WAL +
        busy_timeout to coexist with a live opencode writer rather than cold-
        starting the opencode binary per row (which is too slow for bulk
        deletes and was prone to hangs)."""
        db = instance.config_dir / _DB_FILE
        if db.exists():
            con = sqlite3.connect(str(db), timeout=_DB_TIMEOUT_MS / 1000)
            try:
                con.execute(f"PRAGMA busy_timeout = {_DB_TIMEOUT_MS};")
                con.execute("PRAGMA foreign_keys = ON;")
                con.execute("DELETE FROM session WHERE id = ?", (sid,))
                con.commit()
            finally:
                con.close()
        # Best-effort: drop the per-session diff blob if opencode left one.
        diff = instance.config_dir / "storage" / "session_diff" / f"{sid}.json"
        try:
            diff.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


HANDLER = OpenCodeHandler()
register(HANDLER)
