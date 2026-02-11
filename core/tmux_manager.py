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
