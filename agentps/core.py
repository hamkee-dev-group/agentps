"""Core types: the Handler base class, AgentInstance, registry & config loader.

A `Handler` is per-agent code: how to find sessions, parse cwd, build a resume
command. An `AgentInstance` binds a Handler to a specific config dir + env, so
the same Handler can serve multiple installations (e.g. ~/.codex and
~/.codex-api both use the codex Handler).
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator


HOME = Path(os.path.expanduser("~"))
USER_CONFIG_DIR = HOME / ".config" / "agentps"
USER_CONFIG_FILE = USER_CONFIG_DIR / "config.toml"
USER_HANDLERS_DIR = USER_CONFIG_DIR / "handlers"


# ---------------------------------------------------------------------------
# Handler base class
# ---------------------------------------------------------------------------

class Handler:
    """Subclass this once per agent. Built-ins live in agentps/handlers/.
    User handlers live in ~/.config/agentps/handlers/ and shadow built-ins of
    the same name (with a stderr warning).

    Each method receives the `AgentInstance` so the same Handler can serve
    multiple installations.
    """

    name: str = ""                       # canonical name; matches `handler =` in config
    detect_substrings: list[str] = []    # in addition to f"/{config_dir.name}/"
    nodey: bool = True                   # also peek argv[1] for node/bun/deno wrappers

    def find_sessions(self, instance: "AgentInstance") -> Iterator[dict]:
        """Yield session dicts: {session, session_path, cwd, last_used}."""
        return iter(())

    def session_for_pid(self, instance: "AgentInstance", cwd: str | None,
                        started) -> Path | None:
        """Pick the session this live process is using. Default: the freshest
        session whose cwd matches."""
        if not cwd:
            return None
        best = None
        best_mtime = -1.0
        for s in self.find_sessions(instance):
            if s.get("cwd") != cwd:
                continue
            m = s.get("last_used") or 0
            if m > best_mtime:
                best_mtime = m
                best = s
        return Path(best["session_path"]) if best else None

    def trace_entries(self, instance: "AgentInstance",
                      limit: int) -> Iterator[str]:
        """Yield formatted lines for `--traces` output. Default: re-use
        find_sessions, sorted newest first."""
        from datetime import datetime
        rows = sorted(self.find_sessions(instance),
                      key=lambda s: -(s.get("last_used") or 0))[:limit]
        for s in rows:
            ts = datetime.fromtimestamp(s["last_used"]).strftime("%Y-%m-%d %H:%M")
            yield f"  {ts}  {s.get('cwd') or '?'}  [{s['session']}]"

    def resume_argv(self, instance: "AgentInstance", sid: str,
                    session_path: str,
                    live_argv: list[str] | None = None) -> list[str]:
        """argv to relaunch this session. `live_argv` is the cmdline of the
        currently-running process if there is one, so handlers can replay
        launch flags that aren't persisted (e.g. gemini's -y/yolo)."""
        return [self.name, "--resume", sid]

    def delete_session(self, instance: "AgentInstance", sid: str,
                       session_path: str) -> None:
        """Remove the session. Default: rmtree if dir, else unlink. Codex
        overrides to also nuke the SQLite row."""
        p = Path(session_path)
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()


# ---------------------------------------------------------------------------
# AgentInstance: a Handler + a place on disk
# ---------------------------------------------------------------------------

@dataclass
class AgentInstance:
    name: str                                          # display label
    handler: Handler
    sessions_dir: Path
    config_dir: Path
    env: dict[str, str] = field(default_factory=dict)

    @property
    def detect_substrings(self) -> list[str]:
        """Detection substrings: dir-derived + handler defaults."""
        return [f"/{self.config_dir.name}/", *self.handler.detect_substrings]


# ---------------------------------------------------------------------------
# Handler registry (name -> Handler instance)
# ---------------------------------------------------------------------------

_HANDLERS: dict[str, Handler] = {}


def register(handler: Handler) -> None:
    """Register a Handler. User handlers shadow built-ins (with a warning)."""
    if handler.name in _HANDLERS:
        prev = _HANDLERS[handler.name]
        if prev is not handler:
            print(f"agentps: handler {handler.name!r} shadowed by {handler!r}",
                  file=sys.stderr)
    _HANDLERS[handler.name] = handler


def get_handler(name: str) -> Handler | None:
    return _HANDLERS.get(name)


def known_handlers() -> dict[str, Handler]:
    return dict(_HANDLERS)


def _load_handlers_from_dir(dir_path: Path, kind: str) -> None:
    """Import every .py in dir_path; each must expose a top-level HANDLER."""
    if not dir_path.is_dir():
        return
    for f in sorted(dir_path.glob("*.py")):
        if f.name.startswith("_"):
            continue
        modname = f"agentps._{kind}_{f.stem}"
        spec = importlib.util.spec_from_file_location(modname, f)
        if not spec or not spec.loader:
            continue
        try:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as e:
            print(f"agentps: failed to load {kind} handler {f}: {e}",
                  file=sys.stderr)
            continue
        h = getattr(mod, "HANDLER", None)
        if isinstance(h, Handler):
            register(h)
        else:
            print(f"agentps: {f} has no top-level HANDLER (skipped)",
                  file=sys.stderr)


def load_builtin_handlers() -> None:
    """Import the built-in handlers package. Each module registers itself."""
    # Importing the package runs handlers/__init__.py which imports each module.
    from . import handlers  # noqa: F401


def load_user_handlers() -> None:
    """Load user handlers from ~/.config/agentps/handlers/."""
    _load_handlers_from_dir(USER_HANDLERS_DIR, "user")


# ---------------------------------------------------------------------------
# Config + registry assembly
# ---------------------------------------------------------------------------

@dataclass
class UISettings:
    sort: str = "date"
    date: str = "%m-%d-%Y"
    delay: float = 60.0


@dataclass
class Config:
    ui: UISettings = field(default_factory=UISettings)
    extras: list[dict] = field(default_factory=list)


def load_config(path: Path | None = None) -> Config:
    """Load ~/.config/agentps/config.toml (or the given path). Missing file
    is fine — defaults stand."""
    p = path or USER_CONFIG_FILE
    if not p.is_file():
        return Config()
    try:
        with open(p, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"agentps: failed to read {p}: {e}", file=sys.stderr)
        return Config()

    cfg = Config()
    ui = data.get("ui") or {}
    if "sort" in ui:
        cfg.ui.sort = str(ui["sort"])
    if "date" in ui:
        cfg.ui.date = str(ui["date"])
    if "delay" in ui:
        try:
            cfg.ui.delay = float(ui["delay"])
        except (TypeError, ValueError):
            pass
    cfg.extras = list(data.get("extra") or [])
    return cfg


def _expand(p: str) -> Path:
    """Expand ~ and $VAR in a path."""
    return Path(os.path.expandvars(os.path.expanduser(p)))


def build_registry(cfg: Config | None = None) -> list[AgentInstance]:
    """Construct the list of AgentInstances: built-in defaults + [[extra]]."""
    cfg = cfg or load_config()
    instances: list[AgentInstance] = []

    # Built-in defaults: one instance per registered handler, with the
    # handler's default dirs (set by the handler module).
    for name, handler in known_handlers().items():
        defaults = getattr(handler, "default_dirs", None)
        if not defaults:
            continue
        sessions_dir, config_dir = defaults()
        instances.append(AgentInstance(
            name=name,
            handler=handler,
            sessions_dir=sessions_dir,
            config_dir=config_dir,
        ))

    # User-added extras.
    for row in cfg.extras:
        try:
            handler_name = row.get("handler") or row.get("name")
            handler = _HANDLERS.get(handler_name)
            if handler is None:
                print(f"agentps: unknown handler {handler_name!r} in config "
                      f"(known: {sorted(_HANDLERS)})", file=sys.stderr)
                continue
            display = row.get("name") or handler_name
            base_dir = _expand(row["dir"])
            sessions_dir, config_dir = handler.default_dirs(base_dir)  # type: ignore[attr-defined]
            env = {str(k): str(v) for k, v in (row.get("env") or {}).items()}
            instances.append(AgentInstance(
                name=display,
                handler=handler,
                sessions_dir=sessions_dir,
                config_dir=config_dir,
                env=env,
            ))
        except KeyError as e:
            print(f"agentps: skipping extra (missing key {e}): {row}",
                  file=sys.stderr)

    return instances


def all_instances(cfg: Config | None = None) -> list[AgentInstance]:
    """Convenience: load handlers (once), build the registry."""
    if not _HANDLERS:
        load_builtin_handlers()
        load_user_handlers()
    return build_registry(cfg)


# ---------------------------------------------------------------------------
# Process detection (handler-aware)
# ---------------------------------------------------------------------------

def detect_handler(pid: int, registry: Iterable[AgentInstance],
                   loose: bool = False) -> Handler | None:
    """Return the Handler this PID belongs to, or None. Handler-level
    detection — instance/variant disambiguation happens later via
    session_for_pid."""
    from .proc import proc_cmdline, proc_comm, proc_exe

    cmdline = proc_cmdline(pid)
    if not cmdline:
        return None
    comm = proc_comm(pid)
    exe = proc_exe(pid) or ""
    base0 = os.path.basename(cmdline[0]) if cmdline else ""

    # Hardcoded daemon exclusion: the Claude rpc daemon at ~/.claude/remote/server
    # lives under the claude tree but is not an agent.
    if os.path.basename(exe) == "server":
        return None

    handlers = {inst.handler for inst in registry}
    by_name = {h.name: h for h in handlers}

    # Build the set of binary paths to inspect, expanding node/bun/deno
    # wrappers to also consider argv[1] (the JS entry point — which is the
    # actual agent for things like /usr/bin/gemini).
    binary_paths = [exe, cmdline[0]]
    is_nodey = (
        comm in ("node", "bun", "deno")
        or os.path.basename(exe) in ("node", "bun", "deno")
        or base0 in ("node", "bun", "deno")
    )
    if is_nodey and len(cmdline) >= 2:
        binary_paths.append(cmdline[1])

    # Strong signal: comm OR any binary path's basename matches a handler.
    if comm in by_name:
        return by_name[comm]
    for bp in binary_paths:
        bn = os.path.basename(bp) if bp else ""
        if bn in by_name:
            return by_name[bn]

    # Substring match. Longest substring wins so /.claude-api/ beats /.claude/
    # if both are configured (they don't actually overlap, but be safe).
    matches: list[tuple[int, Handler]] = []
    for inst in registry:
        for sub in inst.detect_substrings:
            for bp in binary_paths:
                if bp and sub in bp:
                    matches.append((len(sub), inst.handler))
    if matches:
        matches.sort(reverse=True)
        return matches[0][1]

    if loose:
        for inst in registry:
            if any(os.path.basename(a) == inst.name for a in cmdline):
                return inst.handler
    return None


def instance_for_pid(handler: Handler, registry: Iterable[AgentInstance],
                     cwd: str | None, started) -> tuple[AgentInstance | None,
                                                        Path | None]:
    """Given the handler that owns this PID, pick which AgentInstance (variant)
    holds the live session. Falls back to the first instance with this handler
    when no session matches the cwd."""
    fallback: AgentInstance | None = None
    for inst in registry:
        if inst.handler is not handler:
            continue
        if fallback is None:
            fallback = inst
        sess = handler.session_for_pid(inst, cwd, started)
        if sess is not None:
            return inst, sess
    return fallback, None
