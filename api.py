#!/usr/bin/env python3
"""TinyAgentic.ai — REST API + WebSocket server."""

import asyncio
import re
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query
from websockets.exceptions import ConnectionClosedOK, ConnectionClosedError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core.sessions import (
    create_session,
    update_session,
    delete_session,
    get_session,
    list_sessions,
    list_dashboard_sessions,
)
from core.dashboards import (
    create_dashboard,
    update_dashboard,
    delete_dashboard,
    get_dashboard,
    list_dashboards,
    get_active_dashboard_id,
    set_active_dashboard,
    get_session_dashboard,
    move_session_to_dashboard,
)
from core.macros import (
    create_macro,
    update_macro,
    delete_macro,
    get_macro,
    list_macros,
    substitute_vars,
)
from core.routines import (
    create_routine,
    update_routine,
    delete_routine,
    list_routines,
    RoutinesDaemon,
)
from core.scripts import list_scripts, load_script, save_script, delete_script, run_script
from core.session_vars import (
    list_session_vars,
    add_session_var,
    update_session_var,
    delete_session_var,
)
from core.tmux_manager import (
    start_session as tmux_start,
    stop_session as tmux_stop,
    is_running,
    capture_pane,
    send_keys,
    send_special_key,
    send_text_block,
    get_foreground_process,
    claude_ready,
)


# ── Pydantic Models ──────────────────────────────────────────────────


class SessionCreate(BaseModel):
    id: str
    name: Optional[str] = None
    working_dir: Optional[str] = None
    dashboard_id: Optional[str] = None
    vars: Optional[dict[str, str]] = None


class SessionUpdate(BaseModel):
    name: Optional[str] = None
    working_dir: Optional[str] = None
    vars: Optional[dict[str, str]] = None


class DashboardCreate(BaseModel):
    id: str
    name: Optional[str] = None
    sessions: Optional[list[str]] = None


class DashboardUpdate(BaseModel):
    name: Optional[str] = None


class SetActiveDashboard(BaseModel):
    id: str


class MoveDashboard(BaseModel):
    new_dashboard_id: str


class MacroCreate(BaseModel):
    id: str
    name: str
    keys: str
    enter: bool = True
    send_when: str = "always"


class MacroUpdate(BaseModel):
    name: Optional[str] = None
    keys: Optional[str] = None
    enter: Optional[bool] = None
    send_when: Optional[str] = None


class RoutineCreate(BaseModel):
    id: str
    name: str
    script: Optional[str] = None
    macro: Optional[str] = None
    session: str = "*"
    interval_seconds: int = 30
    enabled: bool = True
    send_when: str = "idle"
    run_when_process: str = ""


class RoutineUpdate(BaseModel):
    name: Optional[str] = None
    script: Optional[str] = None
    macro: Optional[str] = None
    session: Optional[str] = None
    interval_seconds: Optional[int] = None
    enabled: Optional[bool] = None
    send_when: Optional[str] = None
    run_when_process: Optional[str] = None


class ScriptSave(BaseModel):
    content: str


class SessionVarCreate(BaseModel):
    name: str
    label: str = ""


class SessionVarUpdate(BaseModel):
    label: Optional[str] = None


class SendKeysRequest(BaseModel):
    keys: str
    enter: bool = True


class SendSpecialKeyRequest(BaseModel):
    key: str


class SendTextBlockRequest(BaseModel):
    text: str


# ── Health Detection (replaces TUI panel widgets) ────────────────────

IDLE_THRESHOLD = 30.0

_WORKING_INDICATORS = [
    "Running", "esc to interrupt", "Fetching", "Coalescing",
    "Churning", "Baking", "Mustering", "Crunching", "Cooking",
    "Cogitating", "Fluorescing", "Spelunking",
]
_THINKING_INDICATORS = ["thought for", "Thinking", "Deliberating"]


