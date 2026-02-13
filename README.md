# TinyAgentic.ai

A terminal UI for managing fleets of AI agents running in tmux sessions. Built with [Textual](https://textual.textualize.io/), designed for orchestrating [Claude Code](https://docs.anthropic.com/en/docs/claude-code) instances and similar AI coding agents at scale.

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## What it does

TinyAgentic.ai gives you a dashboard to spawn, monitor, and automate multiple AI agent sessions from a single terminal. Instead of juggling tmux windows manually, you get:

- **Live terminal panels** showing each agent's output in real time
- **Dashboards** to organize sessions by project or purpose
- **Macros** to send predefined commands to agents with variable substitution
- **Routines** that run scripts or macros on a schedule (e.g. auto-approve prompts every 10s)
- **Claude Code readiness detection** that knows when an agent is actually at an empty prompt vs. thinking, waiting for approval, or mid-task
- **Session variables** so macros can reference per-agent values like `$agent_name` or `$project_path`

## Quick start

### Prerequisites

- Python 3.11+
- tmux (`apt install tmux`)

### Install & run

```bash
git clone https://github.com/cesarlee/tinyagentic.git
cd tinyagentic

pip install textual python-dotenv rich

# Optional: add your Anthropic API key for AI-powered features
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env

python main.py
```

## How it works

### Layout

```
┌─────────────────────────────────────────────────────────────┐
│ TinyAgentic.ai                                              │
├──────────┬──────────────────────────────────────────────────┤
│ DASHBDS  │  ┌──────────────────┐  ┌──────────────────┐     │
│  Main    │  │ ● agent-1        │  │ ● agent-2        │     │
│          │  │ WORKING | tmux:… │  │ idle | tmux:…    │     │
│ SESSIONS │  │                  │  │                  │     │
│  ● agnt1 │  │ (live terminal   │  │ (live terminal   │     │
│  ○ agnt2 │  │  output)         │  │  output)         │     │
│ + Session│  └──────────────────┘  └──────────────────┘     │
│  Sess Var│                                                  │
│          │  ┌──────────────────┐                            │
│ SCRIPTS  │  │ ○ agent-3        │                            │
│ MACROS   │  │ stopped          │                            │
│ ROUTINES │  └──────────────────┘                            │
│          │                                                  │
│ ● Daemon │                                                  │
├──────────┴──────────────────────────────────────────────────┤
│ Session 'agent-1' started                                   │
├─────────────────────────────────────────────────────────────┤
│ n e d s x i m r M R p + - Tab q                             │
└─────────────────────────────────────────────────────────────┘
```

### Keybindings

| Key | Action |
|-----|--------|
| `n` | New session |
| `e` | Edit focused item |
| `d` | Delete focused item |
| `s` | Start session |
| `x` | Stop session |
| `i` | Interactive mode (attach to tmux) |
| `m` | Send message to session |
| `r` | Run script on session |
| `M` | Send macro to session |
| `R` | New routine |
| `p` | Toggle routines daemon |
| `+` / `-` | Grow / shrink panels |
| `Tab` | Toggle sidebar / panel focus |
| `Arrow keys` | Navigate panels and sidebar |
| `Enter` | Activate sidebar item |
| `1`-`9` | Switch dashboard |
| `q` | Quit |

## Features

### Sessions

Each session maps to a tmux terminal. Create a session, give it a working directory, start it, and watch the output live in the dashboard panels. Press `i` to attach interactively (detach back to TUI with `Ctrl+B d`).

Sessions show their health state in the panel subtitle:

| State | Meaning |
|-------|---------|
| **WORKING** | Claude Code is actively running tools/thinking |
| **WAITING** | Approval prompt detected |
| **idle** | At the prompt, ready for input |
| **LOW CTX** | Context window below 15% |
| **0% CTX** | Context exhausted, needs restart |
| **stopped** | tmux session not running |

### Dashboards

Group sessions into dashboards. Each dashboard has its own set of terminal panels. Scripts, macros, and routines are shared globally. Switch dashboards with number keys `1`-`9`.

### Macros

Predefined key sequences you can send to any session. Macros support:

- **Variable substitution** — Use `$var_name` in macro text, replaced with session-specific values at send time
- **Multi-line commands** — Auto-inserts bash line continuations
- **Send conditions** — Choose when a macro is allowed to fire:
  - `Always` — Send immediately
  - `Session idle` — Only if the session has no recent output
  - `Claude Code ready` — Only if Claude Code is at an empty prompt

### Routines

Scheduled automation that runs scripts or macros against sessions at configurable intervals.

- **Target sessions** — Run against a specific session or all running sessions (`*`)
- **Send when** — `Always`, `Session idle`, or `Claude Code ready`
- **Run when process** — Only execute when a specific process (e.g. `claude`) is the foreground command
- **Interactive protection** — Automatically skips sessions where a user is attached

The routines daemon runs in the background and can be toggled with `p`. The sidebar shows live countdowns to next execution.

### Scripts

Python scripts stored in `scripts/` that run against sessions. Each script defines a `run(session_id, tmux)` function:

```python
def run(session_id, tmux):
    content = tmux.capture_pane(session_id)
    if "some condition" in content:
        tmux.send_keys(session_id, "some command", enter=True)
        return {"status": "sent"}
    return {"status": "skipped"}
```

The `tmux` object provides: `capture_pane()`, `send_keys()`, `send_special_key()`, `is_running()`, `send_text_block()`.

**Built-in scripts:**

- **approval-watcher** — Auto-approves Claude Code permission prompts, handles clear-context safely, detects 0% context
- **check-chat** — Prompts idle Claude agents to check team chat messages

### Session Variables

Define global variable schemas (e.g. `agent_name`, `project_path`) that appear as fields when creating or editing sessions. Each session stores its own values. Macros reference them with `$var_name` syntax — substituted automatically at send time.

### Claude Code Readiness Detection

Goes beyond simple idle detection. Parses Claude Code's terminal UI to determine the actual agent state:

1. **Active indicators** — Running, Thinking, Fetching, Cooking, etc. (don't interrupt)
2. **Approval prompts** — "Do you want to", "Esc to cancel" (don't interrupt)
3. **Prompt detection** — Looks for `? for shortcuts` or `accept edits on` (confirms at prompt)
4. **Typed text check** — If text exists after the `❯` symbol, the agent has a pending self-prompt (skip)
5. **Empty prompt** — Safe to send input

This powers the `Claude Code ready` option on macros and routines.

## Project structure

```
tinyagentic.ai/
├── main.py                  # TUI application (Textual)
├── .env                     # API keys (gitignored)
├── config.json              # All configuration (gitignored)
├── core/
│   ├── config.py            # Thread-safe atomic config read/write
│   ├── sessions.py          # Session CRUD
│   ├── dashboards.py        # Dashboard CRUD
│   ├── macros.py            # Macro CRUD + variable substitution
│   ├── routines.py          # Routine CRUD + RoutinesDaemon
│   ├── scripts.py           # Script library + dynamic execution
│   ├── session_vars.py      # Session variable definitions
│   └── tmux_manager.py      # Tmux operations + Claude readiness
└── scripts/
    ├── approval-watcher.py  # Auto-approve Claude prompts
    └── check-chat.py        # Prompt agents to check chat
```

## Configuration

All state lives in `config.json` (created automatically on first run). It's intentionally a plain JSON file so agents can read and edit it programmatically.

```json
{
  "dashboards": {
    "main": { "name": "Main", "sessions": ["agent-1", "agent-2"] }
  },
  "active_dashboard": "main",
  "session_vars": [
    { "name": "agent_name", "label": "Agent Name" }
  ],
  "sessions": {
    "agent-1": {
      "name": "Agent 1",
      "working_dir": "/home/ubuntu/project",
      "vars": { "agent_name": "vm_architect" }
    }
  },
  "macros": {
    "check-status": {
      "name": "Check Status",
      "keys": "git status",
      "enter": true,
      "send_when": "claude_ready"
    }
  },
  "routines": {
    "approval-watcher": {
      "name": "Approval Watcher",
      "type": "script",
      "script": "approval-watcher",
      "session": "*",
      "interval_seconds": 10,
      "enabled": true,
      "send_when": "always"
    }
  },
  "settings": {
    "tmux_prefix": "ta",
    "scripts_dir": "scripts"
  }
}
```

## Architecture notes

- **Thread-safe config** — Reads and writes are mutex-locked; writes use atomic temp-file + rename to prevent corruption from concurrent access
- **Background workers** — Panel capture, session start, and script execution run in Textual worker threads to keep the UI responsive
- **No polling waste** — Routines daemon sleeps in 1-second increments for responsive shutdown; panel capture refreshes every 3 seconds
- **Dynamic script loading** — Scripts are loaded via `importlib` with clean module isolation (no `sys.modules` pollution)
- **Config as interface** — `config.json` is designed to be readable and writable by both the TUI and by the agents themselves

