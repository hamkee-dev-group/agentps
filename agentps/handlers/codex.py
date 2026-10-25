"""OpenAI Codex CLI handler.

Layout:  <config_dir>/sessions/YYYY/MM/DD/rollout-<ts>-<sid>.jsonl
         <config_dir>/state_5.sqlite  (threads table)
         plus thread_history_1, goals_1, queue_1, memories_*, logs_2 databases
         and session_index.jsonl, all keyed by thread id.

Resume: `codex resume <uuid>` restores the thread's model, effort and provider
itself; passing --model or an effort override makes codex fall back to the
current config, provider included, so neither is replayed. Permissions are
restored by codex from 0.160 on. Before that the TUI used the current config's
approval and sandbox, so they are replayed from the rollout's last
turn_context, whose sandbox type uses the CLI's own mode names. The threads
row's sandbox_policy is a permission profile ("managed", "disabled") that
--sandbox rejects.

Delete: `codex delete --force <uuid>`, which takes the thread's locks, refuses
while a fork still references it, and clears every database above. The direct
SQL path is kept only for when the codex binary is not available.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Iterator

from ..core import AgentInstance, Handler, HOME, register
from ..format import fmt_title
from ..transcript import tail_lines


_DB_TIMEOUT_MS = 5000
_DB_FILE = "state_5.sqlite"
_PID_SCAN_LIMIT = 200  # max rollouts to inspect when correlating a live PID
_SID_RE = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_PROMPT_LINE_RE = re.compile(rb'"type":\s*"UserMessage"|"role":\s*"user"')
# Blocks codex itself puts into a user message ahead of what was typed.
_NOT_A_PROMPT = ("<environment_context>", "<user_instructions>",
                 "<permissions instructions>", "<turn_aborted>")


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


def _prompt_text(obj: dict) -> str | None:
    """What the user typed in a rollout line: a completed UserMessage item,
    or on rollouts without those the user message among the response
    items."""
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return None
    if obj.get("type") == "event_msg":
        payload = payload.get("item")
        if not isinstance(payload, dict) or payload.get("type") != "UserMessage":
            return None
    elif payload.get("type") != "message" or payload.get("role") != "user":
        return None
    content = payload.get("content")
    if not isinstance(content, list):
        return None
    texts = [c.get("text") for c in content if isinstance(c, dict)]
    return "\n".join(t for t in texts if isinstance(t, str)
                     and not t.lstrip().startswith(_NOT_A_PROMPT)) or None


def _last_prompt(rollout: str) -> str | None:
    for line in tail_lines(rollout):
        if not _PROMPT_LINE_RE.search(line):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        title = fmt_title(_prompt_text(obj)) if isinstance(obj, dict) else None
        if title:
            return title
    return None


def _thread_titles(instance: AgentInstance) -> dict[str, str]:
    """{thread id: title} from codex's own database: the name the thread was
    given, else its title, which codex takes from the first prompt."""
    db = instance.config_dir / _DB_FILE
    if not db.exists():
        return {}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        con.execute(f"PRAGMA busy_timeout = {_DB_TIMEOUT_MS};")
        rows = con.execute("SELECT id, name, title FROM threads").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        con.close()
    titles = {}
    for tid, name, title in rows:
        text = fmt_title(name) or fmt_title(title)
        if text:
            titles[tid] = text
    return titles


_CLI_TIMEOUT_S = 60
_PERMISSIONS_RESTORED = (0, 160)   # codex restores approval/sandbox itself
_APPROVAL_FLAG_VALUES = {"on-request", "never"}
_SANDBOX_MODES = {"read-only", "workspace-write", "danger-full-access"}
# Process names that resolve to the codex binary but are tools it runs for a
# session (sandbox launcher, patch applier), not sessions.
_HELPER_ARGV0 = {"codex-linux-sandbox", "apply_patch", "applypatch",
                 "codex-execve-wrapper"}
# Subcommands that run as a process named codex without being a session. The
# app-server daemon hosts threads for every client and its cwd is its own.
_NON_SESSION_SUBCOMMANDS = {
    "app-server", "exec-server", "mcp", "mcp-server", "remote-control",
    "agents", "queue", "archive", "unarchive", "delete", "doctor", "update",
    "features", "migrate-rollouts", "debug", "sandbox", "login", "logout",
    "completion", "apply", "cloud",
}

# Global options that take a value, so the value is not read as a subcommand.
_VALUE_OPTIONS = {"-c", "--config", "-m", "--model", "-p", "--profile",
                  "-C", "--cd", "-s", "--sandbox", "-a", "--ask-for-approval",
                  "-i", "--image", "--enable", "--disable", "--add-dir",
                  "--remote", "--local-provider"}


def _subcommand(argv: list[str]) -> str | None:
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--":
            return None
        if a in _VALUE_OPTIONS:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a
    return None


_version_cache: dict[tuple, tuple[int, ...] | None] = {}


def _env(instance: AgentInstance) -> dict[str, str]:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(instance.config_dir)
    env.update(instance.env)
    return env


def _codex_version(instance: AgentInstance) -> tuple[int, ...] | None:
    key = tuple(sorted(instance.env.items()))
    if key not in _version_cache:
        version = None
        try:
            out = subprocess.run(["codex", "--version"], capture_output=True,
                                 text=True, timeout=10,
                                 env=_env(instance)).stdout
            m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
            if m:
                version = tuple(int(x) for x in m.groups())
        except (OSError, subprocess.TimeoutExpired):
            pass
        _version_cache[key] = version
    return _version_cache[key]


def _last_turn_context(rollout: Path) -> dict:
    last: dict = {}
    try:
        with open(rollout, "r", errors="replace") as f:
            for line in f:
                if '"turn_context"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "turn_context":
                    last = obj.get("payload") or {}
    except OSError:
        pass
    return last


def _resume_flags(instance: AgentInstance, session_path: str) -> list[str]:
    """Permission flags for codex releases that do not restore them. An
    unknown version gets none: on a newer codex they would replace a custom
    permission profile with a coarse mode, and dropping them never widens
    what the agent may do."""
    version = _codex_version(instance)
    if version is None or version >= _PERMISSIONS_RESTORED or not session_path:
        return []
    ctx = _last_turn_context(Path(session_path))
    flags: list[str] = []
    approval = ctx.get("approval_policy")
    if approval in _APPROVAL_FLAG_VALUES:
        flags.extend(["--ask-for-approval", approval])
    elif isinstance(approval, str) and approval:
        flags.extend(["-c", f"approval_policy={approval}"])
    sandbox = ctx.get("sandbox_policy")
    kind = sandbox.get("type") if isinstance(sandbox, dict) else sandbox
    if kind in _SANDBOX_MODES:
        flags.extend(["--sandbox", kind])
    return flags


def _delete_via_sql(instance: AgentInstance, sid: str,
                    session_path: str) -> None:
    """Fallback when the codex binary is unavailable: the threads row, then
    the file. Leaves the history/goal/queue databases, which only codex knows
    how to clear consistently."""
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


class CodexHandler(Handler):
    name = "codex"

    def default_dirs(self, base: Path | None = None):
        config_dir = base or (HOME / ".codex")
        return config_dir / "sessions", config_dir

    def find_sessions(self, instance: AgentInstance) -> Iterator[dict]:
        sd = instance.sessions_dir
        if not sd.is_dir():
            return
        titles = _thread_titles(instance)
        for p in sd.rglob("rollout-*.jsonl"):
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            cwd = _meta_cwd(_read_first_meta(p)) or "?"
            sid = _SID_RE.search(p.name)
            title = titles.get(sid.group(0)) if sid else None
            yield {
                "session": p.name,
                "session_path": str(p),
                "cwd": cwd,
                "last_used": m,
                "title": title or _last_prompt(str(p)),
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

    def is_agent_process(self, argv):
        if not argv:
            return False
        a0 = argv[0]
        if os.path.basename(a0) in _HELPER_ARGV0:
            return False
        if "/.codex/tmp/arg0/" in a0 or "/app-server-daemon/" in a0:
            return False
        return _subcommand(argv) not in _NON_SESSION_SUBCOMMANDS

    def resume_argv(self, instance, sid, session_path, live_argv=None):
        return ["codex", "resume", *_resume_flags(instance, session_path), sid]

    def delete_session(self, instance, sid, session_path):
        try:
            res = subprocess.run(["codex", "delete", "--force", sid],
                                 capture_output=True, text=True,
                                 timeout=_CLI_TIMEOUT_S, env=_env(instance),
                                 stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            _delete_via_sql(instance, sid, session_path)
            return
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"codex delete {sid} timed out")
        if res.returncode != 0:
            # A refusal (e.g. a fork still references the thread) is final.
            lines = (res.stderr or res.stdout).strip().splitlines()
            raise RuntimeError(lines[-1] if lines else
                               f"codex delete exited {res.returncode}")


HANDLER = CodexHandler()
register(HANDLER)