def _parse_claude_health(text: str) -> str:
    """Derive Claude Code health state from pane output.

    Returns: working, thinking, waiting, typed, idle, low_ctx, zero_ctx,
    or empty string if no Claude-specific state detected.
    """
    lines = text.split("\n")
    tail = [l for l in lines if l.strip()][-6:]
    if not tail:
        return ""
    bottom = "\n".join(tail)

    if "Context left until auto-compact: 0%" in bottom and "? for shortcuts" in bottom:
        return "zero_ctx"

    ctx_match = re.search(r"Context left until auto-compact:\s*(\d+)%", bottom)
    if ctx_match and int(ctx_match.group(1)) < 15 and "? for shortcuts" in bottom:
        return "low_ctx"

    if any(ind in bottom for ind in _WORKING_INDICATORS):
        return "working"

    if any(ind in bottom for ind in _THINKING_INDICATORS):
        return "thinking"

    if (
        "Esc to cancel" in bottom
        or "Do you want to proceed?" in bottom
        or "Do you want to allow" in bottom
        or ("\u276f" in bottom and "(esc)" in bottom)
        or ("\u276f" in bottom and "ctrl-g to edit" in bottom)
    ):
        return "waiting"

    if "? for shortcuts" in bottom or "accept edits on" in bottom or "\u276f" in bottom:
        for line in tail:
            if "\u276f" in line:
                after = line.split("\u276f", 1)[1].strip()
                if after:
                    return "typed"
                break

    if "? for shortcuts" in bottom:
        return "idle"

    return ""


class IdleTracker:
    """Tracks session health states without TUI panel widgets."""

    def __init__(self):
        self._states: dict[str, str] = {}
        self._last_content: dict[str, str] = {}
        self._last_changed: dict[str, float] = {}
        self._task: asyncio.Task | None = None

    def start(self):
        self._task = asyncio.create_task(self._loop())

    def stop(self):
        if self._task:
            self._task.cancel()

    def get_health(self, session_id: str) -> str:
        return self._states.get(session_id, "stopped")

    def is_idle(self, session_id: str) -> bool:
        return self.get_health(session_id) not in ("working", "thinking")

    async def _loop(self):
        while True:
            try:
                for sid in list_sessions():
                    if is_running(sid):
                        content = capture_pane(sid, lines=20)
                        prev = self._last_content.get(sid, "")
                        if content != prev:
                            self._last_changed[sid] = time.monotonic()
                            self._last_content[sid] = content
                        health = _parse_claude_health(content)
                        if health:
                            self._states[sid] = health
                        else:
                            elapsed = time.monotonic() - self._last_changed.get(sid, 0)
                            self._states[sid] = "idle" if elapsed >= IDLE_THRESHOLD else "working"
                    else:
                        self._states[sid] = "stopped"
                        self._last_content.pop(sid, None)
            except Exception:
                pass
            await asyncio.sleep(3.0)


# ── App State & Lifespan ─────────────────────────────────────────────

daemon = RoutinesDaemon()
idle_tracker = IdleTracker()


@asynccontextmanager
async def lifespan(app: FastAPI):
    idle_tracker.start()
    daemon.idle_checker = idle_tracker.is_idle
    daemon.interactive_checker = lambda sid: False
    daemon.start()
    yield
    daemon.stop()
    idle_tracker.stop()


