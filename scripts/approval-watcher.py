"""Auto-approve Claude Code permission prompts.

Scans the last 50 lines of a session's terminal output for known
permission-prompt patterns and sends the appropriate keystroke.
"""

import re

# Each entry: (compiled_pattern, keystroke_to_send)
# Order matters — more specific patterns first.
PATTERNS = [
    # "Would you like to proceed?" with auto-accept edits option → "2"
    (re.compile(
        r"Would you like to proceed\?.*?1\.\s*Yes.*?auto-accept",
        re.DOTALL,
    ), "2"),
    # "Do you want to ...?" with session-wide allow as option 2 → "2"
    # Covers: proceed, overwrite, make this edit, etc.
    (re.compile(
        r"Do you want to\s+.+?\?.*?1\.\s*Yes\s*\n\s*2\.\s*Yes,\s*allow",
        re.DOTALL,
    ), "2"),
    # "Do you want to ...?" with just Yes/No → "1"
    (re.compile(
        r"Do you want to\s+.+?\?.*?[❯>]\s*1\.\s*Yes",
        re.DOTALL,
    ), "1"),
    # "Allow X?" with Yes option → "1"
    (re.compile(
        r"Allow\s+.+?\?.*?[❯>]\s*1\.\s*Yes",
        re.DOTALL,
    ), "1"),
    # Generic Approve/Confirm/Proceed prompt → "1"
    (re.compile(
        r"(?:Allow|Approve|Confirm|Proceed)\s*\?.*?\n\s*[❯>]\s*1\.\s*Yes\s*\n\s*2\.\s*No",
        re.DOTALL | re.IGNORECASE,
    ), "1"),
]


def run(session_id, tmux):
    """Called by the routine engine.

    Args:
        session_id: the tmux session to check
        tmux: the tmux_manager module (capture_pane, send_keys, is_running)

    Returns:
        dict with status: "approved", "stale", "clear", or "skipped"
    """
    if not tmux.is_running(session_id):
        return {"status": "skipped"}

    content = tmux.capture_pane(session_id)
    recent = "\n".join(content.strip().splitlines()[-50:])

    for pattern, keystroke in PATTERNS:
        if pattern.search(recent):
            # Re-capture to avoid acting on a stale prompt
            fresh = tmux.capture_pane(session_id)
            fresh_recent = "\n".join(fresh.strip().splitlines()[-50:])
            if pattern.search(fresh_recent):
                tmux.send_keys(session_id, keystroke, enter=True)
                return {"status": "approved", "keystroke": keystroke}
            return {"status": "stale"}

    return {"status": "clear"}
