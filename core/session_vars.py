"""Session Vars — global custom variable definitions for sessions."""

from .config import load_config, save_config


def list_session_vars():
    """Return the list of global var definitions."""
    config = load_config()
    return config.get("session_vars", [])


def add_session_var(name, label=""):
    """Add a new global var definition."""
    config = load_config()
    sv = config.setdefault("session_vars", [])
    for v in sv:
        if v["name"] == name:
            raise ValueError(f"Session var '{name}' already exists")
    sv.append({"name": name, "label": label or name})
    save_config(config)


def update_session_var(name, **kwargs):
    """Update label or other properties of a global var definition."""
    config = load_config()
    for v in config.get("session_vars", []):
        if v["name"] == name:
            v.update(kwargs)
            save_config(config)
            return
    raise ValueError(f"Session var '{name}' not found")


def delete_session_var(name):
    """Remove a global var definition."""
    config = load_config()
    sv = config.get("session_vars", [])
    config["session_vars"] = [v for v in sv if v["name"] != name]
    save_config(config)
