"""Curses TUI plus shared format helpers (date/age/shortening, encoding
fallbacks). Pure presentation — no agent-specific logic."""

from __future__ import annotations

import curses
import signal
import sys

from .actions import (_self_pids, copy_to_clipboard, perform_delete,
                      resume_command_str, session_id, signal_pids)
from .core import ResumeUnavailable, all_instances
from .discovery import discover, discover_all
from .format import (ARROWS, SORT_ASC, SORT_DESC, fmt_date, fmt_idle, fmt_pid,
                     short_session, shorten)


_TUI_HEADER = ["AGENT", "PID", "S", "USER", "LAST_USED", "AGE", "IDLE", "CWD",
               "SESSION", "WHERE"]


def row_key(r) -> str:
    """Stable identity for a row. Selection and focus are keyed by this, never
    by list position: the list is re-sorted on every refresh, so an index marks
    a different session a minute later."""
    return r.get("session_path") or f"pid:{r.get('pid')}"


def _tui_cells(r):
    return [
        r["agent"],
        fmt_pid(r),
        r.get("state") or "-",
        r["user"],
        fmt_date(r.get("last_used")),
        r.get("age") or "-",
        fmt_idle(r.get("last_used")),
        r["cwd"],
        short_session(r.get("session")),
        r.get("where") or "-",
    ]


_CWD_COL = _TUI_HEADER.index("CWD")
_DATE_COL = _TUI_HEADER.index("LAST_USED")
_MIN_CWD = 16
_MARKER_WIDTH = 3      # focus mark, selection mark, space


def _tui_widths(rows, header, max_width: int | None = None):
    """Column widths, with CWD given up to whatever is left. It is the only
    unbounded column, so without a cap one deep path pushes SESSION and WHERE
    off the right edge."""
    cells = [header] + [_tui_cells(r) for r in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(header))]
    if max_width:
        used = sum(widths) + 2 * (len(widths) - 1) + _MARKER_WIDTH
        overflow = used - max_width
        if overflow > 0:
            widths[_CWD_COL] = max(_MIN_CWD, widths[_CWD_COL] - overflow)
    return widths


def _fit(cells, widths):
    """Shorten the cwd cell to its allotted width, keeping the tail."""
    if len(cells[_CWD_COL]) > widths[_CWD_COL]:
        cells = list(cells)
        cells[_CWD_COL] = shorten(cells[_CWD_COL], widths[_CWD_COL])
    return cells


def _header_with_sort(sort_mode: str):
    cells = list(_TUI_HEADER)
    if sort_mode == "date":
        cells[_DATE_COL] += SORT_DESC
    elif sort_mode == "path":
        cells[_CWD_COL] += " " + SORT_ASC
    return cells


def _tui_help(stdscr, delay: float) -> None:
    auto = f"{int(delay)}s" if delay and delay > 0 else "off"
    lines = [
        "agentps — keyboard reference",
        "",
        "  Navigation",
        f"    {ARROWS} or j/k       move focus",
        "    PgUp / PgDn        jump by page",
        "    Home / End / G     jump to top / bottom",
        "",
        "  Selection",
        "    Space              mark / unmark focused row",
        "                       on a group header: marks all children",
        "",
        "  Actions",
        "    Enter / o          open session, or expand/collapse a group",
        "    a                  switch to the tmux pane the agent runs in",
        "    c                  copy resume command for focused session (OSC52)",
        "    K                  SIGTERM the process(es) behind the session",
        "    d                  delete focused or marked session(s)",
        "                       on a group header: deletes the entire group",
        "",
        "  Modes",
        "    g                  toggle group-by-cwd (collapsed by default)",
        f"    s                  toggle sort: date {SORT_DESC} <-> path {SORT_ASC}",
        "",
        "  Other",
        "    r                  manual refresh",
        f"                       (auto-refresh: {auto}, set with -d SECONDS)",
        "    h                  show this help",
        "    q / Esc            quit",
        "",
        "  Legend",
        "    [+] / [-]          collapsed / expanded group",
        "    *                  row or group fully selected",
        "    +                  group partially selected",
        f"    {SORT_DESC} {SORT_ASC}                active sort column (date desc / path asc)",
        "    dim row            cwd no longer exists",
        "",
        "  Press any key to close",
    ]
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    for i, line in enumerate(lines):
        if i >= h - 1:
            break
        try:
            attr = curses.A_BOLD if i == 0 else 0
            stdscr.addstr(i, 0, line[: w - 1], attr)
        except curses.error:
            pass
    stdscr.refresh()
    while True:
        ch = stdscr.getch()
        if ch == -1:
            continue
        if ch == curses.KEY_RESIZE:
            return
        return


