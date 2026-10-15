# agentps

`agentps` is a Linux command-line tool for inspecting local CLI coding-agent sessions. It scans live processes and on-disk session artifacts, then presents them as a `ps`-like inventory with resume and cleanup operations.

It currently includes built-in handlers for:

- OpenAI Codex CLI
- Claude Code
- Gemini CLI
- opencode

For an introduction and walkthrough, see the [agentps blog post](https://hamkee.net/blog/agentps-manage-coding-agent-sessions/).

## What it does

- Lists live and historical agent sessions across supported tools
- Maps a live process back to its session artifact when possible
- Shows agent, PID, user, last-used time, cwd, session id, and launch context
- Resumes a session from its recorded working directory
- Deletes orphaned sessions or duplicate copies
- Provides a curses TUI for browsing, grouping, copying resume commands, and deleting sessions

## Platform

- Linux only
- Python 3.11+

`agentps` requires `/proc` and is designed around the session layouts used by the supported CLIs on a local Linux machine.

## Install

From the repository:

```bash
python -m pip install .
```

For development:

```bash
python -m pip install -e .
```

This package uses only the Python standard library at runtime.

## Quick Start

List sessions:

```bash
agentps list
```

Open the interactive view:

```bash
agentps
```

Print JSON:

```bash
agentps --json list
```

Show recent sessions found directly in agent config directories:

```bash
agentps --traces list
```

Stop a runaway agent, or every agent under a directory:

```bash
agentps kill 9b1c6f2e
```

Join a running agent — its tmux pane, or `claude attach` for a Claude
background session:

```bash
agentps attach 9b1c6f2e
```

Resume a session by id or unambiguous prefix:

```bash
agentps resume 9b1c6f2e
```

Print the resume command without executing it:

```bash
agentps resume --print 9b1c6f2e
```

Delete sessions by id prefix:

```bash
agentps delete 9b1c6f2e
```

Delete sessions whose recorded cwd no longer exists:

```bash
agentps delete --orphans
```

Delete duplicate session copies across configured instances:

```bash
agentps delete --dupes
```

## Commands

```text
agentps [OPTIONS] [top|list|resume|delete] [OPTIONS]
```

Global options are accepted on either side of the subcommand, so both of these
work:

```bash
agentps --json list
agentps list --json
```

Subcommands:

- `top`: interactive TUI; also the default when no subcommand is given
- `list`: print the table to stdout
- `resume PREFIX`: open a session. A running session is joined (the agent's
  own attach command, else its tmux pane) rather than resumed a second time;
  anything else is resumed from its recorded cwd
- `attach PREFIX`: join a running session: its tmux pane, or the agent's own
  attach command where it has one
- `kill ...`: stop the sessions matched by id prefix or cwd path
- `delete ...`: delete sessions by id prefix or by cwd path

Global options:

- `--json`: emit JSON instead of the table
- `--traces`: include per-agent recent-session scans from config dirs
- `--all`: looser process detection, with higher false-positive risk
- `-d`, `--delay`: TUI refresh interval in seconds; `0` disables auto-refresh
- `--config PATH`: alternate config file; governs both the UI settings and
  which agent instances exist. Missing file is an error rather than a silent
  fallback to the default

Kill options:

- `-9`, `--force`: send `SIGKILL` instead of `SIGTERM`
- `-y`, `--yes`: skip confirmation

`kill` stops a session through the agent's own stop command where it has one
(`claude stop` for a Claude background session, which keeps the conversation
resumable), and otherwise signals every process behind it. `--force` always
signals. It refuses to touch the session agentps itself was launched from, and
skips any process that exited and whose PID was reused while it waited for
confirmation.

Delete options:

- `--orphans`: delete sessions whose cwd no longer exists. A cwd that was
  never recorded, or that cannot be checked (permissions, a hung mount), does
  not count
- `--dupes`: delete duplicate copies of the same session id
- `-y`, `--yes`: skip confirmation

## Output columns

- `AGENT`: the instance name, which is the handler name unless an `[[extra]]`
  gave it another one
- `PID`: the process holding the session, or `-` for a session with nothing
  running. A trailing `+N` means N further processes share the same session —
  opencode opens one per attached client — and the number shown is the lowest
- `S`: process state — `R` running, `S` sleeping, `D` uninterruptible,
  `T` stopped, `Z` zombie
- `USER`: owner of the session
- `LAST_USED`: from the session artifact, or from the agent's own database for
  agents that do not keep per-session files
- `AGE`: how long the process has been running
- `IDLE`: time since the session was last written — the activity signal for an
  agent nobody is watching. A long `IDLE` on a running process is an agent that
  is waiting or stuck
- `CWD`: the recorded working directory. Sessions whose cwd no longer exists
  are grouped below a separator in `list`, and dimmed in the TUI
- `SESSION`: shortened session id
- `WHERE`: tmux pane, screen, or ssh when the process can be traced to one;
  `bg:<id>` for a Claude background session (join it with `agentps attach`);
  otherwise `via:<name>` for whatever launched it — a systemd unit, cron, or a
  script — which is the usual case for agents started headlessly

## TUI

The TUI is a curses interface over the same inventory used by `list`.

Key actions:

- `j` / `k` or arrow keys: move
- `Enter` or `o`: open (joins a running session) or expand
- `Space`: mark row or group
- `a`: join a running session: its tmux pane or the agent's attach command
- `c`: copy the command that opens the session, using OSC52
- `K`: stop the focused or marked sessions (agent's stop command, else SIGTERM)
- `d`: delete focused or marked sessions
- `g`: toggle group-by-cwd
- `s`: toggle sort between date and path
- `r`: refresh
- `h`: help
- `q` or `Esc`: quit

## Session Handling Notes

Checked against Claude Code 2.1.300, Codex 0.155 (and the 0.162 source),
Gemini CLI 0.41.2 (and the 0.64 source), and opencode 1.18 (and the 2.0
schema).

### Codex

- Scans `~/.codex/sessions/`
- `codex resume <id>` restores the thread's own model, effort and provider, so
  none of them is replayed. Before codex 0.160 the approval policy and sandbox
  mode are replayed from the session's last turn context; from 0.160 codex
  restores them itself
- Deletion runs `codex delete --force <id>`, which also clears the thread's
  history, goals, queue and memory databases and refuses while a fork still
  references it. Direct SQL is a fallback only when the binary is missing
- The app-server daemon, sandbox helpers and non-session subcommands
  (`mcp`, `app-server`, …) are not listed as sessions

### Claude

- Scans `~/.claude/projects/`
- Live processes are matched through Claude's own registry
  (`~/.claude/sessions/<pid>.json`), then `--session-id` on the command line,
  and only then by cwd
- Background sessions (`claude --bg`, and agents launched from another session)
  show as `bg:<id>`. Opening one runs `claude attach <id>` — `claude --resume`
  refuses a session that is still running — and `kill` runs `claude stop <id>`
- Reads each session's own recorded `cwd` from its JSONL. The encoded directory
  name is lossy — `/home/u/my-proj` and `/home/u/my/proj` encode identically —
  so it is only used for sessions that record no `cwd` of their own
- Replays persisted permission mode on resume
- Deletion also removes the session's subagent transcripts, file history,
  saved environment, tasks and todos, and its background-job record unless that
  job owns a worktree

### Gemini

- Scans `~/.gemini/tmp/`
- Deduplicates per-project snapshot files by inner `sessionId`
- Resumes with `gemini --resume <uuid>`. Gemini accepts a full session id there
  even though `--help` only mentions numbers; numbers count oldest-first and
  shift as sessions come and go
- Replays launch-only flags such as yolo or approval settings only when a live process is available

### opencode

- Reads `~/.local/share/opencode/opencode.db` (SQLite); there are no per-session files
- Supports both schemas: `session` (1.x) and `session_v2` (2.x). While 2.x is
  still migrating old sessions, the ones not yet copied are shown too
- Only top-level sessions are listed; subagent sessions belong to their parent
- Archived sessions are hidden; the session id itself is used as the display id
- 1.x: replays the persisted model and agent on resume. 2.x reads them from the
  session itself. Launch-only flags such as `--auto` only when a live process
  is available
- Deletion removes the session, its subagent sessions and their event logs.
  On 2.x it goes through `opencode session delete`, since the shared
  background service owns the database while it runs
- 2.x's shared background service (`opencode serve --service`) is not listed
  as a session

## Configuration

Default config path:

```text
~/.config/agentps/config.toml
```

Supported UI settings:

```toml
[ui]
sort = "date"      # "date" or "path"
date = "%m-%d-%Y"  # strftime format for LAST_USED
delay = 60
```

You can add extra agent instances, such as alternate config roots:

```toml
[[extra]]
name = "codex-api"
handler = "codex"
dir = "~/.codex-api"

[[extra]]
name = "claude-work"
handler = "claude"
dir = "~/work/.claude"

[extra.env]
EXAMPLE_FLAG = "1"
```

Each `[[extra]]` entry binds a handler to another base directory and optional environment variables used during resume. `dir` is the handler's base directory, which is not always a dotfile config dir — for `opencode` it is the data dir holding `opencode.db` (default `~/.local/share/opencode`).

## Custom Handlers

User-defined handlers can be placed in:

```text
~/.config/agentps/handlers/
```

Each handler module must expose a top-level `HANDLER` object derived from the internal `Handler` interface. User handlers shadow built-in handlers of the same name.

## Limitations

- Linux only
- Session detection depends on the supported CLIs' current on-disk layouts
- Resume fidelity varies by agent; some launch flags are reconstructable only for live sessions
- `--all` can match unrelated processes if their command lines look similar to a supported agent

## License

MIT. See [LICENSE](LICENSE).
