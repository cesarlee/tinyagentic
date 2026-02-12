"""Session CRUD — manages terminal session entries in config."""

from .config import load_config, save_config


def create_session(session_id, name=None, working_dir=None, dashboard_id=None, vars=None):
    config = load_config()

    if session_id in config["sessions"]:
        raise ValueError(f"Session '{session_id}' already exists")

    session = {
        "name": name or session_id.replace("-", " ").title(),
        "working_dir": working_dir,
    }
    if vars:
        session["vars"] = vars
    config["sessions"][session_id] = session
    # Add to the specified or active dashboard
    target_db = dashboard_id or config.get("active_dashboard", "main")
    dashboards = config.get("dashboards", {})
    if target_db in dashboards:
        if session_id not in dashboards[target_db]["sessions"]:
            dashboards[target_db]["sessions"].append(session_id)
    save_config(config)
    return config["sessions"][session_id]


def update_session(session_id, **kwargs):
    config = load_config()
    if session_id not in config["sessions"]:
        raise ValueError(f"Session '{session_id}' not found")
    config["sessions"][session_id].update(kwargs)
    save_config(config)
    return config["sessions"][session_id]


def delete_session(session_id):
    config = load_config()
    if session_id not in config["sessions"]:
        raise ValueError(f"Session '{session_id}' not found")
    del config["sessions"][session_id]
    # Remove from all dashboards
    for db in config.get("dashboards", {}).values():
        if session_id in db.get("sessions", []):
            db["sessions"].remove(session_id)
    save_config(config)


def get_session(session_id):
    config = load_config()
    return config["sessions"].get(session_id)


def list_sessions():
    config = load_config()
    return config["sessions"]


def list_dashboard_sessions(dashboard_id=None):
    """Return sessions belonging to a specific dashboard, in dashboard order."""
    config = load_config()
    if dashboard_id is None:
        dashboard_id = config.get("active_dashboard", "main")
    dashboard = config.get("dashboards", {}).get(dashboard_id, {})
    session_ids = dashboard.get("sessions", [])
    all_sessions = config.get("sessions", {})
    return {sid: all_sessions[sid] for sid in session_ids if sid in all_sessions}
