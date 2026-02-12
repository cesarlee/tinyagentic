"""Auto-approve Claude Code permission prompts.

Only checks the bottom of the pane (last 4-6 non-empty lines) to avoid
false positives from scroll history. Skips actively working sessions.
"""


ACTIVE_INDICATORS = [
    "Running", "Waiting", "esc to interrupt", "Fetching",
    "Coalescing", "Churning", "Baking", "Mustering",
    "Crunching", "Cooking", "Cogitating", "Fluorescing",
    "Spelunking", "thought for", "Thinking", "Deliberating",
]

CLEAR_CONTEXT_INDICATORS = [
    "clear context", "clear the context", "compact the context",
    "compact context", "work on the plan", "enter plan mode",
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
    all_lines = content.split("\n")
    last_lines = [l for l in all_lines if l.strip()][-6:]
    bottom = "\n".join(last_lines)

    # Skip actively working sessions
    if any(ind in bottom for ind in ACTIVE_INDICATORS):
        return {"status": "clear"}

    # Detect approval prompt at the bottom
    is_prompt = (
        "Esc to cancel" in bottom
        or "Do you want to proceed?" in bottom
        or "Do you want to allow" in bottom
        or ("❯" in bottom and "(esc)" in bottom)
        or ("❯" in bottom and "ctrl-g to edit" in bottom)
    )

    if not is_prompt:
        return {"status": "clear"}

    # Double-check (stale guard)
    fresh = tmux.capture_pane(session_id)
    fresh_lines = [l for l in fresh.split("\n") if l.strip()][-6:]
    fresh_bottom = "\n".join(fresh_lines)
    if not any(marker in fresh_bottom for marker in
               ["Esc to cancel", "Do you want to", "(esc)", "ctrl-g to edit"]):
        return {"status": "stale"}

    # Clear-context prompt — select option 2 (accept edits, don't clear)
    lower = fresh_bottom.lower()
    if any(ind in lower for ind in CLEAR_CONTEXT_INDICATORS):
        tmux.send_special_key(session_id, "Down")
        import time; time.sleep(0.2)
        tmux.send_special_key(session_id, "Enter")
        return {"status": "approved", "note": "clear-context, chose option 2"}

    # Standard approval — Enter selects the highlighted option
    tmux.send_special_key(session_id, "Enter")
    return {"status": "approved"}