app = FastAPI(title="TinyAgentic.ai", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Helpers ───────────────────────────────────────────────────────────


def _enrich_session(session_id: str, data: dict) -> dict:
    running = is_running(session_id)
    return {
        "id": session_id,
        "name": data.get("name", session_id),
        "working_dir": data.get("working_dir"),
        "vars": data.get("vars", {}),
        "running": running,
        "health": idle_tracker.get_health(session_id) if running else "stopped",
    }


def _enrich_routine(routine_id: str, data: dict) -> dict:
    return {
        "id": routine_id,
        **data,
        "countdown": daemon.get_countdown(routine_id),
    }


# ── Sessions ──────────────────────────────────────────────────────────


@app.get("/sessions")
def list_sessions_endpoint():
    return {sid: _enrich_session(sid, s) for sid, s in list_sessions().items()}


@app.get("/sessions/{session_id}")
def get_session_endpoint(session_id: str):
    s = get_session(session_id)
    if not s:
        raise HTTPException(404, f"Session '{session_id}' not found")
    return _enrich_session(session_id, s)


@app.post("/sessions", status_code=201)
def create_session_endpoint(body: SessionCreate):
    try:
        data = create_session(
            body.id, name=body.name, working_dir=body.working_dir,
            dashboard_id=body.dashboard_id, vars=body.vars,
        )
        return _enrich_session(body.id, data)
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.patch("/sessions/{session_id}")
def update_session_endpoint(session_id: str, body: SessionUpdate):
    updates = body.dict(exclude_none=True)
    if not updates:
        raise HTTPException(422, "No fields to update")
    try:
        data = update_session(session_id, **updates)
        return _enrich_session(session_id, data)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.delete("/sessions/{session_id}")
def delete_session_endpoint(session_id: str):
    try:
        delete_session(session_id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(404, str(e))


# ── Tmux Operations ──────────────────────────────────────────────────


@app.post("/sessions/{session_id}/start")
def start_session_endpoint(session_id: str):
    s = get_session(session_id)
    if not s:
        raise HTTPException(404, f"Session '{session_id}' not found")
    try:
        tmux_name = tmux_start(session_id)
        return {"tmux_name": tmux_name}
    except (ValueError, RuntimeError) as e:
        raise HTTPException(422, str(e))


@app.post("/sessions/{session_id}/stop")
def stop_session_endpoint(session_id: str):
    try:
        tmux_stop(session_id)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(422, str(e))


@app.get("/sessions/{session_id}/running")
def session_running_endpoint(session_id: str):
    return {"running": is_running(session_id)}


@app.get("/sessions/{session_id}/capture")
def capture_pane_endpoint(session_id: str, lines: int = Query(20)):
    if not is_running(session_id):
        raise HTTPException(422, f"Session '{session_id}' is not running")
    try:
        content = capture_pane(session_id, lines=lines)
        return {"content": content, "health": idle_tracker.get_health(session_id)}
    except RuntimeError as e:
        raise HTTPException(500, str(e))


@app.post("/sessions/{session_id}/send-keys")
def send_keys_endpoint(session_id: str, body: SendKeysRequest):
    if not is_running(session_id):
        raise HTTPException(422, f"Session '{session_id}' is not running")
    send_keys(session_id, body.keys, enter=body.enter)
    return {"ok": True}


@app.post("/sessions/{session_id}/send-special-key")
def send_special_key_endpoint(session_id: str, body: SendSpecialKeyRequest):
    if not is_running(session_id):
        raise HTTPException(422, f"Session '{session_id}' is not running")
    send_special_key(session_id, body.key)
    return {"ok": True}


@app.post("/sessions/{session_id}/send-text-block")
def send_text_block_endpoint(session_id: str, body: SendTextBlockRequest):
    if not is_running(session_id):
        raise HTTPException(422, f"Session '{session_id}' is not running")
    send_text_block(session_id, body.text)
    return {"ok": True}


@app.get("/sessions/{session_id}/process")
def session_process_endpoint(session_id: str):
    return {"process": get_foreground_process(session_id)}


@app.get("/sessions/{session_id}/claude-ready")
def claude_ready_endpoint(session_id: str):
    return claude_ready(session_id)


@app.get("/sessions/{session_id}/dashboard")
def session_dashboard_endpoint(session_id: str):
    return {"dashboard_id": get_session_dashboard(session_id)}


@app.put("/sessions/{session_id}/dashboard")
def move_session_dashboard_endpoint(session_id: str, body: MoveDashboard):
    try:
        move_session_to_dashboard(session_id, body.new_dashboard_id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(422, str(e))


# ── Dashboards ────────────────────────────────────────────────────────


@app.get("/dashboards/active")
def get_active_dashboard():
    return {"id": get_active_dashboard_id()}


@app.put("/dashboards/active")
def set_active_dashboard_endpoint(body: SetActiveDashboard):
    set_active_dashboard(body.id)
    return {"ok": True}


@app.get("/dashboards")
def list_dashboards_endpoint():
    dbs = list_dashboards()
    return {did: {"id": did, **db} for did, db in dbs.items()}


@app.get("/dashboards/{dashboard_id}")
def get_dashboard_endpoint(dashboard_id: str):
    db = get_dashboard(dashboard_id)
    if not db:
        raise HTTPException(404, f"Dashboard '{dashboard_id}' not found")
    return {"id": dashboard_id, **db}


@app.get("/dashboards/{dashboard_id}/sessions")
def list_dashboard_sessions_endpoint(dashboard_id: str):
    sessions = list_dashboard_sessions(dashboard_id)
    return {sid: _enrich_session(sid, s) for sid, s in sessions.items()}


@app.post("/dashboards", status_code=201)
def create_dashboard_endpoint(body: DashboardCreate):
    try:
        data = create_dashboard(body.id, name=body.name, sessions=body.sessions)
        return {"id": body.id, **data}
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.patch("/dashboards/{dashboard_id}")
def update_dashboard_endpoint(dashboard_id: str, body: DashboardUpdate):
    updates = body.dict(exclude_none=True)
    if not updates:
        raise HTTPException(422, "No fields to update")
    try:
        data = update_dashboard(dashboard_id, **updates)
        return {"id": dashboard_id, **data}
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.delete("/dashboards/{dashboard_id}")
def delete_dashboard_endpoint(dashboard_id: str):
    try:
        delete_dashboard(dashboard_id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(422, str(e))


# ── Macros ────────────────────────────────────────────────────────────


@app.get("/macros")
def list_macros_endpoint():
    macros = list_macros()
    return {mid: {"id": mid, **m} for mid, m in macros.items()}


@app.get("/macros/{macro_id}")
def get_macro_endpoint(macro_id: str):
    m = get_macro(macro_id)
    if not m:
        raise HTTPException(404, f"Macro '{macro_id}' not found")
    return {"id": macro_id, **m}


@app.post("/macros", status_code=201)
def create_macro_endpoint(body: MacroCreate):
    try:
        data = create_macro(body.id, body.name, body.keys,
                            enter=body.enter, send_when=body.send_when)
        return {"id": body.id, **data}
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.patch("/macros/{macro_id}")
def update_macro_endpoint(macro_id: str, body: MacroUpdate):
    updates = body.dict(exclude_none=True)
    if not updates:
        raise HTTPException(422, "No fields to update")
    try:
        data = update_macro(macro_id, **updates)
        return {"id": macro_id, **data}
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.delete("/macros/{macro_id}")
def delete_macro_endpoint(macro_id: str):
    try:
        delete_macro(macro_id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/macros/{macro_id}/send/{session_id}")
def send_macro_endpoint(macro_id: str, session_id: str):
    m = get_macro(macro_id)
    if not m:
        raise HTTPException(404, f"Macro '{macro_id}' not found")
    if not is_running(session_id):
        raise HTTPException(422, f"Session '{session_id}' is not running")

    send_when = m.get("send_when", "always")
    if send_when == "idle" and not idle_tracker.is_idle(session_id):
        raise HTTPException(409, "Session is not idle")
    if send_when == "claude_ready":
        check = claude_ready(session_id)
        if not check["ready"]:
            raise HTTPException(409, f"Claude not ready: {check['reason']}")

    keys = substitute_vars(m["keys"], session_id)
    do_enter = m.get("enter", True)
    if "\n" in keys:
        send_text_block(session_id, keys)
    else:
        send_keys(session_id, keys, enter=False)
    if do_enter:
        send_keys(session_id, "", enter=True)
    return {"ok": True}


# ── Routines ──────────────────────────────────────────────────────────


@app.get("/routines")
def list_routines_endpoint():
    routines = list_routines()
    return {rid: _enrich_routine(rid, r) for rid, r in routines.items()}


@app.post("/routines", status_code=201)
def create_routine_endpoint(body: RoutineCreate):
    try:
        data = create_routine(
            body.id, body.name, script=body.script, macro=body.macro,
            session=body.session, interval_seconds=body.interval_seconds,
            enabled=body.enabled, send_when=body.send_when,
            run_when_process=body.run_when_process,
        )
        return _enrich_routine(body.id, data)
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.patch("/routines/{routine_id}")
def update_routine_endpoint(routine_id: str, body: RoutineUpdate):
    updates = body.dict(exclude_none=True)
    if not updates:
        raise HTTPException(422, "No fields to update")
    try:
        data = update_routine(routine_id, **updates)
        return _enrich_routine(routine_id, data)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.delete("/routines/{routine_id}")
def delete_routine_endpoint(routine_id: str):
    try:
        delete_routine(routine_id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(404, str(e))


# ── Daemon ────────────────────────────────────────────────────────────


@app.get("/daemon/status")
def daemon_status():
    return {
        "active": daemon.is_active,
        "log": daemon._log[-20:],
    }


@app.post("/daemon/start")
def daemon_start():
    started = daemon.start()
    return {"ok": True, "was_already_running": not started}


@app.post("/daemon/stop")
def daemon_stop():
    daemon.stop()
    return {"ok": True}


# ── Scripts ───────────────────────────────────────────────────────────


@app.get("/scripts")
def list_scripts_endpoint():
    return list_scripts()


@app.get("/scripts/{name}")
def get_script_endpoint(name: str):
    try:
        content = load_script(name)
        return {"name": name, "content": content}
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.put("/scripts/{name}")
def save_script_endpoint(name: str, body: ScriptSave):
    save_script(name, body.content)
    return {"ok": True}


@app.delete("/scripts/{name}")
def delete_script_endpoint(name: str):
    try:
        delete_script(name)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/scripts/{name}/run/{session_id}")
def run_script_endpoint(name: str, session_id: str):
    if not is_running(session_id):
        raise HTTPException(422, f"Session '{session_id}' is not running")
    try:
        result = run_script(name, session_id)
        return {"result": result}
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Session Vars ──────────────────────────────────────────────────────


@app.get("/session-vars")
def list_session_vars_endpoint():
    return list_session_vars()


@app.post("/session-vars", status_code=201)
def create_session_var_endpoint(body: SessionVarCreate):
    try:
        add_session_var(body.name, body.label)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.patch("/session-vars/{name}")
def update_session_var_endpoint(name: str, body: SessionVarUpdate):
    updates = body.dict(exclude_none=True)
    if not updates:
        raise HTTPException(422, "No fields to update")
    try:
        update_session_var(name, **updates)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.delete("/session-vars/{name}")
def delete_session_var_endpoint(name: str):
    delete_session_var(name)
    return {"ok": True}


# ── WebSocket: Terminal Stream ────────────────────────────────────────


@app.websocket("/ws/terminal/{session_id}")
async def ws_terminal(websocket: WebSocket, session_id: str):
    await websocket.accept()
    capture_lines = 20
    last_content = ""

    try:
        while True:
            # Check for client messages (non-blocking)
            try:
                msg = await asyncio.wait_for(websocket.receive_json(), timeout=0.5)
                if msg.get("type") == "resize":
                    capture_lines = msg.get("lines", 20)
            except asyncio.TimeoutError:
                pass

            running = is_running(session_id)
            if running:
                try:
                    content = capture_pane(session_id, lines=capture_lines)
                except RuntimeError:
                    content = ""
                health = idle_tracker.get_health(session_id)
                if content != last_content:
                    last_content = content
                    await websocket.send_json({
                        "type": "output",
                        "content": content,
                        "health": health,
                        "running": True,
                    })
            else:
                if last_content != "":
                    last_content = ""
                    await websocket.send_json({
                        "type": "output",
                        "content": "",
                        "health": "stopped",
                        "running": False,
                    })

            await asyncio.sleep(0.5)
    except (WebSocketDisconnect, ConnectionClosedOK, ConnectionClosedError):
        pass


# ── WebSocket: State Broadcast ────────────────────────────────────────


@app.websocket("/ws/state")
async def ws_state(websocket: WebSocket):
    await websocket.accept()

    try:
        while True:
            sessions_config = list_sessions()
            sessions_state = {}
            for sid in sessions_config:
                running = is_running(sid)
                sessions_state[sid] = {
                    "running": running,
                    "health": idle_tracker.get_health(sid) if running else "stopped",
                }

            routines_config = list_routines()
            routines_state = {}
            for rid, r in routines_config.items():
                routines_state[rid] = {
                    "enabled": r.get("enabled", False),
                    "countdown": daemon.get_countdown(rid),
                }

            await websocket.send_json({
                "type": "state",
                "daemon_active": daemon.is_active,
                "daemon_log": daemon._log[-10:],
                "sessions": sessions_state,
                "routines": routines_state,
                "active_dashboard": get_active_dashboard_id(),
            })

            await asyncio.sleep(2.0)
    except (WebSocketDisconnect, ConnectionClosedOK, ConnectionClosedError):
        pass


# ── Frontend ─────────────────────────────────────────────────────────


@app.get("/")
def serve_frontend():
    return FileResponse("web/index.html")



# ── Main ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
