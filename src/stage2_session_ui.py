"""Read-only viewer for retained Stage 2 run sessions.

The state bridge is the only data source.  This module never constructs an
engine, calls a provider, takes a lock, submits/applies work, or changes files.
"""
from __future__ import annotations

import importlib
import inspect
import os
from pathlib import Path
import tkinter as tk
from tkinter import ttk

from stage2_theme import resolve_palette, tk_colours


def _ctx(context, key, default=None):
    if isinstance(context, dict):
        return context.get(key, default)
    return getattr(context, key, default) if context is not None else default


def _colours(context):
    if isinstance(context, dict) and "BG" in context:
        return {key: context.get(key, value) for key, value in tk_colours().items()}
    return tk_colours(_ctx(context, "palette", None))


def _style_button(context, widget, primary=False):
    styler = _ctx(context, "style_button")
    colours = _colours(context)
    if callable(styler):
        styler(widget, colours["PRIMARY"] if primary else colours["RAISED"], colours["PANEL"])
    else:
        widget.configure(bg=colours["PRIMARY"] if primary else colours["RAISED"], fg=colours["PRIMARY_TEXT"] if primary else colours["FG"], relief="flat")
    return widget


def _observer(context, care_home_dir=None):
    existing = _ctx(context, "run_observer")
    if existing is not None:
        return existing
    module = importlib.import_module("stage2_run_state")
    cls = getattr(module, "RunObserver")
    signature = inspect.signature(cls)
    registry_root = _ctx(context, "registry_root")
    if "registry_root" in signature.parameters and registry_root is not None:
        return cls(registry_root=registry_root)
    return cls()


def _invoke(observer, name, *args, **kwargs):
    """Invoke only the agreed RunObserver method signature.

    Missing methods and incompatible bridge signatures are programmer errors,
    not an empty run list.  This avoids silently hiding a broken integration.
    """
    fn = getattr(observer, name)
    signature = inspect.signature(fn)
    accepted = {key: value for key, value in kwargs.items() if key in signature.parameters}
    positional = list(args[:len([p for p in signature.parameters.values() if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)])])
    return fn(*positional, **accepted)


def _status(item):
    state = str(item.get("display_state", "")).lower()
    if state in {"running", "active", "started"}:
        return "running"
    if state in {"failed", "error", "crashed"}:
        return "failed"
    if state in {"owner-ended", "owner_ended"}:
        return "owner-ended"
    if state in {"operation-ended", "operation_ended", "exited"}:
        return "operation-ended"
    return "stale-unverified"


def _event_text(event):
    if isinstance(event, str):
        return event[:300]
    kind = event.get("kind", "event")
    stamp = event.get("timestamp_utc", "")
    phase = event.get("phase", "")
    progress = ""
    if "completed" in event or "total" in event:
        progress = f" {event.get('completed', '?')}/{event.get('total', '?')}"
    suffix = f" phase={phase}" if phase else ""
    return f"{stamp} {kind}{progress}{suffix}"[:300]


def _format_details(detail, timeline):
    owner = detail.get("owner") if isinstance(detail.get("owner"), dict) else {}
    completed, total = detail.get("completed", "unknown"), detail.get("total", "unknown")
    age = detail.get("heartbeat_age_seconds", "unknown")
    lines = [f"State: {_status(detail)}", f"PID: {owner.get('pid', 'unknown')}",
             f"Started: {detail.get('started_utc', 'unknown')}", f"Mode: {detail.get('mode', 'unknown')}",
             f"Care-home: {detail.get('care_home_dir', 'unknown')}", f"Destination: {detail.get('destination') or 'none'}",
             f"Phase: {detail.get('phase', 'unknown')}", f"Counts: {completed} / {total}",
             f"Current document: {detail.get('current_document') or 'none reported'}",
             f"Estimated cost: GBP {detail.get('cost_gbp', 'unknown')}", f"Heartbeat age: {age} seconds",
             f"Last activity: {detail.get('last_activity', 'unknown')}",
             f"Operation outcome: {detail.get('operation_outcome') or 'not recorded'}",
             f"Error: {detail.get('error') or 'none recorded'}",
             "Watch only: operation-ended is not pipeline-verified-complete.", "", "Timeline:"]
    lines.extend(_event_text(x) for x in timeline[-100:] if isinstance(x, (str, dict)))
    return "\n".join(lines)


