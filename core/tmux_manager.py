"""Tmux session management — start, stop, capture, send keys."""

import os
import subprocess
import tempfile
from .config import load_config


def _session_name(session_id, config=None):
    if config is None:
        config = load_config()
    prefix = config["settings"].get("tmux_prefix", "ta")
    return f"{prefix}-{session_id}"


def start_session(session_id, working_dir=None):
    config = load_config()
    session = config["sessions"].get(session_id)

    work_dir = working_dir or (session.get("working_dir") if session else None) or os.path.expanduser("~")
    session_name = _session_name(session_id, config)

    if is_running(session_id):
        raise ValueError(f"Session '{session_name}' is already running")

    os.makedirs(work_dir, exist_ok=True)

    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name, "-c", work_dir],
        check=True,
    )
    return session_name


def stop_session(session_id):
    session_name = _session_name(session_id)
    subprocess.run(["tmux", "kill-session", "-t", session_name], check=True)


def is_running(session_id):
    session_name = _session_name(session_id)
    result = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True,
    )
    return result.returncode == 0


def get_foreground_process(session_id):
    """Return the name of the foreground process in the session's pane."""
    session_name = _session_name(session_id)
    result = subprocess.run(
        ["tmux", "display-message", "-p", "-t", session_name,
         "#{pane_current_command}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def enter_session(session_id):
    session_name = _session_name(session_id)

    # Save current status-right and set a hint bar
    saved = subprocess.run(
        ["tmux", "show-option", "-t", session_name, "-v", "status-right"],
        capture_output=True, text=True,
    )
    old_status = saved.stdout.strip() if saved.returncode == 0 else ""

    subprocess.run([
        "tmux", "set-option", "-t", session_name, "status-right",
        " To get back to the TUI, press  Ctrl+B d ",
    ])
    subprocess.run([
        "tmux", "set-option", "-t", session_name, "status-style",
        "bg=colour236,fg=white",
    ])
    subprocess.run([
        "tmux", "set-option", "-t", session_name, "status-right-style",
        "bg=colour202,fg=white,bold",
    ])

    subprocess.run(["tmux", "attach-session", "-t", session_name])

    # Restore original status bar after detach
    if old_status:
        subprocess.run([
            "tmux", "set-option", "-t", session_name, "status-right", old_status,
        ])
    else:
        subprocess.run([
            "tmux", "set-option", "-t", session_name, "-u", "status-right",
        ])
    subprocess.run(["tmux", "set-option", "-t", session_name, "-u", "status-right-style"])
    subprocess.run(["tmux", "set-option", "-t", session_name, "-u", "status-style"])


def capture_pane(session_id, lines=200):
    session_name = _session_name(session_id)
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", session_name, "-p", "-S", f"-{lines}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to capture pane: {result.stderr}")
    return result.stdout


def capture_pane_ansi(session_id, lines=200):
    """Capture pane with ANSI escape sequences preserved (for xterm.js)."""
    session_name = _session_name(session_id)
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", session_name, "-p", "-e", "-S", f"-{lines}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to capture pane: {result.stderr}")
    return result.stdout


def get_pane_size(session_id):
    """Return (cols, rows) of the session's pane."""
    session_name = _session_name(session_id)
    result = subprocess.run(
        ["tmux", "display-message", "-p", "-t", session_name,
         "#{pane_width} #{pane_height}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return (80, 24)
    parts = result.stdout.strip().split()
    return (int(parts[0]), int(parts[1])) if len(parts) == 2 else (80, 24)


def resize_pane(session_id, cols, rows):
    """Resize the tmux pane to the given dimensions."""
    session_name = _session_name(session_id)
    subprocess.run(
        ["tmux", "resize-window", "-t", session_name, "-x", str(cols), "-y", str(rows)],
        capture_output=True,
    )


def send_keys(session_id, keys, enter=True):
    session_name = _session_name(session_id)
    subprocess.run(
        ["tmux", "send-keys", "-t", session_name, "-l", keys],
        check=True,
    )
    if enter:
        subprocess.run(
            ["tmux", "send-keys", "-t", session_name, "Enter"],
            check=True,
        )


def send_special_key(session_id, key):
    """Send a special key (e.g. 'Down', 'Up', 'Tab', 'Escape') without -l flag."""
    session_name = _session_name(session_id)
    subprocess.run(
        ["tmux", "send-keys", "-t", session_name, key],
        check=True,
    )


# Indicators that Claude Code is actively working (don't interrupt).
_CLAUDE_ACTIVE_INDICATORS = [
    "Running", "Waiting", "esc to interrupt", "Fetching",
    "Coalescing", "Churning", "Baking", "Mustering",
    "Crunching", "Cooking", "Cogitating", "Fluorescing",
    "Spelunking", "thought for", "Thinking", "Deliberating",
]

# Indicators that Claude Code is on an approval/confirmation prompt.
_CLAUDE_APPROVAL_MARKERS = [
    "Esc to cancel", "Do you want to", "(esc)", "ctrl-g to edit",
]


def claude_ready(session_id):
    """Check if Claude Code is at an empty, ready-to-receive prompt.

    Returns:
        dict with:
          ready: bool — True if safe to send input
          reason: str — why it's ready or not
    """
    if not is_running(session_id):
        return {"ready": False, "reason": "not_running"}

    content = capture_pane(session_id)
    all_lines = content.split("\n")
    last_lines = [l for l in all_lines if l.strip()][-6:]
    bottom = "\n".join(last_lines)

    if any(ind in bottom for ind in _CLAUDE_ACTIVE_INDICATORS):
        return {"ready": False, "reason": "active"}

    if any(marker in bottom for marker in _CLAUDE_APPROVAL_MARKERS):
        return {"ready": False, "reason": "approval_prompt"}

    at_prompt = "? for shortcuts" in bottom or "accept edits on" in bottom
    if not at_prompt:
        return {"ready": False, "reason": "not_at_prompt"}

    # Check for typed-but-unsubmitted text
    for line in last_lines:
        if "❯" in line:
            after = line.split("❯", 1)[1].strip()
            if after:
                return {"ready": False, "reason": "has_typed_text"}
            break

    return {"ready": True, "reason": "empty_prompt"}


def send_text_block(session_id, text):
    """Send a large block of text using tmux load-buffer + paste-buffer."""
    session_name = _session_name(session_id)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(text)
        tmp_path = f.name

    try:
        subprocess.run(["tmux", "load-buffer", tmp_path], check=True)
        subprocess.run(
            ["tmux", "paste-buffer", "-t", session_name], check=True
        )
    finally:
        os.unlink(tmp_path)
