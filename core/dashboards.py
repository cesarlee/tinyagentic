"""Dashboard CRUD — groups of sessions for the TUI."""

from .config import load_config, save_config


def create_dashboard(dashboard_id, name=None, sessions=None):
    config = load_config()
    dashboards = config.setdefault("dashboards", {})
    if dashboard_id in dashboards:
        raise ValueError(f"Dashboard '{dashboard_id}' already exists")
    dashboards[dashboard_id] = {
        "name": name or dashboard_id.replace("-", " ").title(),
        "sessions": sessions or [],
    }
    save_config(config)
    return dashboards[dashboard_id]


def update_dashboard(dashboard_id, **kwargs):
    config = load_config()
    dashboards = config.get("dashboards", {})
    if dashboard_id not in dashboards:
        raise ValueError(f"Dashboard '{dashboard_id}' not found")
    dashboards[dashboard_id].update(kwargs)
    save_config(config)
    return dashboards[dashboard_id]


def delete_dashboard(dashboard_id):
    config = load_config()
    dashboards = config.get("dashboards", {})
    if dashboard_id not in dashboards:
        raise ValueError(f"Dashboard '{dashboard_id}' not found")
    if len(dashboards) <= 1:
        raise ValueError("Cannot delete the last dashboard")
    del dashboards[dashboard_id]
    if config.get("active_dashboard") == dashboard_id:
        config["active_dashboard"] = next(iter(dashboards))
    save_config(config)


def get_dashboard(dashboard_id):
    config = load_config()
    return config.get("dashboards", {}).get(dashboard_id)


def list_dashboards():
    config = load_config()
    return config.get("dashboards", {})


def get_active_dashboard_id():
    config = load_config()
    active = config.get("active_dashboard", "main")
    dashboards = config.get("dashboards", {})
    if active not in dashboards and dashboards:
        active = next(iter(dashboards))
    return active


def set_active_dashboard(dashboard_id):
    config = load_config()
    if dashboard_id not in config.get("dashboards", {}):
        raise ValueError(f"Dashboard '{dashboard_id}' not found")
    config["active_dashboard"] = dashboard_id
    save_config(config)


def add_session_to_dashboard(dashboard_id, session_id):
    config = load_config()
    dashboard = config.get("dashboards", {}).get(dashboard_id)
    if not dashboard:
        raise ValueError(f"Dashboard '{dashboard_id}' not found")
    if session_id not in dashboard["sessions"]:
        dashboard["sessions"].append(session_id)
        save_config(config)


def remove_session_from_dashboard(dashboard_id, session_id):
    config = load_config()
    dashboard = config.get("dashboards", {}).get(dashboard_id)
    if not dashboard:
        raise ValueError(f"Dashboard '{dashboard_id}' not found")
    if session_id in dashboard["sessions"]:
        dashboard["sessions"].remove(session_id)
        save_config(config)


def get_session_dashboard(session_id):
    """Return the dashboard ID that contains this session, or None."""
    config = load_config()
    for did, db in config.get("dashboards", {}).items():
        if session_id in db.get("sessions", []):
            return did
    return None


def move_session_to_dashboard(session_id, new_dashboard_id):
    """Move a session from its current dashboard to a new one."""
    config = load_config()
    dashboards = config.get("dashboards", {})
    if new_dashboard_id not in dashboards:
        raise ValueError(f"Dashboard '{new_dashboard_id}' not found")
    # Remove from current dashboard
    for db in dashboards.values():
        if session_id in db.get("sessions", []):
            db["sessions"].remove(session_id)
    # Add to new dashboard
    if session_id not in dashboards[new_dashboard_id]["sessions"]:
        dashboards[new_dashboard_id]["sessions"].append(session_id)
    save_config(config)
