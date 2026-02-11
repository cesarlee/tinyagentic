import json
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

DEFAULT_CONFIG = {
    "dashboards": {
        "main": {"name": "Main", "sessions": []},
    },
    "active_dashboard": "main",
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
    if "dashboards" not in config:
        all_session_ids = list(config.get("sessions", {}).keys())
        config["dashboards"] = {
            "main": {"name": "Main", "sessions": all_session_ids},
        }
        config["active_dashboard"] = "main"
        save_config(config)
    return config


def load_config():
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)
    return _migrate_config(config)


def save_config(config):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2, sort_keys=False)