def _delete_prompt(target_rows) -> str:
    """Name what is about to be deleted. A bare count is not enough to catch a
    mis-selection before it is irreversible."""
    if len(target_rows) == 1:
        r = target_rows[0]
        return (f"Delete {short_session(r.get('session'))} "
                f"({r['agent']}) in {r['cwd']}? [y/N]")
    cwds = {r["cwd"] for r in target_rows}
    where = next(iter(cwds)) if len(cwds) == 1 else f"{len(cwds)} directories"
    return f"Delete {len(target_rows)} sessions in {where}? [y/N]"


def _tui_confirm(stdscr, prompt: str) -> bool:
    h, w = stdscr.getmaxyx()
    try:
        stdscr.move(h - 1, 0)
        stdscr.clrtoeol()
        stdscr.addstr(h - 1, 0, prompt[: w - 1], curses.A_BOLD)
    except curses.error:
        pass
    stdscr.refresh()
    while True:
        ch = stdscr.getch()
        if ch == -1:
            continue
        if ch in (ord("y"), ord("Y")):
            return True
        if ch in (ord("n"), ord("N"), 27, ord("q")):
            return False


def _sort_rows(rows, mode: str):
    if mode == "path":
        rows.sort(key=lambda r: (r["cwd_missing"], r["cwd"],
                                 -(r.get("last_used") or 0)))
    else:
        rows.sort(key=lambda r: (r["cwd_missing"],
                                 -(r.get("last_used") or 0)))
    return rows


def _build_groups(rows, sort_mode: str):
    by_cwd: dict = {}
    for i, r in enumerate(rows):
        by_cwd.setdefault(r["cwd"], []).append(i)
    groups = []
    for cwd, idxs in by_cwd.items():
        last = max((rows[i].get("last_used") or 0) for i in idxs)
        missing = bool(rows[idxs[0]].get("cwd_missing"))
        groups.append({
            "cwd": cwd,
            "children": idxs,
            "last_used": last,
            "cwd_missing": missing,
        })
    if sort_mode == "path":
        groups.sort(key=lambda g: (g["cwd_missing"], g["cwd"]))
    else:
        groups.sort(key=lambda g: (g["cwd_missing"], -g["last_used"]))
    return groups


def _focus_target(view, rows, focus):
    """What the cursor is on right now, as an identity we can search for after
    the list is rebuilt: a group cwd or a row key."""
    if not (0 <= focus < len(view)):
        return None
    kind, payload = view[focus]
    if kind == "group":
        return ("group", payload["cwd"])
    return ("row", row_key(rows[payload]))


def _find_focus(view, rows, target):
    """Where that identity landed in the rebuilt view, or None if it is gone."""
    kind, ident = target
    for i, (item_kind, payload) in enumerate(view):
        if kind == "group":
            if item_kind == "group" and payload["cwd"] == ident:
                return i
        elif item_kind != "group" and row_key(rows[payload]) == ident:
            return i
    return None


def _build_view(rows, group_mode: bool, sort_mode: str, expanded: set):
    if not group_mode:
        return [("row", i) for i in range(len(rows))]
    view = []
    for g in _build_groups(rows, sort_mode):
        view.append(("group", g))
        if g["cwd"] in expanded:
            children = sorted(
                g["children"],
                key=lambda i: -(rows[i].get("last_used") or 0),
            )
            for i in children:
                view.append(("child", i))
    return view


