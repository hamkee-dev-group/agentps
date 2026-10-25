"""opencode handler.

Layout:  <config_dir>/opencode.db   (SQLite — drizzle schema, WAL mode)
         no per-session files; everything lives in the DB.

The default config_dir is ~/.local/share/opencode, unchanged in OpenCode 2.
Sessions live in table `session` on 1.x and `session_v2` on 2.x. The 2.x
migration copies old rows over in the background and never deletes them, so
`session` goes stale once `session_v2` exists: it is read only for rows not
copied yet. Subagent sessions are rows with parent_id set; like opencode's own
`session list`, only top-level sessions are listed, and deleting a session
takes its children with it.

Reads always open with mode=ro, since opencode processes may be writing.
Deletion goes through `opencode session delete` on 2.x, whose shared service
owns the database while running; on 1.x it is direct SQL (cold-starting the
binary per row is slow for bulk deletes) that mirrors what the command does:
children first, then the row, then the session's event log.

Processes: 2.x runs one shared background service (`opencode serve
--service`, cwd $HOME) that hosts every session; it is not a session.

Sessions have no file on disk, so session_stat is overridden — otherwise the
LAST_USED and USER columns stat a synthetic path and render '?'.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Iterator

from ..core import AgentInstance, Handler, HOME, register
from ..format import fmt_title


_DB_FILE = "opencode.db"
_DB_TIMEOUT_MS = 5000

# Launch-only flags worth replaying when a live process is running. These
# aren't persisted in the session row, so cold resumes can't replay them.
_BOOL_FLAGS = {"--auto", "--pure", "--print-logs", "--mini"}
# 2.x dropped --pure and turned --mini into a subcommand.
_BOOL_FLAGS_V2 = {"--auto", "--print-logs"}
_CLI_TIMEOUT_S = 60
_COLS = "id, parent_id, directory, title, time_updated, time_archived, " \
        "model, agent"
# Subcommands that run as a process named opencode without being a session;
# `serve --service` is 2.x's shared host for every session, cwd $HOME.
_NON_SESSION_SUBCOMMANDS = {
    "serve", "web", "service", "session", "debug", "upgrade", "uninstall",
    "models", "auth", "mcp", "stats", "export", "import", "github", "acp",
    "api", "agent", "completion", "pr", "providers",
}
_VALUE_OPTIONS = {"-m", "--model", "-s", "--session", "--agent", "--prompt",
                  "--log-level", "--port", "--hostname", "--server"}


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


def _tables(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}


def _migration_done(con: sqlite3.Connection) -> bool:
    """Whether 2.x finished copying 1.x sessions into session_v2. Unknown
    counts as done: reading the old table after that would bring back
    sessions that were deleted in 2.x."""
    try:
        row = con.execute(
            "SELECT value FROM kv WHERE key = 'migration.v1-v2'").fetchone()
    except sqlite3.Error:
        return True
    if not row:
        return False
    try:
        return json.loads(row[0]).get("phase") == "completed"
    except (TypeError, ValueError, AttributeError):
        return True


def _source(con: sqlite3.Connection) -> str:
    """FROM-clause for the current sessions, whichever schema is on disk."""
    tables = _tables(con)
    if "session_v2" not in tables:
        return "session"
    if "session" in tables and not _migration_done(con):
        return (f"(SELECT {_COLS} FROM session_v2 UNION ALL "
                f"SELECT {_COLS} FROM session "
                f"WHERE id NOT IN (SELECT id FROM session_v2))")
    return "session_v2"


def _is_v2(db: Path) -> bool:
    con = _connect_ro(db)
    if con is None:
        return False
    try:
        return "session_v2" in _tables(con)
    except sqlite3.Error:
        return False
    finally:
        con.close()


def _subcommand(argv: list[str]) -> str | None:
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in _VALUE_OPTIONS:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a
    return None


def _session_tree(con: sqlite3.Connection, table: str, sid: str) -> list[str]:
    """sid and every descendant. parent_id has no foreign key, so children
    are not removed by any cascade."""
    rows = con.execute(
        f"WITH RECURSIVE t(id) AS (SELECT ? UNION "
        f"SELECT s.id FROM {table} s JOIN t ON s.parent_id = t.id) "
        f"SELECT id FROM t", (sid,)).fetchall()
    return [r[0] for r in rows]


def _delete_via_sql(db: Path, sid: str) -> list[str]:
    """What `opencode session delete` does, for 1.x and as a fallback: the
    session and its children, each with its event log. Rows in message, part,
    todo and the other child tables go by foreign-key cascade. Returns the
    ids removed."""
    con = sqlite3.connect(str(db), timeout=_DB_TIMEOUT_MS / 1000)
    try:
        con.execute(f"PRAGMA busy_timeout = {_DB_TIMEOUT_MS};")
        con.execute("PRAGMA foreign_keys = ON;")
        tables = _tables(con)
        session_tables = [t for t in ("session_v2", "session") if t in tables]
        ids: list[str] = []
        for t in session_tables:
            for i in _session_tree(con, t, sid):
                if i not in ids:
                    ids.append(i)
        for i in ids:
            for t in session_tables:
                con.execute(f"DELETE FROM {t} WHERE id = ?", (i,))
            # event cascades from event_sequence; deleted directly as well in
            # case an older schema lacks the foreign key.
            for t in ("event_sequence", "event"):
                if t in tables:
                    con.execute(f"DELETE FROM {t} WHERE aggregate_id = ?",
                                (i,))
        con.commit()
        return ids
    finally:
        con.close()


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
                f"SELECT id, directory, title, time_updated "
                f"FROM {_source(con)} "
                f"WHERE time_archived IS NULL AND parent_id IS NULL"
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
                "title": fmt_title(title),
            }

    def session_stat(self, instance, session_path):
        """time_updated from the DB; owner is whoever owns the DB file."""
        sid = Path(session_path).name
        db = instance.config_dir / _DB_FILE
        try:
            uid = db.stat().st_uid
        except OSError:
            uid = None
        con = _connect_ro(db)
        if con is None:
            return None, uid
        try:
            row = con.execute(
                f"SELECT time_updated FROM {_source(con)} WHERE id = ?", (sid,)
            ).fetchone()
        except sqlite3.Error:
            return None, uid
        finally:
            con.close()
        if not row or row[0] is None:
            return None, uid
        return row[0] / 1000.0, uid

    def is_agent_process(self, argv):
        return _subcommand(argv) not in _NON_SESSION_SUBCOMMANDS

    def session_from_process(self, instance, pid, argv):
        for flag in ("-s", "--session"):
            if flag in argv:
                i = argv.index(flag)
                if i + 1 < len(argv):
                    return Path(_synth_path(instance.config_dir, argv[i + 1]))
        return None

    def session_for_pid(self, instance, cwd, started):
        if not cwd:
            return None
        db = instance.config_dir / _DB_FILE
        con = _connect_ro(db)
        if con is None:
            return None
        try:
            row = con.execute(
                f"SELECT id FROM {_source(con)} "
                f"WHERE directory = ? AND time_archived IS NULL "
                f"AND parent_id IS NULL "
                f"ORDER BY time_updated DESC LIMIT 1",
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
        db = instance.config_dir / _DB_FILE
        if _is_v2(db):
            # 2.x reads model and agent from the session itself, and its root
            # command no longer takes --model or --agent.
            return ["opencode", "--session", sid,
                    *[a for a in (live_argv or []) if a in _BOOL_FLAGS_V2]]
        argv = ["opencode", "--session", sid]
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
        db = instance.config_dir / _DB_FILE
        if not db.exists():
            return
        if _is_v2(db):
            ids = self._delete_via_cli(instance, sid, db)
        else:
            ids = _delete_via_sql(db, sid)
        # Best-effort: drop per-session diff blobs 1.x may have left.
        for i in ids or [sid]:
            diff = instance.config_dir / "storage" / "session_diff" / f"{i}.json"
            try:
                diff.unlink()
            except OSError:
                pass

    def _delete_via_cli(self, instance, sid, db) -> list[str]:
        """2.x: the shared service owns the session while it runs, so the
        delete goes through opencode itself. SQL only if the binary is gone."""
        env = os.environ.copy()
        env.update(instance.env)
        try:
            res = subprocess.run(["opencode", "session", "delete", sid],
                                 capture_output=True, text=True, env=env,
                                 timeout=_CLI_TIMEOUT_S,
                                 stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            return _delete_via_sql(db, sid)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"opencode session delete {sid} timed out")
        if res.returncode != 0:
            lines = (res.stderr or res.stdout).strip().splitlines()
            raise RuntimeError(lines[-1] if lines else
                               f"opencode session delete exited "
                               f"{res.returncode}")
        return [sid]


HANDLER = OpenCodeHandler()
register(HANDLER)
