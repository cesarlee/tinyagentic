#!/usr/bin/env python3
"""TinyAgentic.ai — Tmux Session Manager (Textual TUI)"""

import os
import time
from functools import partial

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from textual.app import App, ComposeResult
from textual.screen import Screen, ModalScreen
from textual.binding import Binding
from textual.widgets import (
    Header,
    Footer,
    Static,
    Input,
    Button,
    Label,
    TextArea,
    Select,
    Switch,
)
from textual.containers import Vertical, Horizontal, VerticalScroll
from textual.worker import Worker, WorkerState
from textual.css.query import NoMatches
from textual.reactive import reactive
from rich.panel import Panel
from rich.text import Text

from core.config import load_config
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
from core.tmux_manager import (
    start_session as tmux_start,
    stop_session as tmux_stop,
    is_running,
    enter_session,
    capture_pane,
    send_keys,
)
from core.scripts import list_scripts, load_script, save_script, delete_script, run_script
from core.macros import create_macro, update_macro, delete_macro, get_macro, list_macros
from core.routines import (
    create_routine,
    update_routine,
    delete_routine,
    list_routines,
    RoutinesDaemon,
)


# ── Widgets ──────────────────────────────────────────────────────────


IDLE_THRESHOLD = 30.0  # seconds without output change → idle
PANEL_HEIGHT_DEFAULT = 24
PANEL_HEIGHT_STEP = 4
PANEL_HEIGHT_MIN = 8
PANEL_HEIGHT_MAX = 60


class SessionPanel(Static, can_focus=True):
    """A single session's terminal output panel for the dashboard."""

    def __init__(self, session_id: str, session_name: str, **kw):
        super().__init__(**kw)
        self.session_id = session_id
        self.session_name = session_name
        self._running = False
        self._last_text = ""
        self._prev_text = ""
        self._last_changed_at = time.monotonic() - IDLE_THRESHOLD

    def _activity_state(self) -> str:
        """Return 'working', 'idle', or 'stopped'."""
        if not self._running:
            return "stopped"
        elapsed = time.monotonic() - self._last_changed_at
        return "idle" if elapsed >= IDLE_THRESHOLD else "working"

    def update_content(self, text: str, running: bool = True) -> None:
        self._running = running
        if running and text != self._prev_text:
            self._last_changed_at = time.monotonic()
            self._prev_text = text
        self._last_text = text
        self._render_panel()

    def _render_panel(self) -> None:
        state = self._activity_state()
        focused = self.has_focus

        if state == "stopped":
            style = "cyan" if focused else "dim"
            subtitle = "stopped"
            indicator = ""
        elif state == "working":
            style = "bright_cyan" if focused else "green"
            subtitle = "WORKING"
            indicator = "\u25cf "  # ●
        else:  # idle
            style = "bright_cyan" if focused else "yellow"
            subtitle = "idle"
            indicator = "\u25cb "  # ○

        body = self._last_text or "[dim](empty)[/dim]"

        panel = Panel(
            body,
            title=f" {indicator}{self.session_name} [{self.session_id}] ",
            subtitle=f" {subtitle} ",
            title_align="left",
            border_style=style,
            padding=(0, 1),
        )
        self.update(panel)

    def on_focus(self) -> None:
        self._render_panel()

    def on_blur(self) -> None:
        self._render_panel()


class SidebarItem(Static, can_focus=True):
    """A single focusable item in the sidebar."""

    def __init__(self, item_type: str, item_id: str | None = None, **kw):
        super().__init__(**kw)
        self.item_type = item_type  # "session", "script", "macro", "routine", "add-*"
        self.item_id = item_id      # script name or routine id


# ── Status Bar Widget ────────────────────────────────────────────────


class StatusBar(Static):
    """Status bar widget for displaying notifications at the bottom of the screen."""

    message = reactive("")
    severity = reactive("info")

    def __init__(self, **kwargs):
        super().__init__("", **kwargs)

    def show_message(self, message: str, severity: str = "info") -> None:
        """Display a message in the status bar."""
        self.message = message
        self.severity = severity

        # Apply color based on severity
        if severity == "error":
            color = "red"
        elif severity == "warning":
            color = "yellow"
        elif severity == "success":
            color = "green"
        else:
            color = "cyan"

        self.update(f"[{color}]{message}[/{color}]")

        # Clear message after 5 seconds for non-error messages
        if severity != "error":
            self.set_timer(5.0, self.clear_message)

    def clear_message(self) -> None:
        """Clear the status bar message."""
        self.update("")
        self.message = ""


# ── Modal Screens ────────────────────────────────────────────────────