def _tui_main(stdscr, delay: float, sort_default: str, registry=None):
    curses.curs_set(0)
    stdscr.keypad(True)
    timeout_ms = int(delay * 1000) if delay and delay > 0 else -1
    stdscr.timeout(timeout_ms)
    sel_attr = curses.A_BOLD
    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLUE)
        sel_attr = curses.color_pair(1)
    except curses.error:
        pass

    if registry is None:
        registry = all_instances()
    rows = discover_all(registry)
    sort_mode = sort_default if sort_default in ("date", "path") else "date"
    _sort_rows(rows, sort_mode)
    focus = 0
    selected: set[str] = set()
    scroll = 0
    msg = ""
    group_mode = False
    expanded: set = set()
    refocus: tuple[str, str] | None = None

    def refresh():
        """Re-read state, keeping marks on the sessions they were put on and
        the cursor on the row it was left on."""
        nonlocal rows, selected, refocus
        refocus = _focus_target(view, rows, focus)
        rows = _sort_rows(discover_all(registry), sort_mode)
        selected &= {row_key(r) for r in rows}

    while True:
        h, w = stdscr.getmaxyx()
        header_cells = _header_with_sort(sort_mode)
        widths = _tui_widths(rows, header_cells, max_width=w - 1)
        line_fmt = "  ".join(f"{{:<{x}}}" for x in widths)
        view = _build_view(rows, group_mode, sort_mode, expanded)
        if refocus is not None:
            found = _find_focus(view, rows, refocus)
            if found is not None:
                focus = found
            refocus = None
        if focus >= len(view):
            focus = max(0, len(view) - 1)

        stdscr.erase()
        try:
            header = ("   " + line_fmt.format(*header_cells))[: w - 1]
            stdscr.addstr(0, 0, header, curses.A_BOLD)
        except curses.error:
            pass

        visible = max(0, h - 3)
        if focus < scroll:
            scroll = focus
        elif focus >= scroll + visible:
            scroll = max(0, focus - visible + 1)

        for i in range(visible):
            idx = scroll + i
            if idx >= len(view):
                break
            item = view[idx]
            focus_mark = ">" if idx == focus else " "

            if item[0] == "group":
                g = item[1]
                indicator = "[-]" if g["cwd"] in expanded else "[+]"
                cells = [""] * len(_TUI_HEADER)
                cells[0] = indicator
                cells[_DATE_COL] = fmt_date(g["last_used"])
                cells[_TUI_HEADER.index("IDLE")] = fmt_idle(g["last_used"])
                cells[_CWD_COL] = g["cwd"]
                cells[_TUI_HEADER.index("SESSION")] = f"({len(g['children'])})"
                sel_count = sum(1 for c in g["children"]
                                if row_key(rows[c]) in selected)
                if sel_count == 0:
                    sel_mark = " "
                elif sel_count == len(g["children"]):
                    sel_mark = "*"
                else:
                    sel_mark = "+"
                line = (focus_mark + sel_mark + " "
                        + line_fmt.format(*_fit(cells, widths)))[: w - 1]
                attr = curses.A_BOLD
                if g["cwd_missing"]:
                    attr |= curses.A_DIM
                if sel_count == len(g["children"]) and sel_count > 0:
                    attr |= sel_attr
                if idx == focus:
                    attr |= curses.A_REVERSE
            else:
                r = rows[item[1]]
                is_sel = row_key(r) in selected
                sel_mark = "*" if is_sel else " "
                line = (focus_mark + sel_mark + " "
                        + line_fmt.format(*_fit(_tui_cells(r), widths)))[: w - 1]
                attr = 0
                if r.get("cwd_missing"):
                    attr |= curses.A_DIM
                if is_sel:
                    attr |= sel_attr
                if idx == focus:
                    attr |= curses.A_REVERSE

            try:
                stdscr.addstr(1 + i, 0, line.ljust(w - 1)[: w - 1], attr)
            except curses.error:
                pass

        bar = (
            f" {focus + 1}/{len(view)}  "
            f"Space:mark  Enter:open  a:attach  c:copy  K:kill  d:del  "
            f"g:group  s:sort  r:refresh  h:help  q:quit "
        )
        try:
            stdscr.addstr(h - 2, 0, bar[: w - 1], curses.A_BOLD)
        except curses.error:
            pass
        if msg:
            try:
                stdscr.addstr(h - 1, 0, msg[: w - 1])
            except curses.error:
                pass
        stdscr.refresh()
        msg = ""

        ch = stdscr.getch()
        if ch == -1:
            refresh()
            continue
        if ch in (ord("q"), 27):
            return None
        if ch == curses.KEY_RESIZE:
            continue
        if ch in (curses.KEY_UP, ord("k")):
            focus = max(0, focus - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            focus = min(max(0, len(view) - 1), focus + 1)
        elif ch == curses.KEY_NPAGE:
            focus = min(max(0, len(view) - 1), focus + max(1, visible - 1))
        elif ch == curses.KEY_PPAGE:
            focus = max(0, focus - max(1, visible - 1))
        elif ch == curses.KEY_HOME:
            focus = 0
        elif ch in (curses.KEY_END, ord("G")):
            focus = max(0, len(view) - 1)
        elif ch == ord("g"):
            group_mode = not group_mode
            expanded.clear()
            selected.clear()
            focus = 0
            scroll = 0
            msg = f"group: {'on' if group_mode else 'off'}"
        elif ch == ord(" "):
            if 0 <= focus < len(view):
                item = view[focus]
                if item[0] == "group":
                    keys = [row_key(rows[i]) for i in item[1]["children"]]
                    if keys and all(k in selected for k in keys):
                        selected.difference_update(keys)
                    else:
                        selected.update(keys)
                else:
                    selected ^= {row_key(rows[item[1]])}
                focus = min(max(0, len(view) - 1), focus + 1)
        elif ch in (curses.KEY_ENTER, 10, 13, ord("o")):
            if 0 <= focus < len(view):
                item = view[focus]
                if item[0] == "group":
                    cwd = item[1]["cwd"]
                    if cwd in expanded:
                        expanded.discard(cwd)
                    else:
                        expanded.add(cwd)
                else:
                    return ("open", rows[item[1]])
        elif ch == ord("c"):
            if 0 <= focus < len(view):
                item = view[focus]
                if item[0] == "group":
                    msg = "no session focused (expand a group to copy)"
                else:
                    r = rows[item[1]]
                    sid = session_id(r.get("session") or "")
                    if not sid:
                        msg = "no session id on focused row"
                    else:
                        instance = next((i for i in registry
                                         if i.name == r["agent"]), None)
                        if instance is None:
                            msg = f"no handler for {r['agent']!r}"
                        else:
                            cwd = r["cwd"] if r["cwd"] != "?" else "."
                            try:
                                cmd = resume_command_str(
                                    instance, sid,
                                    r.get("session_path") or "", cwd,
                                    live_argv=r.get("live_argv"),
                                )
                            except ResumeUnavailable as e:
                                msg = str(e)
                            else:
                                status = copy_to_clipboard(cmd)
                                msg = (cmd if status.startswith("copied")
                                       else f"{status}: {cmd}")
        elif ch == ord("d"):
            if selected:
                target_rows = [r for r in rows if row_key(r) in selected]
            elif 0 <= focus < len(view):
                item = view[focus]
                if item[0] == "group":
                    target_rows = [rows[i] for i in item[1]["children"]]
                else:
                    target_rows = [rows[item[1]]]
            else:
                target_rows = []
            if not target_rows:
                continue
            if not _tui_confirm(stdscr, _delete_prompt(target_rows)):
                msg = "delete aborted"
                continue
            live_paths = {a["session_path"] for a in discover(registry)
                          if a.get("session_path")}
            targets = []
            skipped_live = 0
            for r in target_rows:
                sid = session_id(r.get("session") or "")
                if not sid:
                    continue
                if r["session_path"] in live_paths:
                    skipped_live += 1
                    continue
                targets.append((sid, r))
            removed, errors = perform_delete(targets, registry)
            refresh()
            selected.clear()
            parts = [f"removed {removed}"]
            if skipped_live:
                parts.append(f"skipped {skipped_live} live")
            if errors:
                parts.append(f"{len(errors)} error(s)")
            msg = ", ".join(parts)
        elif ch == ord("K"):
            if selected:
                target_rows = [r for r in rows if row_key(r) in selected]
            elif 0 <= focus < len(view):
                item = view[focus]
                target_rows = ([rows[i] for i in item[1]["children"]]
                               if item[0] == "group" else [rows[item[1]]])
            else:
                target_rows = []
            live_rows = [r for r in target_rows if r.get("pid")]
            if not live_rows:
                msg = "no running process on the selected row(s)"
                continue
            mine = _self_pids()
            pids = [p for r in live_rows for p in (r.get("pids") or [r["pid"]])
                    if p not in mine]
            if not pids:
                msg = "that is the session running agentps — exit it instead"
                continue
            if not _tui_confirm(
                    stdscr, f"Send SIGTERM to {len(pids)} process(es) in "
                            f"{len(live_rows)} session(s)? [y/N]"):
                msg = "kill aborted"
                continue
            sent, errors = signal_pids(pids, signal.SIGTERM)
            refresh()
            selected.clear()
            msg = f"signalled {sent}" + (f", {len(errors)} error(s)"
                                         if errors else "")
        elif ch == ord("a"):
            if 0 <= focus < len(view) and view[focus][0] != "group":
                r = rows[view[focus][1]]
                if r.get("pane"):
                    return ("attach", r)
                msg = f"no tmux pane for this session (running under: " \
                      f"{r.get('where') or '-'})"
            else:
                msg = "no session focused"
        elif ch == ord("r"):
            refresh()
            msg = "refreshed"
        elif ch == ord("s"):
            sort_mode = "path" if sort_mode == "date" else "date"
            _sort_rows(rows, sort_mode)
            selected.clear()
            focus = 0
            scroll = 0
            msg = f"sorted by {sort_mode}"
        elif ch == ord("h") or ch == ord("?"):
            _tui_help(stdscr, delay)


def tui(delay: float = 60.0, sort_default: str = "date", registry=None) -> int:
    import os

    if registry is None:
        registry = all_instances()
    try:
        result = curses.wrapper(_tui_main, delay, sort_default, registry)
    except curses.error as e:
        print(f"agentps: cannot start the interactive view ({e}). "
              f"Use `agentps list` when there is no terminal.", file=sys.stderr)
        return 2
    if not result:
        return 0
    if result[0] == "attach":
        from .actions import attach
        sid = session_id(result[1].get("session") or "")
        if not sid:
            print("focused row has no session id", file=sys.stderr)
            return 1
        return attach(sid, registry=registry)
    if result[0] == "open":
        row = result[1]
        sid = session_id(row.get("session") or "")
        if not sid:
            print("focused row has no session id", file=sys.stderr)
            return 1
        instance = next((i for i in registry if i.name == row["agent"]), None)
        if instance is None:
            print(f"no handler for {row['agent']!r}", file=sys.stderr)
            return 1
        cwd = row["cwd"] if row["cwd"] != "?" else "."
        try:
            argv = instance.handler.resume_argv(
                instance, sid, row.get("session_path") or "",
                live_argv=row.get("live_argv"),
            )
        except ResumeUnavailable as e:
            print(f"agentps: {e}", file=sys.stderr)
            return 1
        env = os.environ.copy()
        env.update(instance.env)
        try:
            os.chdir(cwd)
        except OSError as e:
            print(f"cannot cd to {cwd}: {e}", file=sys.stderr)
            return 1
        try:
            os.execvpe(argv[0], argv, env)
        except FileNotFoundError:
            print(f"command not found: {argv[0]}", file=sys.stderr)
            return 1
        except OSError as e:
            print(f"failed to launch {argv[0]}: {e}", file=sys.stderr)
            return 1
    return 0
