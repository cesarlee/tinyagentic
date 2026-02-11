"""Script library — CRUD and execution of Python scripts that run against sessions."""

import importlib.util
import os
import sys

from .config import BASE_DIR

SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")


def list_scripts():
    """List available script names (without .py extension)."""
    if not os.path.exists(SCRIPTS_DIR):
        os.makedirs(SCRIPTS_DIR, exist_ok=True)
        return []
    return sorted(
        f[:-3] for f in os.listdir(SCRIPTS_DIR)
        if f.endswith(".py") and not f.startswith("_")
    )


def load_script(name):
    """Load a script's source code as a string."""
    path = os.path.join(SCRIPTS_DIR, f"{name}.py")
    if not os.path.exists(path):
        raise ValueError(f"Script '{name}' not found")
    with open(path, "r") as f:
        return f.read()


def save_script(name, content):
    """Write script content to file."""
    os.makedirs(SCRIPTS_DIR, exist_ok=True)
    path = os.path.join(SCRIPTS_DIR, f"{name}.py")
    with open(path, "w") as f:
        f.write(content)


def delete_script(name):
    """Delete a script file."""
    path = os.path.join(SCRIPTS_DIR, f"{name}.py")
    if not os.path.exists(path):
        raise ValueError(f"Script '{name}' not found")
    os.unlink(path)


def run_script(name, session_id):
    """Execute a script's run() function against a session.

    The script's run(session_id, tmux) receives:
    - session_id: the ID of the session to operate on
    - tmux: the tmux_manager module (has capture_pane, send_keys, is_running, etc.)

    Returns whatever the script's run() returns (typically a dict).
    """
    from . import tmux_manager

    path = os.path.join(SCRIPTS_DIR, f"{name}.py")
    if not os.path.exists(path):
        raise ValueError(f"Script '{name}' not found")

    spec = importlib.util.spec_from_file_location(f"scripts.{name}", path)
    module = importlib.util.module_from_spec(spec)

    # Don't pollute sys.modules permanently
    old = sys.modules.get(f"scripts.{name}")
    try:
        spec.loader.exec_module(module)
        if not hasattr(module, "run"):
            raise ValueError(f"Script '{name}' has no run() function")
        return module.run(session_id, tmux_manager)
    finally:
        if old is None:
            sys.modules.pop(f"scripts.{name}", None)
        else:
            sys.modules[f"scripts.{name}"] = old