class FormScreen(ModalScreen):
    """Base modal with up/down arrow navigation between form fields."""

    BINDINGS = [
        Binding("up", "focus_prev", show=False),
        Binding("down", "focus_next", show=False),
    ]

    def notify(self, message: str, severity: str = "info") -> None:
        """Forward notification to MainScreen's status bar."""
        for screen in self.app.screen_stack:
            if isinstance(screen, MainScreen) and screen.query("#status-bar"):
                screen.query_one("#status-bar", StatusBar).show_message(message, severity)
                return
        super().notify(message, severity=severity)

    def _form_fields(self):
        return list(self.query("Input, Button, Select, Switch"))

    def action_focus_next(self) -> None:
        fields = self._form_fields()
        if not fields:
            return
        focused = self.app.focused
        if focused in fields:
            idx = fields.index(focused)
            fields[(idx + 1) % len(fields)].focus()
        else:
            fields[0].focus()

    def action_focus_prev(self) -> None:
        fields = self._form_fields()
        if not fields:
            return
        focused = self.app.focused
        if focused in fields:
            idx = fields.index(focused)
            fields[(idx - 1) % len(fields)].focus()
        else:
            fields[-1].focus()


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("y", "yes", "Yes", show=False),
        Binding("n", "cancel", "No", show=False),
    ]

    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(classes="confirm-dialog"):
            yield Static(self.message, classes="confirm-message")
            with Horizontal(classes="button-row"):
                yield Button("Yes (y)", variant="error", id="btn-yes")
                yield Button("No (n)", variant="primary", id="btn-no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class CreateSessionScreen(FormScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        dashboards = list_dashboards()
        active = get_active_dashboard_id()
        db_options = [(db.get("name", did), did) for did, db in dashboards.items()]
        active_label = dashboards.get(active, {}).get("name", active)

        with VerticalScroll(classes="modal-dialog"):
            yield Static("[bold]Create Session[/bold]", classes="form-title")
            yield Label("Session ID")
            yield Input(placeholder="e.g. my-project", id="f-session-id")
            yield Label("Display Name")
            yield Input(placeholder="e.g. My Project", id="f-name")
            yield Label("Working Directory")
            yield Input(placeholder="e.g. /home/ubuntu/my-project", id="f-workdir")
            yield Label("Dashboard")
            yield Select(db_options, id="f-dashboard", prompt=active_label)
            with Horizontal(classes="button-row"):
                yield Button("Create", variant="primary", id="btn-create")
                yield Button("Cancel", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-create":
            self._do_create()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "f-workdir":
            self._do_create()

    def _do_create(self) -> None:
        session_id = self.query_one("#f-session-id", Input).value.strip()
        if not session_id:
            self.notify("Session ID is required", severity="error")
            return
        name = self.query_one("#f-name", Input).value.strip() or None
        workdir = self.query_one("#f-workdir", Input).value.strip() or None
        db_select = self.query_one("#f-dashboard", Select)
        dashboard_id = db_select.value if db_select.value is not Select.BLANK else None

        try:
            create_session(session_id, name=name, working_dir=workdir, dashboard_id=dashboard_id)
            self.notify(f"Session '{session_id}' created")
            self.dismiss(True)
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    def action_cancel(self) -> None:
        self.dismiss(None)


class EditSessionScreen(FormScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, session_id: str):
        super().__init__()
        self.session_id = session_id
        self._var_counter = 0

    def compose(self) -> ComposeResult:
        s = get_session(self.session_id)
        if not s:
            yield Static(f"Session '{self.session_id}' not found")
            return

        dashboards = list_dashboards()
        current_db = get_session_dashboard(self.session_id)
        db_options = [(db.get("name", did), did) for did, db in dashboards.items()]
        current_label = dashboards.get(current_db, {}).get("name", current_db) if current_db else "None"

        with VerticalScroll(classes="modal-dialog"):
            yield Static(f"[bold]Edit: {self.session_id}[/bold]", classes="form-title")
            yield Label("Name")
            yield Input(value=s.get("name", ""), id="f-name")
            yield Label("Working Directory")
            yield Input(value=s.get("working_dir", "") or "", id="f-workdir")
            yield Label("Dashboard")
            yield Select(db_options, id="f-dashboard", prompt=current_label)

            yield Static("")
            yield Static("[bold cyan]Session Variables[/bold cyan]", classes="form-subtitle")
            yield Vertical(id="vars-container")
            yield Button("+ Add Variable", id="btn-add-var", classes="add-var-btn")

            with Horizontal(classes="button-row"):
                yield Button("Save", variant="primary", id="btn-save")
                yield Button("Cancel", id="btn-cancel")

    def on_mount(self) -> None:
        s = get_session(self.session_id)
        if not s:
            return
        existing_vars = s.get("vars", {})
        for var_name, var_value in existing_vars.items():
            self._add_var_row(var_name, str(var_value))

    def _add_var_row(self, name: str = "", value: str = "") -> None:
        self._var_counter += 1
        idx = self._var_counter
        row = Horizontal(
            Input(value=name, placeholder="Variable name", id=f"var-name-{idx}", classes="var-name-input"),
            Input(value=value, placeholder="Value", id=f"var-val-{idx}", classes="var-val-input"),
            Button("x", id=f"var-del-{idx}", classes="var-del-btn"),
            id=f"var-row-{idx}",
            classes="var-row",
        )
        self.query_one("#vars-container").mount(row)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        if btn_id == "btn-save":
            self._do_save()
        elif btn_id == "btn-add-var":
            self._add_var_row()
        elif btn_id and btn_id.startswith("var-del-"):
            idx = btn_id.replace("var-del-", "")
            try:
                row = self.query_one(f"#var-row-{idx}")
                row.remove()
            except NoMatches:
                pass
        elif btn_id == "btn-cancel":
            self.dismiss(None)

    def _collect_vars(self) -> dict:
        """Collect all variable name/value pairs from the form."""
        variables = {}
        for row in self.query(".var-row"):
            name_inputs = row.query("Input.var-name-input")
            val_inputs = row.query("Input.var-val-input")
            if name_inputs and val_inputs:
                var_name = name_inputs.first().value.strip()
                var_value = val_inputs.first().value
                if var_name:
                    variables[var_name] = var_value
        return variables

    def _do_save(self) -> None:
        s = get_session(self.session_id)
        if not s:
            self.dismiss(None)
            return

        updates = {}
        name = self.query_one("#f-name", Input).value.strip()
        if name and name != s.get("name", ""):
            updates["name"] = name

        workdir = self.query_one("#f-workdir", Input).value.strip() or None
        if workdir != s.get("working_dir"):
            updates["working_dir"] = workdir

        new_vars = self._collect_vars()
        if new_vars != s.get("vars", {}):
            updates["vars"] = new_vars

        # Handle dashboard change
        db_select = self.query_one("#f-dashboard", Select)
        new_db = db_select.value if db_select.value is not Select.BLANK else None
        current_db = get_session_dashboard(self.session_id)
        dashboard_changed = new_db and new_db != current_db

        if updates or dashboard_changed:
            try:
                if updates:
                    update_session(self.session_id, **updates)
                if dashboard_changed:
                    move_session_to_dashboard(self.session_id, new_db)
                    updates["dashboard"] = new_db
                self.notify(f"Updated: {', '.join(updates.keys())}")
                self.dismiss(True)
            except Exception as e:
                self.notify(f"Error: {e}", severity="error")
        else:
            self.notify("No changes")
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SendMessageScreen(FormScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, session_id: str):
        super().__init__()
        self.session_id = session_id

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-dialog message-dialog"):
            yield Static(
                f"[bold]Send message to: {self.session_id}[/bold]",
                classes="form-title",
            )
            yield Input(placeholder="Type your message...", id="f-msg")
            with Horizontal(classes="button-row"):
                yield Button("Send", variant="primary", id="btn-send")
                yield Button("Cancel", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-send":
            self._do_send()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._do_send()

    def _do_send(self) -> None:
        msg = self.query_one("#f-msg", Input).value.strip()
        if msg:
            send_keys(self.session_id, msg, enter=True)
            self.notify(f"Sent to {self.session_id}")
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(None)


class RunScriptScreen(FormScreen):
    """Pick a script to run on the focused session."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, session_id: str):
        super().__init__()
        self.session_id = session_id

    def compose(self) -> ComposeResult:
        scripts = list_scripts()
        options = [(s, s) for s in scripts]

        with Vertical(classes="modal-dialog"):
            yield Static(
                f"[bold]Run Script on: {self.session_id}[/bold]",
                classes="form-title",
            )
            if options:
                yield Select(options, id="f-script", prompt="Select a script")
                with Horizontal(classes="button-row"):
                    yield Button("Run", variant="primary", id="btn-run")
                    yield Button("Cancel", id="btn-cancel")
            else:
                yield Static("[dim]No scripts available. Press N to create one.[/dim]")
                with Horizontal(classes="button-row"):
                    yield Button("Cancel", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-run":
            self._do_run()
        else:
            self.dismiss(None)

    def _do_run(self) -> None:
        select = self.query_one("#f-script", Select)
        script_name = select.value
        if script_name is Select.BLANK:
            self.notify("Select a script first", severity="warning")
            return
        self.dismiss(("run", script_name, self.session_id))

    def action_cancel(self) -> None:
        self.dismiss(None)


class ScriptEditorScreen(Screen):
    """Full-screen script editor."""

    BINDINGS = [
        Binding("escape", "go_back", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
    ]

    def __init__(self, script_name: str, is_new: bool = False):
        super().__init__()
        self.script_name = script_name
        self.is_new = is_new

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(
            f"[bold]Editing:[/bold] scripts/{self.script_name}.py   "
            f"[dim](Ctrl+S = save, Escape = cancel)[/dim]",
            id="script-header",
        )
        content = ""
        if not self.is_new:
            try:
                content = load_script(self.script_name)
            except ValueError:
                content = ""
        if not content:
            content = (
                '"""Script description."""\n\n\n'
                'def run(session_id, tmux):\n'
                '    """Called by the routine engine or manual run.\n\n'
                '    Args:\n'
                '        session_id: the tmux session ID\n'
                '        tmux: the tmux_manager module (capture_pane, send_keys, is_running)\n'
                '    """\n'
                '    content = tmux.capture_pane(session_id)\n'
                '    return {"status": "ok"}\n'
            )
        yield TextArea(content, id="script-editor", language="python")
        with Horizontal(classes="button-row"):
            yield Button("Save", variant="primary", id="btn-save")
            yield Button("Cancel", id="btn-cancel")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-save":
            self.action_save()
        else:
            self.action_go_back()

    def action_save(self) -> None:
        editor = self.query_one("#script-editor", TextArea)
        save_script(self.script_name, editor.text)
        self.notify(f"Saved: scripts/{self.script_name}.py")
        self.app.pop_screen()

    def action_go_back(self) -> None:
        self.app.pop_screen()


class CreateScriptScreen(FormScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-dialog"):
            yield Static("[bold]New Script[/bold]", classes="form-title")
            yield Label("Script name (without .py)")
            yield Input(placeholder="e.g. my-script", id="f-script-name")
            with Horizontal(classes="button-row"):
                yield Button("Create & Edit", variant="primary", id="btn-create")
                yield Button("Cancel", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-create":
            self._do_create()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._do_create()

    def _do_create(self) -> None:
        name = self.query_one("#f-script-name", Input).value.strip()
        if not name:
            self.notify("Script name is required", severity="error")
            return
        # Check if already exists
        if name in list_scripts():
            self.notify(f"Script '{name}' already exists", severity="error")
            return
        self.dismiss(("new-script", name))

    def action_cancel(self) -> None:
        self.dismiss(None)


class CreateRoutineScreen(FormScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        scripts = list_scripts()
        script_options = [(s, s) for s in scripts]
        macros = list_macros()
        macro_options = [(m.get("name", mid), mid) for mid, m in macros.items()]
        sessions = list_sessions()
        session_options = [("* (all sessions)", "*")] + [(sid, sid) for sid in sessions]
        type_options = [("Script", "script"), ("Macro", "macro")]

        with VerticalScroll(classes="modal-dialog"):
            yield Static("[bold]Create Routine[/bold]", classes="form-title")
            yield Label("Routine ID")
            yield Input(placeholder="e.g. approval-watcher", id="f-routine-id")
            yield Label("Display Name")
            yield Input(placeholder="e.g. Approval Watcher", id="f-name")
            yield Label("Type")
            yield Select(type_options, id="f-type", prompt="Script")
            yield Label("Script")
            yield Select(script_options, id="f-script", prompt="Select a script")
            yield Label("Macro")
            yield Select(macro_options, id="f-macro", prompt="Select a macro")
            yield Label("Target Session")
            yield Select(session_options, id="f-session", prompt="* (all sessions)")
            yield Label("Interval (seconds)")
            yield Input(value="30", id="f-interval")
            with Horizontal(classes="switch-row"):
                yield Label("Enabled", classes="switch-label")
                yield Switch(value=True, id="f-enabled")
            with Horizontal(classes="switch-row"):
                yield Label("Only when session is idle", classes="switch-label")
                yield Switch(value=True, id="f-idle-only")
            yield Label("Only when process is running (optional)")
            yield Input(placeholder="e.g. claude", id="f-run-when-process")
            with Horizontal(classes="button-row"):
                yield Button("Create", variant="primary", id="btn-create")
                yield Button("Cancel", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-create":
            self._do_create()
        else:
            self.dismiss(None)

    def _do_create(self) -> None:
        routine_id = self.query_one("#f-routine-id", Input).value.strip()
        if not routine_id:
            self.notify("Routine ID is required", severity="error")
            return
        name = self.query_one("#f-name", Input).value.strip() or routine_id.replace("-", " ").title()

        type_select = self.query_one("#f-type", Select)
        routine_type = type_select.value if type_select.value is not Select.BLANK else "script"

        if routine_type == "macro":
            macro_select = self.query_one("#f-macro", Select)
            if macro_select.value is Select.BLANK:
                self.notify("Select a macro", severity="error")
                return
            macro_id = macro_select.value
            script_name = None
        else:
            try:
                script_select = self.query_one("#f-script", Select)
                script_name = script_select.value
                if script_name is Select.BLANK:
                    self.notify("Select a script", severity="error")
                    return
            except NoMatches:
                self.notify("No scripts available", severity="error")
                return
            macro_id = None

        session_select = self.query_one("#f-session", Select)
        target = session_select.value if session_select.value is not Select.BLANK else "*"

        try:
            interval = int(self.query_one("#f-interval", Input).value.strip())
        except ValueError:
            self.notify("Interval must be a number", severity="error")
            return

        enabled = self.query_one("#f-enabled", Switch).value
        idle_only = self.query_one("#f-idle-only", Switch).value
        run_when_process = self.query_one("#f-run-when-process", Input).value.strip()

        try:
            create_routine(routine_id, name, script=script_name, macro=macro_id,
                           session=target, interval_seconds=interval,
                           enabled=enabled, idle_only=idle_only,
                           run_when_process=run_when_process)
            self.notify(f"Routine '{routine_id}' created")
            self.dismiss(True)
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    def action_cancel(self) -> None:
        self.dismiss(None)


class EditRoutineScreen(FormScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, routine_id: str):
        super().__init__()
        self.routine_id = routine_id

    def compose(self) -> ComposeResult:
        routines = list_routines()
        r = routines.get(self.routine_id)
        if not r:
            yield Static(f"Routine '{self.routine_id}' not found")
            return

        scripts = list_scripts()
        script_options = [(s, s) for s in scripts]
        macros = list_macros()
        macro_options = [(m.get("name", mid), mid) for mid, m in macros.items()]
        sessions = list_sessions()
        session_options = [("* (all sessions)", "*")] + [(sid, sid) for sid in sessions]
        type_options = [("Script", "script"), ("Macro", "macro")]

        current_type = r.get("type", "script")
        current_type_label = "Macro" if current_type == "macro" else "Script"

        with VerticalScroll(classes="modal-dialog"):
            yield Static(f"[bold]Edit: {self.routine_id}[/bold]", classes="form-title")
            yield Label("Name")
            yield Input(value=r.get("name", ""), id="f-name")
            yield Label("Type")
            yield Select(type_options, id="f-type", prompt=current_type_label)
            yield Label("Script")
            current_script = r.get("script", "")
            yield Select(script_options, id="f-script",
                         prompt=current_script or "Select a script")
            yield Label("Macro")
            current_macro = r.get("macro", "")
            macro_label = current_macro or "Select a macro"
            yield Select(macro_options, id="f-macro", prompt=macro_label)
            yield Label("Target Session")
            current_session = r.get("session", "*")
            session_label = "* (all sessions)" if current_session == "*" else current_session
            yield Select(session_options, id="f-session",
                         prompt=session_label or "Select a session")
            yield Label("Interval (seconds)")
            yield Input(value=str(r.get("interval_seconds", 30)), id="f-interval")
            with Horizontal(classes="switch-row"):
                yield Label("Enabled", classes="switch-label")
                yield Switch(value=r.get("enabled", False), id="f-enabled")
            with Horizontal(classes="switch-row"):
                yield Label("Only when session is idle", classes="switch-label")
                yield Switch(value=r.get("idle_only", True), id="f-idle-only")
            yield Label("Only when process is running (optional)")
            yield Input(value=r.get("run_when_process", ""), id="f-run-when-process")
            with Horizontal(classes="button-row"):
                yield Button("Save", variant="primary", id="btn-save")
                yield Button("Delete", variant="error", id="btn-delete")
                yield Button("Cancel", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-save":
            self._do_save()
        elif event.button.id == "btn-delete":
            try:
                delete_routine(self.routine_id)
                self.notify(f"Deleted routine '{self.routine_id}'")
                self.dismiss(True)
            except Exception as e:
                self.notify(f"Error: {e}", severity="error")
        else:
            self.dismiss(None)

    def _do_save(self) -> None:
        updates = {}
        name = self.query_one("#f-name", Input).value.strip()
        if name:
            updates["name"] = name

        type_select = self.query_one("#f-type", Select)
        if type_select.value is not Select.BLANK:
            updates["type"] = type_select.value

        script_select = self.query_one("#f-script", Select)
        if script_select.value is not Select.BLANK:
            updates["script"] = script_select.value

        macro_select = self.query_one("#f-macro", Select)
        if macro_select.value is not Select.BLANK:
            updates["macro"] = macro_select.value

        session_select = self.query_one("#f-session", Select)
        if session_select.value is not Select.BLANK:
            updates["session"] = session_select.value

        try:
            interval = int(self.query_one("#f-interval", Input).value.strip())
            updates["interval_seconds"] = interval
        except ValueError:
            pass

        updates["enabled"] = self.query_one("#f-enabled", Switch).value
        updates["idle_only"] = self.query_one("#f-idle-only", Switch).value
        updates["run_when_process"] = self.query_one("#f-run-when-process", Input).value.strip()

        if updates:
            try:
                update_routine(self.routine_id, **updates)
                self.notify(f"Updated routine '{self.routine_id}'")
                self.dismiss(True)
            except Exception as e:
                self.notify(f"Error: {e}", severity="error")
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


def _add_bash_continuation(text: str) -> str:
    """Auto-insert ' \\' before each internal newline for bash continuation."""
    lines = text.split("\n")
    result = []
    for i, line in enumerate(lines):
        if i < len(lines) - 1:
            # Strip existing trailing ' \' to avoid doubling
            stripped = line.rstrip()
            if stripped.endswith("\\"):
                result.append(line)
            else:
                result.append(line.rstrip() + " \\")
        else:
            result.append(line)
    return "\n".join(result)


class CreateMacroScreen(FormScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="modal-dialog"):
            yield Static("[bold]Create Macro[/bold]", classes="form-title")
            yield Label("Macro ID")
            yield Input(placeholder="e.g. start-dev", id="f-macro-id")
            yield Label("Display Name")
            yield Input(placeholder="e.g. Start Dev Server", id="f-name")
            yield Label("Keys to send [dim](multi-line: auto-inserts \\\\)[/dim]")
            yield TextArea("", id="f-keys", classes="macro-editor")
            with Horizontal(classes="switch-row"):
                yield Label("Send Enter upon completion", classes="switch-label")
                yield Switch(value=True, id="f-enter")
            with Horizontal(classes="button-row"):
                yield Button("Create", variant="primary", id="btn-create")
                yield Button("Cancel", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-create":
            self._do_create()
        else:
            self.dismiss(None)

    def _do_create(self) -> None:
        macro_id = self.query_one("#f-macro-id", Input).value.strip()
        if not macro_id:
            self.notify("Macro ID is required", severity="error")
            return
        name = self.query_one("#f-name", Input).value.strip() or macro_id.replace("-", " ").title()
        raw = self.query_one("#f-keys", TextArea).text
        if not raw.strip():
            self.notify("Keys are required", severity="error")
            return
        keys = _add_bash_continuation(raw)
        send_enter = self.query_one("#f-enter", Switch).value
        try:
            create_macro(macro_id, name, keys, enter=send_enter)
            self.notify(f"Macro '{macro_id}' created")
            self.dismiss(True)
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    def action_cancel(self) -> None:
        self.dismiss(None)


class EditMacroScreen(FormScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, macro_id: str):
        super().__init__()
        self.macro_id = macro_id

    def compose(self) -> ComposeResult:
        m = get_macro(self.macro_id)
        if not m:
            yield Static(f"Macro '{self.macro_id}' not found")
            return

        with VerticalScroll(classes="modal-dialog"):
            yield Static(f"[bold]Edit: {self.macro_id}[/bold]", classes="form-title")
            yield Label("Name")
            yield Input(value=m.get("name", ""), id="f-name")
            yield Label("Keys to send [dim](multi-line: auto-inserts \\\\)[/dim]")
            yield TextArea(m.get("keys", ""), id="f-keys", classes="macro-editor")
            with Horizontal(classes="switch-row"):
                yield Label("Send Enter upon completion", classes="switch-label")
                yield Switch(value=m.get("enter", True), id="f-enter")
            with Horizontal(classes="button-row"):
                yield Button("Save", variant="primary", id="btn-save")
                yield Button("Cancel", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-save":
            self._do_save()
        else:
            self.dismiss(None)

    def _do_save(self) -> None:
        updates = {}
        name = self.query_one("#f-name", Input).value.strip()
        if name:
            updates["name"] = name
        raw = self.query_one("#f-keys", TextArea).text
        if raw.strip():
            updates["keys"] = _add_bash_continuation(raw)
        updates["enter"] = self.query_one("#f-enter", Switch).value
        if updates:
            try:
                update_macro(self.macro_id, **updates)
                self.notify(f"Updated macro '{self.macro_id}'")
                self.dismiss(True)
            except Exception as e:
                self.notify(f"Error: {e}", severity="error")
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── Dashboard Screens ────────────────────────────────────────────────


class CreateDashboardScreen(FormScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="modal-dialog"):
            yield Static("[bold]Create Dashboard[/bold]", classes="form-title")
            yield Label("Dashboard ID")
            yield Input(placeholder="e.g. development", id="f-dashboard-id")
            yield Label("Display Name")
            yield Input(placeholder="e.g. Development", id="f-name")
            with Horizontal(classes="button-row"):
                yield Button("Create", variant="primary", id="btn-create")
                yield Button("Cancel", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-create":
            self._do_create()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "f-name":
            self._do_create()

    def _do_create(self) -> None:
        dashboard_id = self.query_one("#f-dashboard-id", Input).value.strip()
        if not dashboard_id:
            self.notify("Dashboard ID is required", severity="error")
            return
        name = self.query_one("#f-name", Input).value.strip() or None
        try:
            create_dashboard(dashboard_id, name=name)
            self.notify(f"Dashboard '{dashboard_id}' created")
            self.dismiss(True)
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    def action_cancel(self) -> None:
        self.dismiss(None)


class EditDashboardScreen(FormScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, dashboard_id: str):
        super().__init__()
        self.dashboard_id = dashboard_id

    def compose(self) -> ComposeResult:
        db = get_dashboard(self.dashboard_id)
        if not db:
            yield Static(f"Dashboard '{self.dashboard_id}' not found")
            return
        with VerticalScroll(classes="modal-dialog"):
            yield Static(f"[bold]Edit: {self.dashboard_id}[/bold]", classes="form-title")
            yield Label("Name")
            yield Input(value=db.get("name", ""), id="f-name")
            with Horizontal(classes="button-row"):
                yield Button("Save", variant="primary", id="btn-save")
                yield Button("Delete", variant="error", id="btn-delete")
                yield Button("Cancel", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-save":
            self._do_save()
        elif event.button.id == "btn-delete":
            self._do_delete()
        else:
            self.dismiss(None)

    def _do_save(self) -> None:
        name = self.query_one("#f-name", Input).value.strip()
        if name:
            try:
                update_dashboard(self.dashboard_id, name=name)
                self.notify(f"Updated dashboard '{self.dashboard_id}'")
                self.dismiss(True)
            except Exception as e:
                self.notify(f"Error: {e}", severity="error")
        else:
            self.dismiss(None)

    def _do_delete(self) -> None:
        try:
            delete_dashboard(self.dashboard_id)
            self.notify(f"Deleted dashboard '{self.dashboard_id}'")
            self.dismiss("deleted")
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── Main Screen ──────────────────────────────────────────────────────


class MainScreen(Screen):
    BINDINGS = [
        # Session management
        Binding("n", "new_session", "New", show=True),
        Binding("e", "edit_session", "Edit", show=True),
        Binding("d", "delete_focused", "Del", show=True),
        Binding("s", "start_session", "Start", show=True),
        Binding("x", "stop_session", "Stop", show=True),
        Binding("i", "interactive", "Enter", show=True),
        Binding("m", "send_message", "Msg", show=True),
        # Scripts
        Binding("r", "run_script", "Run", show=True),
        Binding("E", "edit_script", "E:Script", show=True),
        Binding("N", "new_script", "N:Script", show=True),
        # Macros
        Binding("M", "run_macro", "M:Macro", show=True),
        # Routines
        Binding("R", "new_routine", "R:Routine", show=True),
        Binding("p", "toggle_daemon", "Routines", show=True),
        # Panel sizing
        Binding("+", "grow_panels", "+Size", show=True),
        Binding("-", "shrink_panels", "-Size", show=True),
        # Navigation
        Binding("enter", "activate_item", "Select", show=False),
        Binding("left", "nav_left", "Left", show=False),
        Binding("right", "nav_right", "Right", show=False),
        Binding("up", "nav_up", "Up", show=False),
        Binding("down", "nav_down", "Down", show=False),
        Binding("tab", "toggle_sidebar_focus", "Tab", show=False),
        Binding("q", "quit_app", "Quit", show=True),
        # Dashboard switching (1-9)
        Binding("1", "switch_dashboard_1", show=False),
        Binding("2", "switch_dashboard_2", show=False),
        Binding("3", "switch_dashboard_3", show=False),
        Binding("4", "switch_dashboard_4", show=False),
        Binding("5", "switch_dashboard_5", show=False),
        Binding("6", "switch_dashboard_6", show=False),
        Binding("7", "switch_dashboard_7", show=False),
        Binding("8", "switch_dashboard_8", show=False),
        Binding("9", "switch_dashboard_9", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main-layout"):
            yield VerticalScroll(id="sidebar")
            yield VerticalScroll(id="session-grid")
        yield StatusBar(id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        self._current_dashboard = get_active_dashboard_id()
        self._last_session_id: str | None = None
        self._last_focused_id: str | None = None
        self._idle_states: dict[str, bool] = {}  # session_id → is_idle (thread-safe cache)
        self._build_sidebar()
        self._build_panels()
        self.set_interval(3.0, self._refresh_panels)
        self.set_interval(1.0, self._refresh_sidebar)
        self.set_timer(0.3, self._focus_first_panel)

    def notify(self, message: str, severity: str = "info") -> None:
        """Show a message in the status bar instead of a toast."""
        try:
            status_bar = self.query_one("#status-bar", StatusBar)
            status_bar.show_message(message, severity)
        except:
            # Fallback to parent notify if status bar not available
            super().notify(message, severity=severity)

    # ── Dashboard switching ──

    def _switch_dashboard(self, dashboard_id: str) -> None:
        if dashboard_id == self._current_dashboard:
            return
        self._current_dashboard = dashboard_id
        set_active_dashboard(dashboard_id)
        self._last_session_id = None
        self._last_focused_id = f"sb-db-{dashboard_id}"
        self._build_panels()
        self._build_sidebar()
        db = get_dashboard(dashboard_id)
        name = db.get("name", dashboard_id) if db else dashboard_id
        self.notify(f"Switched to: {name}")

    def _switch_to_dashboard_index(self, index: int) -> None:
        dbs = list(list_dashboards().keys())
        if index < len(dbs):
            self._switch_dashboard(dbs[index])

    def action_switch_dashboard_1(self): self._switch_to_dashboard_index(0)
    def action_switch_dashboard_2(self): self._switch_to_dashboard_index(1)
    def action_switch_dashboard_3(self): self._switch_to_dashboard_index(2)
    def action_switch_dashboard_4(self): self._switch_to_dashboard_index(3)
    def action_switch_dashboard_5(self): self._switch_to_dashboard_index(4)
    def action_switch_dashboard_6(self): self._switch_to_dashboard_index(5)
    def action_switch_dashboard_7(self): self._switch_to_dashboard_index(6)
    def action_switch_dashboard_8(self): self._switch_to_dashboard_index(7)
    def action_switch_dashboard_9(self): self._switch_to_dashboard_index(8)

    def on_screen_resume(self) -> None:
        """Called when a modal is dismissed and this screen becomes active."""
        self._restore_focus()

    def on_descendant_focus(self, event) -> None:
        widget = event.widget
        if isinstance(widget, SessionPanel):
            self._last_session_id = widget.session_id
        if isinstance(widget, (SessionPanel, SidebarItem)):
            self._last_focused_id = widget.id

    def _focus_first_panel(self) -> None:
        panels = self.query("SessionPanel")
        if panels:
            panels.first().focus()

    def _restore_focus(self) -> None:
        """Restore focus to the last focused panel/sidebar item, or first panel."""
        if self._last_focused_id:
            try:
                widget = self.query_one(f"#{self._last_focused_id}")
                widget.focus()
                return
            except NoMatches:
                pass
        self._focus_first_panel()

    def _focused_session_id(self) -> str | None:
        focused = self.app.focused
        if isinstance(focused, SessionPanel):
            return focused.session_id
        return self._last_session_id

    # ── Sidebar ──

    def _build_sidebar(self) -> None:
        """Build the sidebar. Defers mount if children need removal first."""
        container = self.query_one("#sidebar")
        if container.children:
            container.remove_children()
            # Removal is async in Textual — defer mount to next tick
            self.set_timer(0.05, self._populate_sidebar)
        else:
            self._populate_sidebar()

    def _populate_sidebar(self) -> None:
        """Mount sidebar items into the (now empty) sidebar container."""
        container = self.query_one("#sidebar")
        if container.children:
            return  # Already populated by another call

        # Dashboards header
        container.mount(Static("[bold cyan]DASHBOARDS[/bold cyan]", classes="sidebar-header"))

        for did, db in list_dashboards().items():
            item = SidebarItem("dashboard", did, id=f"sb-db-{did}", classes="sidebar-item")
            container.mount(item)

        add_db = SidebarItem("add-dashboard", None, id="sb-add-dashboard", classes="sidebar-item sidebar-add")
        add_db.update("  [green]+ Dashboard[/green]")
        container.mount(add_db)

        # Sessions header (filtered to current dashboard)
        container.mount(Static(""))
        container.mount(Static("[bold cyan]SESSIONS[/bold cyan]", classes="sidebar-header"))

        for sid, s in list_dashboard_sessions(self._current_dashboard).items():
            item = SidebarItem("session", sid, id=f"sb-sess-{sid}", classes="sidebar-item")
            container.mount(item)

        add_session = SidebarItem("add-session", None, id="sb-add-session", classes="sidebar-item sidebar-add")
        add_session.update("  [green]+ Session[/green]")
        container.mount(add_session)

        # Scripts header
        container.mount(Static(""))
        container.mount(Static("[bold cyan]SCRIPTS[/bold cyan]", classes="sidebar-header"))

        for name in list_scripts():
            item = SidebarItem("script", name, id=f"sb-s-{name}", classes="sidebar-item")
            item.update(f"  {name}")
            container.mount(item)

        add_script = SidebarItem("add-script", None, id="sb-add-script", classes="sidebar-item sidebar-add")
        add_script.update("  [green]+ Script[/green]")
        container.mount(add_script)

        # Macros header
        container.mount(Static(""))
        container.mount(Static("[bold cyan]MACROS[/bold cyan]", classes="sidebar-header"))

        for mid, m in list_macros().items():
            item = SidebarItem("macro", mid, id=f"sb-m-{mid}", classes="sidebar-item")
            name = m.get("name", mid)
            item.update(f"  {name}")
            container.mount(item)

        add_macro = SidebarItem("add-macro", None, id="sb-add-macro", classes="sidebar-item sidebar-add")
        add_macro.update("  [green]+ Macro[/green]")
        container.mount(add_macro)

        # Routines header
        container.mount(Static(""))
        container.mount(Static("[bold cyan]ROUTINES[/bold cyan]", classes="sidebar-header"))

        for rid, r in list_routines().items():
            item = SidebarItem("routine", rid, id=f"sb-r-{rid}", classes="sidebar-item")
            container.mount(item)

        add_routine = SidebarItem("add-routine", None, id="sb-add-routine", classes="sidebar-item sidebar-add")
        add_routine.update("  [green]+ Routine[/green]")
        container.mount(add_routine)

        # Daemon toggle
        container.mount(Static(""))
        container.mount(SidebarItem("daemon-toggle", id="sidebar-daemon-status"))

        # Update all dynamic text immediately
        self._refresh_sidebar()
        self._restore_focus()

    def _refresh_sidebar(self) -> None:
        """Update session status, routine countdowns, and daemon status in-place."""
        # Skip if sidebar is empty (mid-rebuild)
        sidebar = self.query_one("#sidebar")
        if not sidebar.children:
            return

        # Update dashboard items
        dashboards = list_dashboards()
        for idx, (did, db) in enumerate(dashboards.items()):
            try:
                item = self.query_one(f"#sb-db-{did}", SidebarItem)
            except NoMatches:
                self._build_sidebar()
                return
            name = db.get("name", did)
            num = str(idx + 1) if idx < 9 else ""
            prefix = f"[bold cyan]{num}[/bold cyan] " if num else "  "
            if did == self._current_dashboard:
                item.update(f"  {prefix}[bold reverse] {name} [/bold reverse]")
            else:
                item.update(f"  {prefix}{name}")

        # Update session items (filtered to current dashboard)
        for sid, s in list_dashboard_sessions(self._current_dashboard).items():
            try:
                item = self.query_one(f"#sb-sess-{sid}", SidebarItem)
            except NoMatches:
                self._build_sidebar()
                return
            name = s.get("name", sid)
            running = is_running(sid)
            if running:
                item.update(f"  [green]\u25cf[/green] {name}")
            else:
                item.update(f"  [dim]\u25cb {name}[/dim]")

        # Update macro items
        for mid, m in list_macros().items():
            try:
                item = self.query_one(f"#sb-m-{mid}", SidebarItem)
            except NoMatches:
                self._build_sidebar()
                return
            name = m.get("name", mid)
            item.update(f"  {name}")

        # Update routine items
        daemon = self.app.routines_daemon
        routines = list_routines()

        for rid, r in routines.items():
            try:
                item = self.query_one(f"#sb-r-{rid}", SidebarItem)
            except NoMatches:
                # Routine added since last build — rebuild
                self._build_sidebar()
                return

            enabled = r.get("enabled", False)
            name = r.get("name", rid)
            if enabled and daemon and daemon.is_active:
                cd = daemon.get_countdown(rid)
                timer = f"{int(cd)}s" if cd is not None else "..."
                item.update(f"  [green]\u21bb[/green] {name} [{timer}]")
            elif enabled:
                item.update(f"  [yellow]\u25a1[/yellow] {name} [paused]")
            else:
                item.update(f"  [dim]\u25a1 {name} [off][/dim]")

        try:
            status_widget = self.query_one("#sidebar-daemon-status", SidebarItem)
            if daemon and daemon.is_active:
                lines = ["[green]\u25cf Routines: running[/green]  [dim](Enter to stop)[/dim]"]
                for entry in daemon._log[-3:]:
                    lines.append(f"  [dim]{entry}[/dim]")
                status_widget.update("\n".join(lines))
            else:
                status_widget.update("[yellow]\u25cb Routines: stopped[/yellow]  [dim](Enter to start)[/dim]")
        except NoMatches:
            pass

    # ── Panel management ──

    def _build_panels(self) -> None:
        container = self.query_one("#session-grid")
        if container.children:
            container.remove_children()
            self.set_timer(0.05, self._populate_panels)
        else:
            self._populate_panels()

    def _populate_panels(self) -> None:
        container = self.query_one("#session-grid")
        if container.children:
            return  # Already populated by another call

        sessions = list_dashboard_sessions(self._current_dashboard)
        if not sessions:
            container.mount(Static("[dim]No sessions on this dashboard. Press 'n' to create one.[/dim]"))
            return

        items = list(sessions.items())
        for i in range(0, len(items), 2):
            row_widgets = []
            for sid, s in items[i : i + 2]:
                running = is_running(sid)
                panel = SessionPanel(
                    session_id=sid,
                    session_name=s.get("name", sid),
                    id=f"panel-{sid}",
                    classes="session-panel",
                )
                row_widgets.append(panel)
                if running:
                    try:
                        content = capture_pane(sid, lines=20)
                        lines = content.strip().splitlines()
                        panel.update_content("\n".join(lines[-20:]), running=True)
                    except Exception:
                        panel.update_content("[dim](error reading terminal)[/dim]", running=True)
                else:
                    panel.update_content("[dim]not running[/dim]", running=False)

            if len(row_widgets) == 1:
                row_widgets.append(Static("", classes="panel-spacer"))

            row = Horizontal(*row_widgets, classes="panel-row")
            container.mount(row)

        self._restore_focus()

    def _refresh_panels(self) -> None:
        # Skip if grid is empty (mid-rebuild)
        grid = self.query_one("#session-grid")
        if not grid.children:
            return

        sessions = list_dashboard_sessions(self._current_dashboard)
        # Check if panels need rebuilding
        for sid in sessions:
            try:
                self.query_one(f"#panel-{sid}", SessionPanel)
            except NoMatches:
                self._build_panels()
                return

        for sid, s in sessions.items():
            try:
                panel = self.query_one(f"#panel-{sid}", SessionPanel)
            except NoMatches:
                continue

            if is_running(sid):
                self.run_worker(
                    partial(self._capture_for_panel, sid),
                    thread=True,
                    group="panel-capture",
                )
                self._idle_states[sid] = panel._activity_state() == "idle"
            else:
                panel.update_content("[dim]not running[/dim]", running=False)
                self._idle_states[sid] = True

    @staticmethod
    def _capture_for_panel(session_id: str) -> tuple:
        content = capture_pane(session_id, lines=20)
        lines = content.strip().splitlines()
        return (session_id, "\n".join(lines[-20:]))

    # ── Session CRUD ──

    def action_new_session(self) -> None:
        self.app.push_screen(CreateSessionScreen(), callback=self._on_form_done)

    def action_edit_session(self) -> None:
        focused = self.app.focused
        if isinstance(focused, SidebarItem):
            if focused.item_type == "session" and focused.item_id:
                self.app.push_screen(EditSessionScreen(focused.item_id), callback=self._on_form_done)
            elif focused.item_type == "script" and focused.item_id:
                self.app.push_screen(
                    ScriptEditorScreen(focused.item_id),
                    callback=lambda _: self._build_sidebar(),
                )
            elif focused.item_type == "macro" and focused.item_id:
                self.app.push_screen(EditMacroScreen(focused.item_id), callback=self._on_form_done)
            elif focused.item_type == "routine" and focused.item_id:
                self.app.push_screen(EditRoutineScreen(focused.item_id), callback=self._on_form_done)
            elif focused.item_type == "dashboard" and focused.item_id:
                self.app.push_screen(EditDashboardScreen(focused.item_id), callback=self._on_dashboard_form_done)
            else:
                self.notify("Nothing to edit here", severity="warning")
            return
        sid = self._focused_session_id()
        if not sid:
            self.notify("Focus a session panel first", severity="warning")
            return
        self.app.push_screen(EditSessionScreen(sid), callback=self._on_form_done)

    def _on_form_done(self, result) -> None:
        if result:
            self._build_panels()
            self._build_sidebar()

    def _on_dashboard_form_done(self, result) -> None:
        if result == "deleted":
            self._current_dashboard = get_active_dashboard_id()
        if result:
            self._build_panels()
            self._build_sidebar()

    def _on_delete_dashboard_confirm(self, did: str, confirmed: bool) -> None:
        if not confirmed:
            return
        try:
            delete_dashboard(did)
            self.notify(f"Deleted dashboard '{did}'")
            self._current_dashboard = get_active_dashboard_id()
            self._build_panels()
            self._build_sidebar()
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    def action_delete_focused(self) -> None:
        """Context-aware delete: session panel, sidebar script, routine, or session."""
        focused = self.app.focused
        if isinstance(focused, SessionPanel):
            sid = focused.session_id
            self.app.push_screen(
                ConfirmScreen(f"Delete session '{sid}'?"),
                callback=partial(self._on_delete_confirm, sid),
            )
        elif isinstance(focused, SidebarItem):
            if focused.item_type == "session" and focused.item_id:
                sid = focused.item_id
                self.app.push_screen(
                    ConfirmScreen(f"Delete session '{sid}'?"),
                    callback=partial(self._on_delete_confirm, sid),
                )
            elif focused.item_type == "script" and focused.item_id:
                name = focused.item_id
                self.app.push_screen(
                    ConfirmScreen(f"Delete script '{name}'?"),
                    callback=partial(self._on_delete_script_confirm, name),
                )
            elif focused.item_type == "macro" and focused.item_id:
                mid = focused.item_id
                self.app.push_screen(
                    ConfirmScreen(f"Delete macro '{mid}'?"),
                    callback=partial(self._on_delete_macro_confirm, mid),
                )
            elif focused.item_type == "routine" and focused.item_id:
                rid = focused.item_id
                self.app.push_screen(
                    ConfirmScreen(f"Delete routine '{rid}'?"),
                    callback=partial(self._on_delete_routine_confirm, rid),
                )
            elif focused.item_type == "dashboard" and focused.item_id:
                did = focused.item_id
                self.app.push_screen(
                    ConfirmScreen(f"Delete dashboard '{did}'? Sessions will not be deleted."),
                    callback=partial(self._on_delete_dashboard_confirm, did),
                )
            else:
                self.notify("Nothing to delete here", severity="warning")
        else:
            self.notify("Focus a session or sidebar item first", severity="warning")

    def _on_delete_confirm(self, sid: str, confirmed: bool) -> None:
        if not confirmed:
            return
        try:
            if is_running(sid):
                tmux_stop(sid)
            delete_session(sid)
            self.notify(f"Deleted '{sid}'")
            self._build_panels()
            self._build_sidebar()
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    def _on_delete_routine_confirm(self, rid: str, confirmed: bool) -> None:
        if not confirmed:
            return
        try:
            delete_routine(rid)
            self.notify(f"Deleted routine '{rid}'")
            self._build_sidebar()
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    # ── Start / Stop ──

    def action_start_session(self) -> None:
        sid = self._focused_session_id()
        if not sid:
            self.notify("Focus a session panel first", severity="warning")
            return
        if is_running(sid):
            self.notify(f"'{sid}' already running", severity="warning")
            return
        self.notify(f"Starting '{sid}'...")
        self.run_worker(
            partial(self._start_session_blocking, sid),
            thread=True,
            group="session-start",
        )

    @staticmethod
    def _start_session_blocking(session_id: str) -> str:
        tmux_start(session_id)
        return session_id

    def action_stop_session(self) -> None:
        sid = self._focused_session_id()
        if not sid:
            self.notify("Focus a session panel first", severity="warning")
            return
        if not is_running(sid):
            self.notify(f"'{sid}' is not running", severity="warning")
            return
        try:
            tmux_stop(sid)
            self.notify(f"Stopped '{sid}'")
            self._refresh_panels()
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    # ── Interactive / Message ──

    def action_interactive(self) -> None:
        sid = self._focused_session_id()
        if not sid:
            self.notify("Focus a session panel first", severity="warning")
            return
        if not is_running(sid):
            self.notify(f"'{sid}' is not running", severity="warning")
            return
        with self.app.suspend():
            enter_session(sid)
        self._build_panels()

    def action_send_message(self) -> None:
        sid = self._focused_session_id()
        if not sid:
            self.notify("Focus a session panel first", severity="warning")
            return
        if not is_running(sid):
            self.notify(f"'{sid}' is not running", severity="warning")
            return
        self.app.push_screen(SendMessageScreen(sid))

    # ── Scripts ──

    def action_run_script(self) -> None:
        sid = self._focused_session_id()
        if not sid:
            self.notify("Focus a session panel first", severity="warning")
            return
        if not is_running(sid):
            self.notify(f"'{sid}' is not running", severity="warning")
            return
        self.app.push_screen(RunScriptScreen(sid), callback=self._on_run_script)

    def _on_run_script(self, result) -> None:
        if result and isinstance(result, tuple) and result[0] == "run":
            _, script_name, session_id = result
            self.notify(f"Running '{script_name}' on '{session_id}'...")
            self.run_worker(
                partial(self._run_script_blocking, script_name, session_id),
                thread=True,
                group="script-run",
            )

    @staticmethod
    def _run_script_blocking(script_name: str, session_id: str) -> tuple:
        result = run_script(script_name, session_id)
        return (script_name, session_id, result)

    def action_new_script(self) -> None:
        self.app.push_screen(CreateScriptScreen(), callback=self._on_new_script)

    def _on_new_script(self, result) -> None:
        if result and isinstance(result, tuple) and result[0] == "new-script":
            name = result[1]
            self.app.push_screen(
                ScriptEditorScreen(name, is_new=True),
                callback=lambda _: self._build_sidebar(),
            )

    def action_edit_script(self) -> None:
        scripts = list_scripts()
        if not scripts:
            self.notify("No scripts to edit", severity="warning")
            return
        if len(scripts) == 1:
            self.app.push_screen(
                ScriptEditorScreen(scripts[0]),
                callback=lambda _: self._build_sidebar(),
            )
        else:
            self.app.push_screen(ScriptPickerScreen(), callback=self._on_script_picked)

    def _on_script_picked(self, result) -> None:
        if result:
            self.app.push_screen(
                ScriptEditorScreen(result),
                callback=lambda _: self._build_sidebar(),
            )

    def _on_delete_script_confirm(self, name: str, confirmed: bool) -> None:
        if not confirmed:
            return
        try:
            delete_script(name)
            self.notify(f"Deleted script '{name}'")
            self._build_sidebar()
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    # ── Macros ──

    def action_run_macro(self) -> None:
        """Pick a macro to send to the focused session."""
        sid = self._focused_session_id()
        if not sid:
            self.notify("Focus a session panel first", severity="warning")
            return
        if not is_running(sid):
            self.notify(f"'{sid}' is not running", severity="warning")
            return
        macros = list_macros()
        if not macros:
            self.notify("No macros. Navigate to + Macro in sidebar to create one.", severity="warning")
            return
        options = [(m.get("name", mid), mid) for mid, m in macros.items()]
        self.app.push_screen(
            MacroPickerScreen(sid, options),
            callback=self._on_macro_picked,
        )

    def _on_macro_picked(self, result) -> None:
        if result and isinstance(result, tuple):
            macro_id, session_id = result
            self._send_macro(macro_id, session_id)

    def _send_macro(self, macro_id: str, session_id: str | None = None) -> None:
        """Send a macro's keys to a session."""
        m = get_macro(macro_id)
        if not m:
            self.notify(f"Macro '{macro_id}' not found", severity="error")
            return
        sid = session_id or self._focused_session_id()
        if not sid:
            self.notify("Focus a session panel first", severity="warning")
            return
        if not is_running(sid):
            self.notify(f"'{sid}' is not running", severity="warning")
            return
        keys = m["keys"]
        do_enter = m.get("enter", True)
        if "\n" in keys:
            # Multi-line: paste as block so newlines stay as-is
            from core.tmux_manager import send_text_block as paste_block
            paste_block(sid, keys)
        else:
            send_keys(sid, keys, enter=False)
        if do_enter:
            send_keys(sid, "", enter=True)
        self.notify(f"Sent '{m.get('name', macro_id)}' to {sid}")

    def _on_delete_macro_confirm(self, mid: str, confirmed: bool) -> None:
        if not confirmed:
            return
        try:
            delete_macro(mid)
            self.notify(f"Deleted macro '{mid}'")
            self._build_sidebar()
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    # ── Routines ──

    def action_new_routine(self) -> None:
        self.app.push_screen(CreateRoutineScreen(), callback=self._on_form_done)

    def _resize_panels(self, delta: int) -> None:
        panels = list(self.query("SessionPanel"))
        if not panels:
            return
        current = panels[0].styles.height
        if current is not None and current.value:
            h = int(current.value)
        else:
            h = PANEL_HEIGHT_DEFAULT
        h = max(PANEL_HEIGHT_MIN, min(PANEL_HEIGHT_MAX, h + delta))
        for p in panels:
            p.styles.height = h

    def action_grow_panels(self) -> None:
        self._resize_panels(PANEL_HEIGHT_STEP)

    def action_shrink_panels(self) -> None:
        self._resize_panels(-PANEL_HEIGHT_STEP)

    def action_toggle_daemon(self) -> None:
        app = self.app
        if app.routines_daemon.is_active:
            app.routines_daemon.stop()
            self.notify("Routines stopped")
        else:
            app.routines_daemon.start()
            self.notify("Routines running")

    def _get_panel_grid(self) -> list[list[SessionPanel]]:
        """Get panels organized as a 2D grid (rows of up to 2)."""
        panels = list(self.query("SessionPanel"))
        grid = []
        for i in range(0, len(panels), 2):
            grid.append(panels[i : i + 2])
        return grid

    def _find_panel_pos(self, panel: SessionPanel) -> tuple[int, int] | None:
        """Find (row, col) of a panel in the grid."""
        grid = self._get_panel_grid()
        for r, row in enumerate(grid):
            for c, p in enumerate(row):
                if p is panel:
                    return (r, c)
        return None

    def _sidebar_items(self) -> list[SidebarItem]:
        return list(self.query("SidebarItem"))

    def _focus_first_sidebar_item(self) -> None:
        items = self._sidebar_items()
        if items:
            items[0].focus()

    def action_activate_item(self) -> None:
        focused = self.app.focused
        if isinstance(focused, SidebarItem):
            if focused.item_type == "session" and focused.item_id:
                self.app.push_screen(
                    EditSessionScreen(focused.item_id),
                    callback=self._on_form_done,
                )
            elif focused.item_type == "script" and focused.item_id:
                self.app.push_screen(
                    ScriptEditorScreen(focused.item_id),
                    callback=lambda _: self._build_sidebar(),
                )
            elif focused.item_type == "macro" and focused.item_id:
                self.app.push_screen(
                    EditMacroScreen(focused.item_id),
                    callback=self._on_form_done,
                )
            elif focused.item_type == "routine" and focused.item_id:
                self.app.push_screen(
                    EditRoutineScreen(focused.item_id),
                    callback=self._on_form_done,
                )
            elif focused.item_type == "dashboard" and focused.item_id:
                self._switch_dashboard(focused.item_id)
            elif focused.item_type == "add-dashboard":
                self.app.push_screen(CreateDashboardScreen(), callback=self._on_dashboard_form_done)
            elif focused.item_type == "add-session":
                self.action_new_session()
            elif focused.item_type == "add-script":
                self.action_new_script()
            elif focused.item_type == "add-macro":
                self.app.push_screen(CreateMacroScreen(), callback=self._on_form_done)
            elif focused.item_type == "add-routine":
                self.action_new_routine()
            elif focused.item_type == "daemon-toggle":
                self.action_toggle_daemon()
        elif isinstance(focused, SessionPanel):
            self.action_interactive()

    def action_nav_left(self) -> None:
        focused = self.app.focused
        if isinstance(focused, SessionPanel):
            pos = self._find_panel_pos(focused)
            if pos:
                r, c = pos
                if c > 0:
                    grid = self._get_panel_grid()
                    grid[r][c - 1].focus()
                else:
                    self._focus_first_sidebar_item()

    def action_nav_right(self) -> None:
        focused = self.app.focused
        if isinstance(focused, SidebarItem):
            self._focus_first_panel()
        elif isinstance(focused, SessionPanel):
            pos = self._find_panel_pos(focused)
            if pos:
                r, c = pos
                grid = self._get_panel_grid()
                if c + 1 < len(grid[r]):
                    grid[r][c + 1].focus()

    def action_nav_up(self) -> None:
        focused = self.app.focused
        if isinstance(focused, SidebarItem):
            items = self._sidebar_items()
            idx = items.index(focused)
            if idx > 0:
                items[idx - 1].focus()
        elif isinstance(focused, SessionPanel):
            pos = self._find_panel_pos(focused)
            if pos:
                r, c = pos
                if r > 0:
                    grid = self._get_panel_grid()
                    target_row = grid[r - 1]
                    target_col = min(c, len(target_row) - 1)
                    target_row[target_col].focus()

    def action_nav_down(self) -> None:
        focused = self.app.focused
        if isinstance(focused, SidebarItem):
            items = self._sidebar_items()
            idx = items.index(focused)
            if idx < len(items) - 1:
                items[idx + 1].focus()
        elif isinstance(focused, SessionPanel):
            pos = self._find_panel_pos(focused)
            if pos:
                r, c = pos
                grid = self._get_panel_grid()
                if r + 1 < len(grid):
                    target_row = grid[r + 1]
                    target_col = min(c, len(target_row) - 1)
                    target_row[target_col].focus()

    def action_toggle_sidebar_focus(self) -> None:
        focused = self.app.focused
        if isinstance(focused, SidebarItem):
            self._focus_first_panel()
        else:
            self._focus_first_sidebar_item()

    def action_quit_app(self) -> None:
        self.app.exit()

    # ── Worker callbacks ──

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.group == "session-start":
            if event.state == WorkerState.SUCCESS:
                sid = event.worker.result
                self.notify(f"Session '{sid}' started")
                self._refresh_panels()
            elif event.state == WorkerState.ERROR:
                self.notify(f"Start failed: {event.worker.error}", severity="error")

        elif event.worker.group == "panel-capture":
            if event.state == WorkerState.SUCCESS:
                sid, content = event.worker.result
                try:
                    panel = self.query_one(f"#panel-{sid}", SessionPanel)
                    panel.update_content(content, running=True)
                except NoMatches:
                    pass

        elif event.worker.group == "script-run":
            if event.state == WorkerState.SUCCESS:
                script_name, session_id, result = event.worker.result
                status = result.get("status", "?") if isinstance(result, dict) else str(result)
                self.notify(f"Script '{script_name}' on '{session_id}': {status}")
            elif event.state == WorkerState.ERROR:
                self.notify(f"Script failed: {event.worker.error}", severity="error")


class ScriptPickerScreen(FormScreen):
    """Pick a script to edit."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        scripts = list_scripts()
        options = [(s, s) for s in scripts]

        with Vertical(classes="modal-dialog"):
            yield Static("[bold]Edit Script[/bold]", classes="form-title")
            yield Select(options, id="f-script", prompt="Select a script")
            with Horizontal(classes="button-row"):
                yield Button("Open", variant="primary", id="btn-open")
                yield Button("Cancel", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-open":
            select = self.query_one("#f-script", Select)
            if select.value is not Select.BLANK:
                self.dismiss(select.value)
            else:
                self.notify("Select a script first", severity="warning")
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# Shortcut keys: 1-9, then a-z
_SHORTCUT_KEYS = [str(i) for i in range(1, 10)] + [chr(c) for c in range(ord("a"), ord("z") + 1)]


class MacroPickerScreen(ModalScreen):
    """Pick a macro to send to a session. Press a shortcut key or use arrows + Enter."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("up", "nav_up", show=False),
        Binding("down", "nav_down", show=False),
        Binding("enter", "select_focused", show=False),
    ]

    def __init__(self, session_id: str, options: list[tuple[str, str]]):
        super().__init__()
        self.session_id = session_id
        self._options = options  # [(display_name, macro_id), ...]
        # Map shortcut key → macro_id
        self._key_map: dict[str, str] = {}
        for i, (_, macro_id) in enumerate(options):
            if i < len(_SHORTCUT_KEYS):
                self._key_map[_SHORTCUT_KEYS[i]] = macro_id

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="modal-dialog macro-picker"):
            yield Static(
                f"[bold]Run Macro on: {self.session_id}[/bold]",
                classes="form-title",
            )
            for i, (name, macro_id) in enumerate(self._options):
                if i < len(_SHORTCUT_KEYS):
                    key = _SHORTCUT_KEYS[i]
                    label = f"  [bold cyan]{key}[/bold cyan]  {name}"
                else:
                    label = f"      {name}"
                item = Static(label, id=f"mp-{macro_id}", classes="macro-pick-item")
                item.can_focus = True
                item._macro_id = macro_id
                yield item
            yield Static("")
            yield Static("[dim]Press key to send, arrows+enter to select, esc to cancel[/dim]",
                         classes="macro-pick-hint")

    def on_key(self, event) -> None:
        key = event.key
        if key in self._key_map:
            event.prevent_default()
            event.stop()
            self.dismiss((self._key_map[key], self.session_id))

    def action_nav_up(self) -> None:
        items = [w for w in self.query(".macro-pick-item") if w.can_focus]
        if not items:
            return
        focused = self.app.focused
        if focused in items:
            idx = items.index(focused)
            items[(idx - 1) % len(items)].focus()
        else:
            items[-1].focus()

    def action_nav_down(self) -> None:
        items = [w for w in self.query(".macro-pick-item") if w.can_focus]
        if not items:
            return
        focused = self.app.focused
        if focused in items:
            idx = items.index(focused)
            items[(idx + 1) % len(items)].focus()
        else:
            items[0].focus()

    def action_select_focused(self) -> None:
        focused = self.app.focused
        if hasattr(focused, "_macro_id"):
            self.dismiss((focused._macro_id, self.session_id))

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── Application ──────────────────────────────────────────────────────


class TinyAgenticApp(App):
    TITLE = "TinyAgentic.ai"
    SUB_TITLE = "Session Manager"

    CSS = """
    /* ── Global ──────────────────────────── */
    Screen {
        background: $surface;
    }

    /* ── Main layout ─────────────────────── */
    #main-layout {
        height: 1fr;
    }

    #sidebar {
        width: 28;
        min-width: 24;
        height: 1fr;
        border-right: solid $accent;
        padding: 1 0;
        overflow-y: auto;
    }

    .sidebar-header {
        padding: 0 1;
        height: auto;
    }

    .sidebar-item {
        padding: 0 1;
        height: auto;
    }

    SidebarItem:focus {
        background: $accent;
    }

    .sidebar-add {
        margin-top: 0;
    }

    #session-grid {
        width: 1fr;
        height: 1fr;
        padding: 0 1;
    }

    /* ── Session panels ──────────────────── */
    .panel-row {
        height: auto;
    }

    .session-panel {
        width: 1fr;
        height: 24;
        margin: 0 1 1 0;
    }

    .session-panel:focus {
        border: heavy cyan;
    }

    .panel-spacer {
        width: 1fr;
    }

    /* ── Modal dialogs ───────────────────── */
    CreateDashboardScreen,
    EditDashboardScreen,
    CreateSessionScreen,
    EditSessionScreen,
    SendMessageScreen,
    RunScriptScreen,
    CreateScriptScreen,
    CreateMacroScreen,
    EditMacroScreen,
    MacroPickerScreen,
    CreateRoutineScreen,
    EditRoutineScreen,
    ScriptPickerScreen,
    ConfirmScreen {
        align: center middle;
    }

    .modal-dialog {
        width: 72;
        max-width: 90%;
        max-height: 85%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }

    .message-dialog {
        width: 60;
    }

    .macro-picker {
        width: 56;
        max-height: 70%;
    }

    .macro-pick-item {
        padding: 0 1;
        height: auto;
    }

    .macro-pick-item:focus {
        background: $accent;
    }

    .macro-pick-hint {
        padding: 0 1;
        height: auto;
    }

    .form-title {
        width: 100%;
        text-align: center;
        text-style: bold;
        margin-bottom: 1;
        color: $accent;
    }

    .modal-dialog Label {
        margin-top: 1;
        color: $text;
    }

    .modal-dialog Input {
        width: 100%;
    }

    .modal-dialog Select {
        width: 100%;
    }

    .macro-editor {
        height: 8;
        min-height: 4;
        width: 100%;
    }

    .button-row {
        width: 100%;
        height: auto;
        align: center middle;
        margin-top: 2;
    }

    .button-row Button {
        margin: 0 2;
    }

    .switch-row {
        height: 3;
        margin-top: 1;
    }

    .switch-label {
        width: auto;
        margin-right: 2;
        padding-top: 1;
    }

    /* ── Session variables ───────────────── */
    .form-subtitle {
        padding: 0 0;
        height: auto;
    }

    #vars-container {
        height: auto;
        max-height: 20;
    }

    .var-row {
        height: 3;
        margin-bottom: 0;
    }

    .var-name-input {
        width: 1fr;
        margin-right: 1;
    }

    .var-val-input {
        width: 2fr;
        margin-right: 1;
    }

    .var-del-btn {
        width: 5;
        min-width: 5;
        margin: 0;
    }

    .add-var-btn {
        margin-top: 1;
        width: auto;
    }

    /* ── Confirm dialog ──────────────────── */
    .confirm-dialog {
        width: 56;
        max-width: 80%;
        height: auto;
        border: thick $warning;
        background: $surface;
        padding: 2 3;
    }

    .confirm-message {
        width: 100%;
        text-align: center;
        margin-bottom: 1;
    }

    /* ── Script editor ───────────────────── */
    #script-header {
        height: auto;
        margin: 1 2 0 2;
        padding: 0 1;
    }

    #script-editor {
        height: 1fr;
        margin: 1 2;
    }

    /* ── Status Bar ──────────────────────── */
    #status-bar {
        height: 1;
        padding: 0 2;
        background: $panel;
        border-top: solid $primary;
        text-align: left;
        dock: bottom;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.routines_daemon = RoutinesDaemon()
        self.routines_daemon.idle_checker = self._check_session_idle

    def _check_session_idle(self, session_id: str) -> bool:
        """Called by the daemon thread to check if a session panel is idle."""
        try:
            screen = self.screen
            return screen._idle_states.get(session_id, True)
        except Exception:
            return True

    def on_mount(self) -> None:
        load_config()
        self.routines_daemon.start()
        self.push_screen(MainScreen())


if __name__ == "__main__":
    app = TinyAgenticApp()
    app.run()