def ask_close_running(master, context):
    """Return ``minimise``, ``stop`` or ``cancel``; minimise is safest default."""
    colours = _colours(context)
    result = {"value": "minimise"}
    win = tk.Toplevel(master)
    win.title("Run still active")
    win.configure(bg=colours["BG"])
    titlebar = _ctx(context, "style_titlebar_black")
    if callable(titlebar):
        win.after(0, lambda: titlebar(win))
    win.transient(master)
    tk.Label(win, text="A retained run is still active.", bg=colours["BG"], fg=colours["FG"],
             font=("Segoe UI", 11, "bold")).pack(padx=24, pady=(20, 8))
    tk.Label(win, text="Minimise keeps the worker attached. Stop requests a safe stop.",
             bg=colours["BG"], fg=colours["MUTED"], wraplength=360).pack(padx=24, pady=4)
    row = tk.Frame(win, bg=colours["BG"]); row.pack(padx=18, pady=18)
    def choose(value):
        result["value"] = value
        win.destroy()
    buttons = []
    for label, value in (("Minimise and keep running", "minimise"), ("Stop safely and close", "stop"), ("Cancel", "cancel")):
        button = tk.Button(row, text=label, command=lambda v=value: choose(v), padx=12, pady=6)
        _style_button(context, button, primary=value == "minimise").pack(side="left", padx=4)
        buttons.append(button)
    win.protocol("WM_DELETE_WINDOW", lambda: choose("cancel"))
    buttons[0].focus_set()
    win.bind("<Return>", lambda _e: choose("minimise"))
    win.grab_set()
    master.wait_window(win)
    return result["value"]


