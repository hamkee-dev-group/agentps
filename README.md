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
- `resume PREFIX`: resume a session from its cwd
- `delete ...`: delete sessions by id prefix or by cwd path

Global options:

- `--json`: emit JSON instead of the table
- `--traces`: include per-agent recent-session scans from config dirs
- `--all`: looser process detection, with higher false-positive risk
- `-d`, `--delay`: TUI refresh interval in seconds; `0` disables auto-refresh
- `--config PATH`: alternate config file; governs both the UI settings and
  which agent instances exist. Missing file is an error rather than a silent
  fallback to the default

Delete options:

- `--orphans`: delete sessions whose cwd no longer exists
- `--dupes`: delete duplicate copies of the same session id
- `-y`, `--yes`: skip confirmation

## Output columns

- `AGENT`: the instance name, which is the handler name unless an `[[extra]]`
  gave it another one
- `PID`: the process holding the session, or `-` for a session with nothing
  running. A trailing `+N` means N further processes share the same session —
  opencode opens one per attached client — and the number shown is the lowest
- `USER`: owner of the session
- `LAST_USED`: from the session artifact, or from the agent's own database for
  agents that do not keep per-session files
- `CWD`: the recorded working directory. Sessions whose cwd no longer exists
  are grouped below a separator in `list`, and dimmed in the TUI
- `SESSION`: shortened session id
- `WHERE`: tmux pane, screen, or ssh, when the process can be traced to one

## TUI

The TUI is a curses interface over the same inventory used by `list`.

Key actions:

- `j` / `k` or arrow keys: move
- `Enter` or `o`: open or expand
- `Space`: mark row or group
- `c`: copy the resume command using OSC52
- `d`: delete focused or marked sessions
- `g`: toggle group-by-cwd
- `s`: toggle sort between date and path
- `r`: refresh
- `h`: help
- `q` or `Esc`: quit

## Session Handling Notes

### Codex

- Scans `~/.codex/sessions/`
- Replays model, reasoning effort, approval mode, and sandbox mode from `state_5.sqlite`
- Deletion removes both the rollout file and the matching `threads` row

### Claude

- Scans `~/.claude/projects/`
- Reads each session's own recorded `cwd` from its JSONL. The encoded directory
  name is lossy — `/home/u/my-proj` and `/home/u/my/proj` encode identically —
  so it is only used for sessions that record no `cwd` of their own
- Replays persisted permission mode on resume

### Gemini

- Scans `~/.gemini/tmp/`
- Deduplicates per-project snapshot files by inner `sessionId`
- Replays launch-only flags such as yolo or approval settings only when a live process is available
- Gemini has no resume-by-id: `-r` takes `latest` or a position within the
  project's sessions, so the index is only valid while that ordering holds.
  agentps resolves it immediately before launching, and copied or printed
  commands are emitted as `agentps resume <id>` so they re-resolve wherever
  they are pasted
- If a session can no longer be located, resume fails with a message instead
  of falling back to the most recent session

### opencode

- Reads `~/.local/share/opencode/opencode.db` (SQLite); there are no per-session files
- Archived sessions are hidden; the session id itself is used as the display id
- Replays the persisted model and agent on resume; launch-only flags such as `--dangerously-skip-permissions` only when a live process is available
- Deletion removes the `session` row (child rows cascade) and any leftover `session_diff` blob

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
