"""Auto-approve Claude Code permission prompts and detect stuck sessions.

Checks the bottom of the pane (last 6 non-empty lines) to avoid false positives
from scroll history. Skips actively working sessions.

Consolidated from vm_manager's chat_monitor.py approach:
- Robust clear-context handling (scans options, never clears context)
- Unsubmitted prompt detection (agent typed but didn't press Enter)
- 0% context detection
- Stale guard (double-check before acting)
"""

import time


ACTIVE_INDICATORS = [
    "Running", "Waiting", "esc to interrupt", "Fetching",
    "Coalescing", "Churning", "Baking", "Mustering",
    "Crunching", "Cooking", "Cogitating", "Fluorescing",
    "Spelunking", "thought for", "Thinking", "Deliberating",
]

CLEAR_CONTEXT_INDICATORS = [
    "clear context", "clear the context", "compact the context",
    "compact context", "work on the plan", "enter plan mode",
    "planning mode",
]

APPROVAL_MARKERS = [
    "Esc to cancel", "Do you want to proceed?", "Do you want to allow",
]


def _get_bottom(content, n=6):
    """Extract last n non-empty lines from pane content."""
    all_lines = content.split("\n")
    last_lines = [l for l in all_lines if l.strip()][-n:]
    return last_lines, "\n".join(last_lines)


def _is_approval_prompt(bottom):
    """Check if the bottom text contains an approval prompt."""
    return (
        any(marker in bottom for marker in APPROVAL_MARKERS)
        or ("❯" in bottom and "(esc)" in bottom)
        or ("❯" in bottom and "ctrl-g to edit" in bottom)
    )


def _is_clear_context_prompt(bottom):
    """Detect if the prompt is about clearing context / entering plan mode."""
    lower = bottom.lower()
    has_clear = any(ind in lower for ind in CLEAR_CONTEXT_INDICATORS)
    has_prompt = any(marker in bottom for marker in [
        "Do you want to", "❯", "Yes", "No", "Esc to cancel",
    ])
    return has_clear and has_prompt


def _find_safe_option(content):
    """Scan options in the pane to find 'accept edits' / 'don't clear' option.

    Returns the number of Down presses needed from the current ❯ position,
    or None if we can't identify the right option.
    """
    lines = content.split("\n")

    # Find option positions (numbered options and the ❯ marker)
    option_positions = []
    cursor_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("1.", "2.", "3.", "4.", "❯")):
            option_positions.append((i, stripped))
            if "❯" in stripped:
                cursor_idx = len(option_positions) - 1

    if cursor_idx is None:
        return None

    # Search for the safe option (accept edits, don't clear)
    accept_idx = None
    no_idx = None
    for pos, (line_num, text) in enumerate(option_positions):
        text_lower = text.lower()
        if any(w in text_lower for w in [
            "accept edit", "auto-accept", "auto accept",
            "don't clear", "without clear",
        ]):
            accept_idx = pos
        elif text_lower.lstrip("0123456789. ❯").strip().lower() in ("no",):
            no_idx = pos

    target = accept_idx if accept_idx is not None else no_idx
    if target is None:
        return None

    return target - cursor_idx


def _handle_clear_context(session_id, tmux, content):
    """Handle clear-context prompt: navigate to the safe option."""
    downs = _find_safe_option(content)

    if downs is not None:
        for _ in range(abs(downs)):
            key = "Down" if downs > 0 else "Up"
            tmux.send_special_key(session_id, key)
            time.sleep(0.1)
        tmux.send_special_key(session_id, "Enter")
        return {"status": "approved", "note": f"clear-context, navigated {downs} to safe option"}

    # Fallback: option 2 is typically "accept edits" — press Down once
    tmux.send_special_key(session_id, "Down")
    time.sleep(0.2)
    tmux.send_special_key(session_id, "Enter")
    return {"status": "approved", "note": "clear-context, fallback to option 2"}


def run(session_id, tmux):
    """Called by the routine engine.

    Args:
        session_id: the tmux session to check
        tmux: the tmux_manager module (capture_pane, send_keys, is_running, etc.)

    Returns:
        dict with status: "approved", "stale", "clear", "skipped",
                          "unsubmitted", or "zero_context"
    """
    if not tmux.is_running(session_id):
        return {"status": "skipped"}

    content = tmux.capture_pane(session_id)
    last_lines, bottom = _get_bottom(content)

    # Skip actively working sessions
    if any(ind in bottom for ind in ACTIVE_INDICATORS):
        return {"status": "clear"}

    # Check for approval prompt
    if _is_approval_prompt(bottom):
        # Stale guard: re-capture to confirm prompt is still there
        fresh = tmux.capture_pane(session_id)
        _, fresh_bottom = _get_bottom(fresh)

        if not _is_approval_prompt(fresh_bottom):
            return {"status": "stale"}

        # Clear-context prompt — never clear, choose accept-edits
        if _is_clear_context_prompt(fresh_bottom):
            return _handle_clear_context(session_id, tmux, fresh)

        # Standard approval — Enter selects the highlighted option
        tmux.send_special_key(session_id, "Enter")
        return {"status": "approved"}

    # Check for 0% context idle sessions
    if "Context left until auto-compact: 0%" in bottom and "? for shortcuts" in bottom:
        return {"status": "zero_context"}

    # Check for unsubmitted typed prompts (agent typed but didn't press Enter)
    if "? for shortcuts" in bottom or "accept edits on" in bottom or "❯" in bottom:
        for line in last_lines:
            if "❯" in line:
                after = line.split("❯", 1)[1].strip()
                if after:
                    tmux.send_special_key(session_id, "Enter")
                    return {"status": "unsubmitted", "note": after[:80]}
                break

    return {"status": "clear"}