class SessionViewer:
    def __init__(self, master, context, care_home_dir=None):
        self.master, self.context, self.care_home_dir = master, context, care_home_dir
        self.c = _colours(context)
        self.win = tk.Toplevel(master)
        self.win.title("Saved run sessions - read only")
        self.win.configure(bg=self.c["BG"])
        titlebar = _ctx(context, "style_titlebar_black")
        if callable(titlebar):
            titlebar(self.win)
        self.win.geometry("900x600")
        self.win.minsize(720, 420)
        self.sessions = []
        self.observer = _observer(context, care_home_dir)
        self._build()
        self.refresh()
        self._tick()

    def _build(self):
        top = tk.Frame(self.win, bg=self.c["BG"]); top.pack(fill="x", padx=14, pady=12)
        tk.Label(top, text="SAVED RUN SESSIONS", bg=self.c["BG"], fg=self.c["FG"], font=("Segoe UI", 13, "bold")).pack(side="left")
        tk.Label(top, text="READ ONLY / WATCH ONLY", bg=self.c["BG"], fg=self.c["MUTED"]).pack(side="left", padx=15)
        _style_button(self.context, tk.Button(top, text="Refresh", command=self.refresh)).pack(side="right")
        foot = tk.Frame(self.win, bg=self.c["BG"]); foot.pack(side="bottom", fill="x", padx=14, pady=10)
        self.footer = foot
        _style_button(self.context, tk.Button(foot, text="Open log folder", command=self.open_log_folder)).pack(side="left")
        _style_button(self.context, tk.Button(foot, text="Close", command=self.win.destroy)).pack(side="right")
        body = tk.Frame(self.win, bg=self.c["BG"]); body.pack(fill="both", expand=True, padx=14)
        self.body = body
        left = tk.Frame(body, bg=self.c["PANEL"], width=300); left.pack(side="left", fill="y")
        left_scroll = tk.Scrollbar(left, orient="vertical")
        left_scroll.pack(side="right", fill="y")
        self.listbox = tk.Listbox(left, width=32, bg=self.c["PANEL"], fg=self.c["FG"], selectbackground=self.c["RAISED"], relief="flat", exportselection=False)
        self.listbox.configure(yscrollcommand=left_scroll.set)
        left_scroll.configure(command=self.listbox.yview)
        self.listbox.pack(side="left", fill="both", expand=True, padx=6, pady=6); self.listbox.bind("<<ListboxSelect>>", lambda _e: self._show_selected())
        right = tk.Frame(body, bg=self.c["PANEL"]); right.pack(side="left", fill="both", expand=True, padx=(12, 0))
        text_frame = tk.Frame(right, bg=self.c["PANEL"]); text_frame.pack(fill="both", expand=True, padx=10, pady=10)
        scroll = tk.Scrollbar(text_frame, command=lambda *args: self.details.yview(*args))
        scroll.pack(side="right", fill="y")
        self.details = tk.Text(text_frame, bg=self.c["PANEL"], fg=self.c["FG"], insertbackground=self.c["FG"], relief="flat", wrap="word", yscrollcommand=scroll.set)
        self.details.pack(side="left", fill="both", expand=True); self.details.configure(state="disabled")

    def refresh(self):
        selected_id = None
        selection = self.listbox.curselection()
        if selection and selection[0] < len(self.sessions):
            selected_id = self.sessions[selection[0]].get("run_id")
        data = _invoke(self.observer, "list_runs", self.care_home_dir)
        self.sessions = [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []
        self.listbox.delete(0, "end")
        for item in self.sessions:
            care_home = Path(str(item.get("care_home_dir", ""))).name or "care home unknown"
            started = str(item.get("started_utc", "unknown")).replace("T", " ")[:16]
            label = f"{care_home} | {started} | {_status(item)}"
            self.listbox.insert("end", label[:120])
        if self.sessions:
            index = next((i for i, item in enumerate(self.sessions) if item.get("run_id") == selected_id), 0)
            self.listbox.selection_set(index); self.listbox.see(index); self._show_selected(preserve_scroll=selected_id is not None)
        else:
            self._write("No saved session evidence was found.\nNo processing state is inferred from absence.")

    def _show_selected(self, preserve_scroll=False):
        selection = self.listbox.curselection()
        if not selection: return
        item = self.sessions[selection[0]]
        detail = _invoke(self.observer, "get", item.get("run_id"))
        if not isinstance(detail, dict): detail = item
        timeline = _invoke(self.observer, "read_events", item.get("run_id"), limit=100)
        self._write(_format_details(detail, timeline), preserve_scroll=preserve_scroll)

    def _write(self, text, preserve_scroll=False):
        view = self.details.yview() if preserve_scroll else (0.0, 1.0)
        self.details.configure(state="normal"); self.details.delete("1.0", "end"); self.details.insert("1.0", text); self.details.configure(state="disabled")
        if preserve_scroll:
            self.details.yview_moveto(view[0])

    def open_log_folder(self):
        selection = self.listbox.curselection()
        item = self.sessions[selection[0]] if selection else {}
        log_method = getattr(self.observer, "log_folder", None)
        folder = _invoke(self.observer, "log_folder", item.get("run_id")) if callable(log_method) else getattr(self.observer, "registry_root", None)
        if folder and Path(str(folder)).is_dir():
            os.startfile(str(folder))

    def _tick(self):
        if self.win.winfo_exists():
            self.refresh()
            self.win.after(1000, self._tick)


def open_sessions(master, context, care_home_dir=None):
    """Open the read-only retained-session viewer and return its dialog."""
    return SessionViewer(master, context, care_home_dir).win
