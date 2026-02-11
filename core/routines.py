"""Routines engine — periodic execution of scripts or macros against sessions."""

import time
import threading
from datetime import datetime
from .config import load_config, save_config
from .scripts import run_script
from .macros import get_macro
from .sessions import list_sessions
from .tmux_manager import is_running, send_keys, send_text_block, get_foreground_process


def create_routine(routine_id, name, script=None, macro=None, session="*",
                   interval_seconds=30, enabled=True, idle_only=True,
                   run_when_process=""):
    config = load_config()
    if routine_id in config["routines"]:
        raise ValueError(f"Routine '{routine_id}' already exists")
    routine = {
        "name": name,
        "type": "macro" if macro else "script",
        "session": session,
        "interval_seconds": interval_seconds,
        "enabled": enabled,
        "idle_only": idle_only,
    }
    if macro:
        routine["macro"] = macro
    else:
        routine["script"] = script
    if run_when_process:
        routine["run_when_process"] = run_when_process
    config["routines"][routine_id] = routine
    save_config(config)
    return routine


def update_routine(routine_id, **kwargs):
    config = load_config()
    if routine_id not in config["routines"]:
        raise ValueError(f"Routine '{routine_id}' not found")
    config["routines"][routine_id].update(kwargs)
    save_config(config)
    return config["routines"][routine_id]


def delete_routine(routine_id):
    config = load_config()
    if routine_id not in config["routines"]:
        raise ValueError(f"Routine '{routine_id}' not found")
    del config["routines"][routine_id]
    save_config(config)


def list_routines():
    config = load_config()
    return config["routines"]


def _execute_macro(macro_id, session_id):
    """Send a macro's keys to a session. Returns a status dict."""
    m = get_macro(macro_id)
    if not m:
        raise ValueError(f"Macro '{macro_id}' not found")
    keys = m["keys"]
    do_enter = m.get("enter", True)
    if "\n" in keys:
        send_text_block(session_id, keys)
    else:
        send_keys(session_id, keys, enter=False)
    if do_enter:
        send_keys(session_id, "", enter=True)
    return {"status": "sent", "macro": macro_id}


class RoutinesDaemon:
    """Background daemon that runs routines at their configured intervals."""

    def __init__(self):
        self._running = False
        self._thread = None
        self._log = []
        self._last_run = {}      # routine_id → timestamp
        self._next_run = {}      # routine_id → timestamp (for UI countdown)
        self._cooldowns = {}     # (routine_id, session_id) → timestamp
        self.idle_checker = None  # callback(session_id) → bool

    def start(self) -> bool:
        if self._running:
            return False
        self._running = True
        self._thread = threading.Thread(
            target=self._main_loop,
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self):
        self._running = False
        self._thread = None

    @property
    def is_active(self) -> bool:
        return self._running

    def get_countdown(self, routine_id) -> float | None:
        """Seconds until next run. None if not scheduled."""
        nxt = self._next_run.get(routine_id)
        if nxt is None:
            return None
        return max(0.0, nxt - time.time())

    def _is_idle(self, session_id) -> bool:
        """Check if a session is idle via the registered callback."""
        if self.idle_checker:
            try:
                return self.idle_checker(session_id)
            except Exception:
                return True  # assume idle if check fails
        return True  # assume idle if no checker registered

    def _main_loop(self):
        while self._running:
            routines = list_routines()

            for rid, routine in routines.items():
                if not self._running:
                    break
                if not routine.get("enabled", False):
                    continue

                interval = routine.get("interval_seconds", 30)
                last = self._last_run.get(rid, 0)
                now = time.time()

                # Set next_run for UI
                if rid not in self._next_run:
                    self._next_run[rid] = now + interval

                if now - last < interval:
                    continue

                self._last_run[rid] = now
                self._next_run[rid] = now + interval

                routine_type = routine.get("type", "script")
                target_session = routine.get("session", "*")
                idle_only = routine.get("idle_only", False)

                if target_session == "*":
                    session_ids = [
                        sid for sid in list_sessions()
                        if is_running(sid)
                    ]
                else:
                    session_ids = [target_session] if is_running(target_session) else []

                for sid in session_ids:
                    if not self._running:
                        break

                    # Skip non-idle sessions if idle_only is set
                    if idle_only and not self._is_idle(sid):
                        continue

                    # Skip if required process is not running in session
                    run_when = routine.get("run_when_process", "")
                    if run_when:
                        fg = get_foreground_process(sid)
                        if fg is None or run_when.lower() not in fg.lower():
                            continue

                    ts = datetime.now().strftime("%H:%M:%S")
                    try:
                        if routine_type == "macro":
                            macro_id = routine.get("macro")
                            result = _execute_macro(macro_id, sid)
                        else:
                            script_name = routine.get("script")
                            result = run_script(script_name, sid)

                        status = result.get("status", "?") if isinstance(result, dict) else str(result)
                        if status not in ("clear", "skipped"):
                            msg = f"[{ts}] {rid}@{sid}: {status}"
                            self._log.append(msg)
                    except Exception as e:
                        msg = f"[{ts}] {rid}@{sid}: error — {e}"
                        self._log.append(msg)

            # Sleep in 1s increments for responsive stop
            for _ in range(1):
                if not self._running:
                    break
                time.sleep(1)
