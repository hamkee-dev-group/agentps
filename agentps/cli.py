"""Command-line entry point. Argparse, dispatch, and the non-TUI printers."""

from __future__ import annotations

import argparse
import json
import locale
import signal
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .actions import delete, resume
from .core import all_instances, load_config
from .discovery import discover_all
from .format import BAR, fmt_date, short_session, shorten
from . import format as fmt_mod


_TRACES_LIMIT = 15


def print_table(rows) -> None:
    if not rows:
        print("No CLI agents found.")
        return
    header = ["AGENT", "PID", "USER", "LAST_USED", "CWD", "SESSION", "WHERE"]
    out = [header]
    split_at = None  # row index where missing-cwd block begins
    for a in rows:
        if split_at is None and a.get("cwd_missing"):
            split_at = len(out)
        out.append([
            a["agent"],
            str(a["pid"]) if a.get("pid") else "-",
            a["user"],
            fmt_date(a.get("last_used")),
            a["cwd"],
            short_session(a["session"]),
            shorten(a["where"], 28),
        ])
    widths = [max(len(r[i]) for r in out) for i in range(len(out[0]))]
    line = "  ".join(f"{{:<{w}}}" for w in widths)
    total = sum(widths) + 2 * (len(widths) - 1)
    for i, r in enumerate(out):
        if i == split_at:
            label = " sessions whose cwd no longer exists "
            pad = max(0, total - len(label))
            print(BAR * (pad // 2) + label + BAR * (pad - pad // 2))
        print(line.format(*r))
        if i == 0:
            print("  ".join("-" * w for w in widths))


def print_traces() -> None:
    """Per-instance recent-sessions dump. Uses each handler's trace_entries."""
    print()
    print("# Recent agent sessions (config-dir scan)")
    for inst in all_instances():
        print(f"\n## {inst.name}  ({inst.config_dir})")
        if not inst.config_dir.exists():
            print("  (no config dir)")
            continue
        if not inst.sessions_dir.exists():
            print("  (no sessions dir)")
            continue
        emitted = 0
        for line in inst.handler.trace_entries(inst, _TRACES_LIMIT):
            print(line)
            emitted += 1
        if emitted == 0:
            print("  (empty)")


def main() -> int:
    if not Path("/proc").is_dir():
        print("agentps requires Linux: /proc not found.", file=sys.stderr)
        return 2

    # Make `agentps … | head` exit cleanly.
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass

    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass

    p = argparse.ArgumentParser(
        prog="agentps",
        description="ps-like inventory of CLI coding agents",
    )
    p.add_argument("--version", action="version",
                   version=f"agentps {__version__}")
    p.add_argument("--traces", action="store_true",
                   help="also show recent sessions in agent config dirs")
    p.add_argument("--json", action="store_true",
                   help="emit JSON instead of a table")
    p.add_argument("--all", action="store_true",
                   help="loosen detection (more matches, more false positives)")
    p.add_argument("-d", "--delay", type=float, default=None, metavar="SECONDS",
                   help="TUI auto-refresh interval in seconds (default: 60; "
                        "0 disables auto-refresh)")
    p.add_argument("--config", type=Path, default=None, metavar="PATH",
                   help="path to config file "
                        "(default: ~/.config/agentps/config.toml)")

    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("top", help="interactive TUI (default mode)")
    sub.add_parser("list", help="print the table to stdout")
    rp = sub.add_parser("resume",
                        help="cd to the session's dir and launch the agent")
    rp.add_argument("prefix", help="session id (or 8+ char prefix)")
    rp.add_argument("--print", dest="print_only", action="store_true",
                    help="print the resume command instead of running it")
    dp = sub.add_parser("delete", help="delete one or more sessions")
    dp.add_argument("prefix", nargs="*", metavar="ID-OR-PATH",
                    help="session id (or 8+ char prefix), or a path "
                         "(deletes every session at or under that cwd)")
    dp.add_argument("--orphans", action="store_true",
                    help="delete every session whose cwd no longer exists")
    dp.add_argument("--dupes", action="store_true",
                    help="delete duplicates (same id present in multiple "
                         "config dirs)")
    dp.add_argument("-y", "--yes", action="store_true",
                    help="skip the confirmation prompt")
    args = p.parse_args()

    cfg = load_config(args.config)

    # Apply config-driven overrides for format helpers (date format, etc).
    if cfg.ui.date:
        fmt_mod.DATE_FMT = cfg.ui.date
    delay = args.delay if args.delay is not None else cfg.ui.delay
    sort_default = cfg.ui.sort or "date"

    if args.cmd == "resume":
        return resume(args.prefix, print_only=args.print_only)
    if args.cmd == "delete":
        if not args.orphans and not args.dupes and not args.prefix:
            dp.error("specify at least one prefix, or use --orphans/--dupes")
        return delete(args.prefix, force=args.yes,
                      orphans=args.orphans, dupes=args.dupes)

    if args.cmd == "list" or args.json or args.traces:
        rows = discover_all(loose=args.all)
        if args.json:
            print(json.dumps(rows, indent=2, default=str))
            return 0
        print_table(rows)
        if args.traces:
            print_traces()
        return 0

    from .tui import tui
    return tui(delay=delay, sort_default=sort_default)
