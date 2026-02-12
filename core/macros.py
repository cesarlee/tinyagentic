"""Macro CRUD — predefined key sequences to send to sessions."""

from core.config import load_config, save_config
from core.sessions import get_session


def create_macro(macro_id: str, name: str, keys: str, enter: bool = True,
                 send_when: str = "always") -> dict:
    config = load_config()
    macros = config.setdefault("macros", {})
    if macro_id in macros:
        raise ValueError(f"Macro '{macro_id}' already exists")
    macros[macro_id] = {"name": name, "keys": keys, "enter": enter, "send_when": send_when}
    save_config(config)
    return macros[macro_id]


def update_macro(macro_id: str, **kwargs) -> dict:
    config = load_config()
    macros = config.setdefault("macros", {})
    if macro_id not in macros:
        raise ValueError(f"Macro '{macro_id}' not found")
    for key in ("name", "keys", "enter", "send_when"):
        if key in kwargs:
            macros[macro_id][key] = kwargs[key]
    save_config(config)
    return macros[macro_id]


def delete_macro(macro_id: str) -> None:
    config = load_config()
    macros = config.setdefault("macros", {})
    if macro_id not in macros:
        raise ValueError(f"Macro '{macro_id}' not found")
    del macros[macro_id]
    save_config(config)


def get_macro(macro_id: str) -> dict | None:
    config = load_config()
    return config.get("macros", {}).get(macro_id)


def list_macros() -> dict:
    config = load_config()
    return config.get("macros", {})


def substitute_vars(text: str, session_id: str) -> str:
    """Replace $var_name placeholders with session var values."""
    session = get_session(session_id)
    if not session:
        return text
    session_vars = session.get("vars", {})
    for name, value in session_vars.items():
        text = text.replace(f"${name}", str(value))
    return text
