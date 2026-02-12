"""Prompt an idle Claude session to check team chat.

If the session has a typed-but-unsubmitted self-prompt, submit it.
If the session is at an empty prompt, inject the check-chat prompt.
Skips sessions that are actively working, thinking, or on approval prompts.
"""

# Indicators that Claude is actively working (don't interrupt).
# NOTE: "Sautéed", "Brewed", "Worked" are COMPLETION indicators (past tense),
# meaning the agent finished — they should NOT be here.
ACTIVE_INDICATORS = [
    "Running", "Waiting", "esc to interrupt", "Fetching",
    "Coalescing", "Churning", "Baking", "Mustering",
    "Crunching", "Cooking", "Cogitating", "Fluorescing",
    "Spelunking", "thought for", "Thinking", "Deliberating",
]

PROMPT_TEXT = (
    "Please check recent chat messages. Please post responses or updates "
    "as necessary and constructive to a team chat discussion. Do not send "
    "a message if it does not add to the conversation meaningfully."
)


def run(session_id, tmux):
    """Called by the routine engine.

    Returns:
        dict with status: "sent", "submitted", "skipped", + reason.
    """
    if not tmux.is_running(session_id):
        return {"status": "skipped"}

    content = tmux.capture_pane(session_id)
    all_lines = content.split("\n")
    last_lines = [l for l in all_lines if l.strip()][-6:]
    bottom = "\n".join(last_lines)

    # Skip if actively working
    if any(ind in bottom for ind in ACTIVE_INDICATORS):
        return {"status": "skipped", "reason": "active"}

    # Skip if on an approval prompt
    if any(marker in bottom for marker in
           ["Esc to cancel", "Do you want to", "(esc)", "ctrl-g to edit"]):
        return {"status": "skipped", "reason": "approval_prompt"}

    # Must be at an idle prompt: look for "? for shortcuts" or "accept edits on"
    at_prompt = "? for shortcuts" in bottom or "accept edits on" in bottom
    if not at_prompt:
        return {"status": "skipped", "reason": "not_at_prompt"}

    # Check if there's already text typed in the input line
    for line in last_lines:
        if "❯" in line:
            after = line.split("❯", 1)[1].strip()
            if after:
                # Agent has a self-prompt typed but not submitted — press Enter
                tmux.send_special_key(session_id, "Enter")
                return {"status": "submitted", "reason": "had_typed_text", "text": after[:80]}
            break

    # Empty prompt — inject check-chat prompt
    tmux.send_keys(session_id, PROMPT_TEXT, enter=True)
    return {"status": "sent"}
