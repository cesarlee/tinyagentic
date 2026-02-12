import json
import os
import tempfile
import threading

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

_lock = threading.Lock()

DEFAULT_CONFIG = {
    "dashboards": {
        "main": {"name": "Main", "sessions": []},
    },
    "active_dashboard": "main",
    "session_vars": [],
    "sessions": {},
    "macros": {},
    "routines": {
        "approval-watcher": {
            "name": "Approval Watcher",
            "script": "approval-watcher",
            "session": "*",
            "interval_seconds": 10,
            "enabled": True,
        },
    },
    "settings": {
        "tmux_prefix": "ta",
        "scripts_dir": "scripts",
    },
}


def _migrate_config(config):
    """Migrate older config formats to current structure."""
    changed = False
    if "dashboards" not in config:
        all_session_ids = list(config.get("sessions", {}).keys())
        config["dashboards"] = {
            "main": {"name": "Main", "sessions": all_session_ids},
        }
        config["active_dashboard"] = "main"
        changed = True
    if "session_vars" not in config:
        config["session_vars"] = []
        changed = True
    # Migrate idle_only → send_when in routines
    for routine in config.get("routines", {}).values():
        if "idle_only" in routine and "send_when" not in routine:
            routine["send_when"] = "idle" if routine.pop("idle_only") else "always"
            changed = True
    if changed:
        save_config(config)
    return config


def load_config():
    with _lock:
        if not os.path.exists(CONFIG_PATH):
            _save_unlocked(DEFAULT_CONFIG)
            return json.loads(json.dumps(DEFAULT_CONFIG))
        with open(CONFIG_PATH, "r") as f:
            config = json.load(f)
    return _migrate_config(config)


def save_config(config):
    with _lock:
        _save_unlocked(config)


def _save_unlocked(config):
    """Write config atomically: write to temp file then rename."""
    dir_name = os.path.dirname(CONFIG_PATH)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(config, f, indent=2, sort_keys=False)
        os.replace(tmp_path, CONFIG_PATH)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
