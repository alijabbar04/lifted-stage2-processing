"""Obsidian Compact Tk view. Processing stays in the existing Engine.

The main view is deliberately small; the retained full log and counters live
in a reusable details window. Nothing in this module classifies or renames.
"""
import base64
import collections
import datetime
import io
import queue
import threading
from pathlib import Path
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from stage2_progress import PhaseProgress, concise_duration
from stage2_theme import resolve_palette

_DEFAULT_PALETTE = resolve_palette("C")
BG = _DEFAULT_PALETTE.bg
PANEL = _DEFAULT_PALETTE.panel
RAISED = _DEFAULT_PALETTE.raised
LINE = _DEFAULT_PALETTE.line
TEXT = _DEFAULT_PALETTE.text
MUTED = _DEFAULT_PALETTE.muted
SILVER = _DEFAULT_PALETTE.primary
ATTENTION = _DEFAULT_PALETTE.attention


def _set_compat_palette(palette):
    """Keep legacy helpers in this module on the selected palette."""
    global BG, PANEL, RAISED, LINE, TEXT, MUTED, SILVER, ATTENTION
    BG, PANEL, RAISED, LINE = palette.bg, palette.panel, palette.raised, palette.line
    TEXT, MUTED, SILVER, ATTENTION = palette.text, palette.muted, palette.primary, palette.attention
FIELDS = [
    ("Workers completed", "workers"), ("Converted to PDF", "converted"),
    ("Renamed", "renamed"), ("Unknowns defined", "unknown"),
    ("Ranked groups", "ranked"), ("Overwrite filed", "overwrite"),
    ("Bulk filed", "bulk"), ("CoS dated", "cos"),
    ("Contracts signed", "contracts"), ("DBS ranked", "dbs"),
    ("ECS latest", "ecs"), ("BRP latest", "brp"),
    ("eVisa latest", "evisa"), ("NI Number ranked", "ni"),
    ("Share Code dated", "sharecode"), ("Duplicates removed", "duplicates"),
    ("Conversion failed", "convert_failed"), ("Cached / skipped", "skipped_cached"),
    ("Oversized skipped", "skipped_oversized"), ("Page-1 only", "page1_only"),
    ("API skipped", "skipped_api"), ("Errors", "errors"),
    ("Audited", "audited"), ("Audit needs review", "audit_flagged"),
]


def label(parent, text="", *, dim=False, size=10, **kwargs):
    return tk.Label(parent, text=text, bg=parent.cget("bg"),
                    fg=MUTED if dim else TEXT, font=("Segoe UI", size), **kwargs)


def button(parent, text, command, *, primary=False, **kwargs):
    # Native Button keeps keyboard focus/invoke and disabled semantics.  Tk does
    # not provide a dependable rounded button across Windows themes, so use a
    # deliberately filled, bounded control rather than a canvas imitation.
    bg, fg = (SILVER, BG) if primary else (RAISED, TEXT)
    options = {"font": ("Segoe UI", 10), "padx": 12, "pady": 7, "cursor": "hand2"}
    options.update(kwargs)
    result = tk.Button(parent, text=text, command=command, bg=bg, fg=fg,
        activebackground="#ffffff" if primary else PANEL,
        activeforeground=BG if primary else TEXT, disabledforeground=MUTED,
        relief="flat", bd=0, highlightthickness=1, highlightbackground=LINE,
        highlightcolor=SILVER, takefocus=True, **options)
    result._stage2_primary = primary
    return result


def _paint_button(widget, palette):
    """Keep native disabled buttons legible without leaving a green dead-end."""
    primary = widget._stage2_primary
    disabled = str(widget.cget("state")) == "disabled"
    widget.configure(
        bg=palette.raised if disabled else (palette.primary if primary else palette.raised),
        fg=palette.muted if disabled else (palette.primary_text if primary else palette.text),
        activebackground=palette.panel if disabled or not primary else "#ffffff",
        activeforeground=palette.muted if disabled else (palette.primary_text if primary else palette.text),
        disabledforeground=palette.muted,
        highlightbackground=palette.line,
        highlightcolor=palette.primary,
    )


def separator(parent):
    return tk.Frame(parent, bg=LINE, height=1)


class CompactDashboard:
    def __init__(self, app, namespace):
        self.app, self.ns = app, namespace
        # Build against C first, then repaint the entire native widget tree if
        # Settings has selected A or B.  This keeps inherited Tk colours in
        # sync instead of leaving mixed parent/child surfaces.
        self.palette = resolve_palette("C")
        self._requested_palette = app.cfg.get("ui_palette", "C")
        _set_compat_palette(self.palette)
        self.progress = PhaseProgress()
        self.events = collections.deque(maxlen=150)
        self._preview_on = bool(app.cfg.get("show_document_preview", False))
        self._preview_path = ""
        self._preview_generation = 0
        self._preview_queue = queue.Queue()
        self._audit_path = ""
        self._last_worker_count = None
        self._last_context = None
        self._home_full_name = "No care home selected"
        self._closed = False
        self.structured_progress = False
        self._terminal_status = ""
        self._build_styles()
        self._build()
        app.bind("<Destroy>", self._destroyed, add="+")
        self._tick_id = app.after(500, self._tick)

    def _destroyed(self, event):
        if event.widget is self.app:
            self._closed = True
            try:
                self.app.after_cancel(self._tick_id)
            except tk.TclError:
                pass

    def _build_styles(self):
        palette = self.palette
        style = ttk.Style(self.app)
        style.theme_use("clam")
        style.configure("Obsidian.Horizontal.TProgressbar", troughcolor=palette.line,
                        background=palette.primary, borderwidth=0, lightcolor=palette.primary,
                        darkcolor=palette.primary, thickness=5)
        style.layout("Obsidian.Horizontal.TProgressbar", [
            ("Horizontal.Progressbar.trough", {"sticky":"nswe", "children":[
                ("Horizontal.Progressbar.pbar", {"side":"left", "sticky":"ns"})]})])
        style.configure("TNotebook", background=palette.bg, borderwidth=0)
        style.configure("TNotebook.Tab", background=palette.panel, foreground=palette.muted, padding=(14, 8))
        style.map("TNotebook.Tab", background=[("selected", palette.raised)], foreground=[("selected", palette.text)])
        style.configure("TCombobox", fieldbackground=palette.raised, background=palette.panel,
                        foreground=palette.text, arrowcolor=palette.text)
        style.map("TCombobox", fieldbackground=[("readonly", palette.raised)], foreground=[("readonly", palette.text)])
        style.configure("Treeview", background=palette.panel, fieldbackground=palette.panel,
                        foreground=palette.text, borderwidth=0, rowheight=29)
        style.configure("Treeview.Heading", background=palette.raised, foreground=palette.text,
                        font=("Segoe UI", 10))

    def _build(self):
        app = self.app
        app.configure(bg=BG)
        top = tk.Frame(app, bg="#000000")
        top.pack(fill="x")
        brand = tk.Frame(top, bg="#000000")
        brand.pack(side="left", padx=(20, 12), pady=10)
        self._brand_icon = tk.PhotoImage(data=self.ns["APP_ICON_B64"]).subsample(3, 3)
        tk.Label(brand, image=self._brand_icon, bg="#000000").pack(side="left", padx=(0, 9))
        label(brand, "Stage 2", size=13).pack(side="left")
        label(brand, "  /  Processing", dim=True).pack(side="left")
        nav = tk.Frame(top, bg="#000000")
        nav.pack(side="right", padx=13)
        self.nav_buttons = {}
        actions = [("Jobs", app._open_jobs), ("Reports", app._open_reports),
                   ("API Usage", app._open_api_analytics), ("Tools", app._open_tools),
                   ("Guide", lambda:self.ns["open_stage_guide"](app)),
                   ("Settings", app._open_settings)]
        for name, action in actions:
            item = button(nav, name, action)
            item.pack(side="left", padx=2, pady=9)
            self.nav_buttons[name] = item
        separator(app).pack(fill="x")
        app.banner = label(app, dim=True, anchor="w", justify="left", wraplength=1100)
        app.banner.configure(fg=ATTENTION)
        app.banner.pack(fill="x", padx=24, pady=(6, 0))

        body = tk.Frame(app, bg=BG)
        body.pack(fill="both", expand=True, padx=26, pady=(8, 6))
        context = tk.Frame(body, bg=BG)
        context.pack(fill="x", pady=(0, 10))
        self.context = context
        context.columnconfigure(0, weight=1)
        context_text = tk.Frame(context, bg=BG)
        context_text.grid(row=0, column=0, sticky="ew")
        self.home_name = label(context_text, "No care home selected", size=20, anchor="w")
        self.home_name.pack(anchor="w")
        self.home_detail = label(context_text, "Choose the folder containing worker folders.", dim=True, size=9, anchor="w")
        self.home_detail.pack(anchor="w", pady=(4, 0))
        app.pick_btn = button(context, "Choose care-home folder", app._pick_folder)
        app.pick_btn.grid(row=0, column=1, padx=(12, 0), sticky="e")
        separator(body).pack(fill="x")

        # This is intentionally available before Start.  The controller may
        # supply a frozen/configured summary, but the safe visible default does
        # not claim that a work profile is connected.
        self.review_setup = tk.Frame(body, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
        self.review_setup.pack(fill="x", pady=(10, 2))
        review_copy = tk.Frame(self.review_setup, bg=PANEL)
        review_copy.pack(side="left", fill="x", expand=True, padx=13, pady=10)
        label(review_copy, "REVIEW AFTER PROCESSING", dim=True, size=9).pack(anchor="w")
        self.review_summary = tk.Label(review_copy, bg=PANEL, fg=TEXT, anchor="w", justify="left",
                                       wraplength=720, font=("Segoe UI", 9))
        self.review_summary.pack(fill="x", pady=(3, 0))
        review_actions = tk.Frame(self.review_setup, bg=PANEL)
        review_actions.pack(side="right", padx=10, pady=10)
        self.review_config_btn = button(review_actions, "Configure review", self._configure_review)
        self.review_config_btn.pack(side="left", padx=(0, 6))
        self.view_session_btn = button(review_actions, "View AI session", self._view_ai_session)
        self.view_session_btn.pack(side="left")

        self.dashboard_panel = tk.Frame(body, bg=BG)
        self.dashboard_panel.pack(fill="both", expand=True)

        strip = tk.Frame(self.dashboard_panel, bg=BG)
        strip.pack(fill="x", pady=10)
        self.phase_label = label(strip, "Ready to process", size=17, anchor="w")
        self.phase_label.pack(side="left")
        self.state_label = label(strip, "Idle", dim=True, size=9)
        self.state_label.pack(side="left", padx=(16, 0))
        metrics = tk.Frame(strip, bg=BG)
        metrics.pack(side="right")
        self.review_var = tk.StringVar(app, "—")
        self.workers_var = tk.StringVar(app, "0")
        app.cost_var = tk.StringVar(app, "£0.00")
        app.token_var = tk.StringVar(app, "0 tokens")
        for title, var in (("Workers complete", self.workers_var),
                           ("Needs review", self.review_var),
                           ("Estimated run cost", app.cost_var)):
            cell = tk.Frame(metrics, bg=BG, padx=17)
            cell.pack(side="left")
            label(cell, title, dim=True, size=9).pack(anchor="w")
            value = label(cell, size=16, textvariable=var)
            if title == "Needs review":
                value.configure(fg=ATTENTION, cursor="hand2")
                value.bind("<Button-1>", lambda e:app._open_reports())
            value.pack(anchor="w", pady=(2, 0))
        progress_track = tk.Frame(self.dashboard_panel, bg=LINE, height=5)
        progress_track.pack(fill="x", pady=(0, 10))
        progress_track.pack_propagate(False)
        app.progress = ttk.Progressbar(progress_track, mode="determinate", maximum=1,
                                       style="Obsidian.Horizontal.TProgressbar")
        app.progress.pack(fill="both", expand=True)
        meta = tk.Frame(self.dashboard_panel, bg=BG)
        meta.pack(fill="x")
        self.progress_label = label(meta, "Choose a care-home folder to begin", dim=True, size=9,
                                    anchor="w", justify="left", wraplength=680)
        self.progress_label.pack(side="left")
        self.eta_label = label(meta, "", dim=True, size=9, anchor="e")
        self.eta_label.pack(side="right")
        app.status_lbl = label(self.dashboard_panel, "Idle.", dim=True, anchor="w", justify="left", wraplength=1100)
        app.status_lbl.pack(fill="x", pady=(6, 0))

        content = tk.Frame(self.dashboard_panel, bg=BG)
        content.pack(fill="both", expand=True, pady=(12, 0))
        self.main_content = tk.Frame(content, bg=BG)
        self.main_content.pack(side="left", fill="both", expand=True)
        self.preview_panel = tk.Frame(content, bg=PANEL, width=290)
        self.preview_panel.pack_propagate(False)
        label(self.preview_panel, "Document preview", dim=True, size=9).pack(anchor="w", padx=13, pady=10)
        app.preview_canvas = tk.Label(self.preview_panel, bg=PANEL, fg=MUTED, text="No document preview")
        app.preview_canvas.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        app._preview_ref = None
        separator(self.main_content).pack(fill="x")
        current = tk.Frame(self.main_content, bg=BG)
        current.pack(fill="x", pady=10)
        self.document_heading = label(current, "CURRENT DOCUMENT", dim=True, size=9)
        self.document_heading.pack(anchor="w")
        file_row = tk.Frame(current, bg=BG)
        file_row.pack(fill="x", pady=(6, 0))
        file_row.columnconfigure(0, weight=1)
        app.preview_name = label(file_row, "No document selected", anchor="w", justify="left", wraplength=780)
        app.preview_name.grid(row=0, column=0, sticky="ew")
        self.preview_btn = button(file_row, "Preview", self.toggle_preview)
        self.preview_btn.grid(row=0, column=1, padx=(10, 0), sticky="e")
        self.wait_label = label(current, "The current check appears here during a run.", dim=True, size=9, anchor="w", wraplength=950, justify="left")
        self.wait_label.pack(fill="x", pady=(5, 0))
        separator(self.main_content).pack(fill="x")
        activity_head = tk.Frame(self.main_content, bg=BG)
        activity_head.pack(fill="x", pady=(6, 4))
        label(activity_head, "Recent activity").pack(side="left")
        button(activity_head, "Details & full log", self.open_details).pack(side="right")
        self.activity = ttk.Treeview(self.main_content, columns=("time", "event"),
                                    show="headings", height=5, selectmode="browse")
        self.activity.heading("time", text="Time", anchor="w")
        self.activity.heading("event", text="Event", anchor="w")
        self.activity.column("time", width=75, minwidth=70, stretch=False)
        self.activity.column("event", width=650, minwidth=150, stretch=True)
        self.activity.pack(fill="both", expand=True)
        self.activity.bind("<Double-1>", lambda e:self.open_details())

        footer_separator = separator(app)
        footer_separator.pack(fill="x")
        footer = tk.Frame(app, bg=BG)
        footer.pack(fill="x", padx=24, pady=8)
        self.controls = tk.Frame(footer, bg=BG)
        self.controls.pack(side="left")
        app.start_btn = button(self.controls, "Start processing", app._start, primary=True, state="disabled")
        app.start_btn.pack(side="left", padx=(0, 8))
        app.batch_btn = button(self.controls, "Check batch status", app._batch_check_status, state="disabled")
        app.batch_btn.pack(side="left", padx=(0, 8))
        app.stop_btn = button(self.controls, "Stop", app._stop, state="disabled")
        app.stop_btn.pack(side="left")
        # These compact controls retain visible fills and a usable focus ring at
        # the 1000px minimum while keeping all post-run entry points available.
        footer_button = {"font": ("Segoe UI", 9), "padx": 8, "pady": 6}
        self.review_btn = button(footer, "AI Document Review", self._configure_review, **footer_button)
        self.learning_btn = button(footer, "Improve Stage 2", lambda:app._open_ai_workflow("code-learning"), **footer_button)
        self.learning_btn.pack(side="right", padx=(7, 0))
        self.review_btn.pack(side="right", padx=(12, 0))
        self.report_btn = button(footer, "View Audit Report", app._open_reports, **footer_button)
        self.report_btn.pack(side="right", padx=(12, 0))
        # The same viewer is intentionally offered once, beside its review
        # configuration.  Retain the old attribute as a compatibility alias for
        # callers that previously addressed the footer control directly.
        self.session_btn = self.view_session_btn
        self.footer_note = label(app, "Processing uses the API key in Settings. AI reviews use the account you choose.", dim=True, size=8, anchor="w")
        self.footer_note.pack(fill="x", padx=26, pady=(0, 8))
        # Allocate fixed footer controls before the flexible activity area. Tk's
        # packer otherwise clips these controls when the window is shorter than
        # the body's requested height, even at the documented minimum size.
        self.footer_note.pack_configure(side="bottom", before=body)
        footer.pack_configure(side="bottom", before=body)
        footer_separator.pack_configure(side="bottom", before=body)
        self._build_details()
        self._body = body
        self._sync_banner()
        self._refresh_review_summary()
        self.set_preview_visible(self._preview_on)
        self._sync_dashboard_visibility()
        self.apply_palette(self._requested_palette)
        app.bind("<Configure>", self._resize, add="+")

    def _configure_review(self):
        """Open pre-run review configuration without assuming a provider login."""
        callback = getattr(self.app, "_configure_ai_review", None)
        if callable(callback):
            callback()
        else:
            self.app._open_ai_workflow("audit-review")

    def _view_ai_session(self):
        """Return to an existing AI session when the controller supports it."""
        callback = getattr(self.app, "_view_ai_session", None)
        if callable(callback):
            callback()
        else:
            # Existing releases have no session viewer.  Keep this honest and
            # offer the review setup rather than creating a second run.
            self.status_changed("No AI session viewer is available yet. Configure AI Document Review; this does not start a review.")
            self._configure_review()

    def _review_summary_text(self):
        summary = getattr(self.app, "_ai_review_summary", None)
        if callable(summary):
            try:
                summary = summary()
            except Exception:
                # A partially upgraded controller must not stop the dashboard
                # opening.  Keep setup visible and let its own dialog explain
                # any unavailable account/model integration.
                summary = None
        if summary is None:
            # The controller can expose a dynamic snapshot hook.  Until it
            # does, a local Settings dictionary is enough to describe the next
            # run without publishing an account identity in source.
            summary = self.app.cfg.get("ai_review_summary") or self.app.cfg.get("ai_review")
        if isinstance(summary, dict):
            model = summary.get("model", "Sol")
            effort = summary.get("effort", "High")
            account = summary.get("account", summary.get("account_email", "Current Codex account"))
            corrections = "Apply corrections on" if summary.get("apply_corrections", True) else "Propose corrections only"
            return f"After processing: Accuracy audit → {model} / {effort} document review · {account} · {corrections}."
        if str(summary or "").strip():
            return str(summary).strip()
        return ("After processing: Accuracy audit → Sol / High document review · "
                "Current Codex account · Apply corrections on.")

    def _refresh_review_summary(self):
        self.review_summary.configure(text=self._review_summary_text())

    def _should_show_dashboard(self):
        # A zero-filled dashboard is not useful setup information.  Any real
        # phase, terminal result, recovery/pending state, or active worker must
        # remain visible so hiding presentation never hides recoverable work.
        if self.is_busy() or self._terminal_status:
            return True
        return self.progress.phase != "idle" or self.progress.state != "idle"

    def _sync_dashboard_visibility(self):
        show = self._should_show_dashboard()
        if show and not self.dashboard_panel.winfo_manager():
            self.dashboard_panel.pack(fill="both", expand=True)
        elif not show and self.dashboard_panel.winfo_manager():
            self.dashboard_panel.pack_forget()

    def apply_palette(self, palette=None):
        """Apply A/B/C immediately; this never mutates a run configuration."""
        old, new = self.palette, resolve_palette(palette or self.app.cfg.get("ui_palette", "C"))
        self.palette = new
        _set_compat_palette(new)
        replacements = {
            old.bg: new.bg, old.panel: new.panel, old.raised: new.raised,
            old.line: new.line, old.text: new.text, old.muted: new.muted,
            old.primary: new.primary, old.primary_text: new.primary_text,
            old.attention: new.attention,
        }
        def repaint(widget):
            try:
                if isinstance(widget, tk.Button) and hasattr(widget, "_stage2_primary"):
                    _paint_button(widget, new)
                else:
                    for option in ("bg", "fg", "activebackground", "activeforeground",
                                   "highlightbackground", "highlightcolor", "insertbackground"):
                        try:
                            value = str(widget.cget(option))
                            if value in replacements:
                                widget.configure(**{option: replacements[value]})
                        except tk.TclError:
                            pass
                for child in widget.winfo_children():
                    repaint(child)
            except tk.TclError:
                return
        repaint(self.app)
        self._build_styles()
        return new.key

    set_palette = apply_palette

    def _build_details(self):
        app = self.app
        win = tk.Toplevel(app)
        win.withdraw()
        win.title("Stage 2 — Run details")
        win.configure(bg=BG)
        win.geometry("980x650")
        win.protocol("WM_DELETE_WINDOW", win.withdraw)
        self.details_win = win
        tabs = ttk.Notebook(win)
        tabs.pack(fill="both", expand=True, padx=14, pady=14)
        logs, counts, folder = [tk.Frame(tabs, bg=BG) for _ in range(3)]
        tabs.add(logs, text="Full activity log")
        tabs.add(counts, text="Detailed counters")
        tabs.add(folder, text="Folder & utilities")
        app.log_text = tk.Text(logs, bg=BG, fg=TEXT, insertbackground=TEXT,
                              relief="flat", wrap="word", state="disabled", font=("Consolas", 10))
        scroll = ttk.Scrollbar(logs, command=app.log_text.yview)
        scroll.pack(side="right", fill="y")
        app.log_text.pack(fill="both", expand=True)
        app.log_text.configure(yscrollcommand=scroll.set)
        app.info_vars = {}
        for i, (title, key) in enumerate(FIELDS):
            r, c = divmod(i, 4)
            cell = tk.Frame(counts, bg=BG)
            cell.grid(row=r, column=c, sticky="nw", padx=15, pady=9)
            var = tk.StringVar(app, "0")
            app.info_vars[key] = var
            label(cell, title, dim=True, size=9).pack(anchor="w")
            label(cell, size=15, textvariable=var).pack(anchor="w")
        label(counts, dim=True, textvariable=app.token_var).grid(row=7, column=0, columnspan=4, sticky="w", padx=15)
        app.folder_lbl = label(folder, "No folder selected.", anchor="w", justify="left", wraplength=870)
        app.folder_lbl.pack(fill="x", padx=16, pady=20)
        app.flatten_btn = button(folder, "Flatten folders only", app._flatten_only)
        app.flatten_btn.pack(anchor="w", padx=16)
        label(folder, "Flattening is a separate utility, not a required step before processing.", dim=True, size=9).pack(anchor="w", padx=16, pady=12)
        button(folder, "Developer console", app._toggle_console).pack(anchor="w", padx=16)

    def open_details(self):
        self.details_win.deiconify()
        self.ns["style_titlebar_black"](self.details_win)
        self.details_win.lift()

    def _resize(self, event):
        if event.widget is not self.app:
            return
        available = max(250, self.app.winfo_width() - (365 if self._preview_on else 90))
        self.app.preview_name.configure(wraplength=max(200, available - 155))
        self.wait_label.configure(wraplength=available)
        self.progress_label.configure(wraplength=max(250, self.app.winfo_width()-320))
        self.app.status_lbl.configure(wraplength=max(250, self.app.winfo_width()-60))
        self.app.banner.configure(wraplength=max(250, self.app.winfo_width()-60))
        self._fit_home_name()

    def _sync_banner(self):
        """An empty environment warning must not consume a full status row."""
        banner = self.app.banner
        if str(banner.cget("text")).strip():
            if not banner.winfo_manager():
                banner.pack(fill="x", padx=24, pady=(6, 0), before=self._body)
        elif banner.winfo_manager():
            banner.pack_forget()

    def _fit_home_name(self):
        available = max(180, self.app.winfo_width() - self.app.pick_btn.winfo_reqwidth() - 80)
        font = tkfont.Font(font=self.home_name.cget("font"))
        name = self._home_full_name
        if font.measure(name) > available:
            low, high = 0, len(name)
            while low < high:
                middle = (low + high + 1) // 2
                if font.measure(name[:middle].rstrip() + "…") <= available:
                    low = middle
                else:
                    high = middle - 1
            name = name[:low].rstrip() + "…"
        self.home_name.configure(text=name)

    def set_preview_visible(self, show):
        self._preview_on = bool(show)
        if show:
            self.preview_panel.pack(side="right", fill="y", padx=(18, 0), before=self.main_content)
            self.preview_btn.configure(text="Hide preview")
            if self._audit_path:
                self.request_preview(self._audit_path)
        else:
            self.preview_panel.pack_forget()
            self.preview_btn.configure(text="Preview")
        self._resize(type("Resize", (), {"widget":self.app})())

    def toggle_preview(self):
        self.set_preview_visible(not self._preview_on)
        self.app.cfg["show_document_preview"] = self._preview_on
        self.ns["save_config"](self.app.cfg)

    def request_preview(self, path):
        path = str(path or "")
        if not self._preview_on or not path or path == self._preview_path:
            return
        self._preview_path = path
        self._preview_generation += 1
        generation = self._preview_generation
        self.app.preview_canvas.configure(image="", text="Loading current document…")
        self.app._preview_ref = None
        def render():
            try:
                import fitz
                with fitz.open(path) as doc:
                    if not len(doc):
                        raise ValueError("Empty document")
                    pix = doc[0].get_pixmap(matrix=fitz.Matrix(.65, .65), alpha=False)
                    data = pix.tobytes("png")
                self._preview_queue.put((generation, data))
            except Exception:
                self._preview_queue.put((generation, None))
        threading.Thread(target=render, name="stage2-preview", daemon=True).start()

    def activity_event(self, event):
        if event.get("kind") == "run_progress":
            self._terminal_status = ""
            self.structured_progress = True
            self.progress.observe(event)
            titles = {"preparing":"Preparing worker folders", "batch":"Preparing batch requests",
                      "scanning":"Scanning documents · local orientation preflight",
                      "followup_plan":"Planning stronger-model follow-up",
                      "followup_upload":"Preparing stronger-model follow-up",
                      "recovery":"Recovering primary submission", "processing":"Document processing"}
            if self.progress.phase == "batch" and self.progress.state in ("submitting", "submitted"):
                titles["batch"] = "Submitting batch requests"
            elif self.progress.phase == "batch" and self.progress.state == "attention":
                titles["batch"] = "Batch preparation needs attention"
            if self.progress.phase == "followup_upload" and self.progress.state in ("submitting", "submitted"):
                titles["followup_upload"] = "Submitting stronger-model follow-up"
            self.phase_label.configure(text=titles.get(self.progress.phase, "Document processing"))
            self.app.progress.configure(mode="determinate", maximum=max(self.progress.total, 1), value=self.progress.completed)
            self.state_label.configure(text="Working")
            self.refresh_progress()
            self._sync_dashboard_visibility()
            return
        if event.get("phase") == "audit":
            self.progress.observe(event)
            self.phase_label.configure(text="Post-run accuracy audit")
            self.app.progress.configure(mode="determinate", maximum=max(self.progress.total, 1), value=self.progress.completed)
            self.review_var.set(str(self.progress.needs_review))
            self._audit_path = self.progress.path
            if self._audit_path:
                self.app.preview_name.configure(text=Path(self._audit_path).name)
                self.request_preview(self._audit_path)
            state = self.progress.state
            self.document_heading.configure(text="LAST DOCUMENT" if state == "complete" else "INTERRUPTED DOCUMENT" if state in ("stopped", "failed") else "CURRENT DOCUMENT")
            if state == "complete":
                self.app._latest_audit_report = self.progress.report
                self.add_activity("Accuracy audit completed; review report ready")
            elif state == "started":
                self.add_activity(f"Accuracy audit started: {self.progress.total:,} eligible documents")
            elif state == "adjudicating":
                self.add_activity("Possible mismatch: requesting a second opinion")
            elif state in ("stopped", "failed", "skipped"):
                self.add_activity("Accuracy audit " + state + "; review incomplete")
            self.refresh_progress()
            self._sync_dashboard_visibility()
        elif event.get("kind") == "phase_started":
            self.add_activity(str(event.get("label") or event.get("phase", "Processing")).capitalize() + " started")
            self._sync_dashboard_visibility()

    def refresh_progress(self):
        state = self.progress
        self.progress_label.configure(text=state.caption())
        if state.phase != "audit":
            if state.phase in ("preparing", "scanning", "batch", "followup_plan", "followup_upload"):
                labels = {"running":"Preparing", "scanning":"Scanning", "orienting":"Local orientation check",
                          "rendering":"Rendering", "submitting":"Sending to provider",
                          "submitted":"Accepted · processing pending", "complete":"Phase complete",
                          "stopped":"Stopped", "failed":"Needs attention",
                          "limited":"Configured scan limit reached",
                          "attention":"Needs attention · nothing sent"}
                self.state_label.configure(text=labels.get(state.state, "Working"))
                if state.state in ("running", "scanning", "orienting", "rendering", "submitting"):
                    self.wait_label.configure(text=f"Current operation · {concise_duration(state.wait_seconds())} since last progress")
                elif state.state == "submitted":
                    self.wait_label.configure(text="Accepted requests still need provider processing, result application and any enabled audit.")
                elif state.state == "attention":
                    self.wait_label.configure(text="No provider batch/classification result was submitted or applied. Preparation changes, if any, remain; repair or restore documents, then retry.")
                elif state.state == "stopped":
                    self.wait_label.configure(text="Local work has stopped; a request already sent may still finish. Check saved batch status before retrying.")
                elif state.state == "failed":
                    self.wait_label.configure(text="Local work ended with an issue. Submitted provider work may still exist; check saved status and activity before retrying.")
                elif state.phase == "preparing":
                    self.wait_label.configure(text="Preparation passes finished, including skips. Check the activity log for conversion failures.")
                elif state.phase == "followup_plan":
                    self.wait_label.configure(text="Sizing pass finished. Payloads are rebuilt for submission; no follow-up requests were sent by the sizing pass.")
                elif state.phase == "followup_upload":
                    self.wait_label.configure(text="No new follow-up requests could be submitted. Primary results are retained for the normal apply path.")
                elif state.phase == "batch":
                    self.wait_label.configure(text="Batch preparation finished. Check saved batch status before starting another batch.")
                else:
                    self.wait_label.configure(text="Local scanning finished for this run's selected scope; this is not the post-run accuracy audit.")
                self.eta_label.configure(text="Time remaining unavailable" if state.wait_seconds() else "")
            return
        labels = {"started":"Starting", "checking":"Checking", "adjudicating":"Second opinion",
                  "document_done":"Checking", "writing_report":"Saving report", "complete":"Complete",
                  "stopped":"Stopped", "failed":"Needs attention", "skipped":"Not run"}
        self.state_label.configure(text=labels.get(state.state, "Running"))
        wait = state.wait_seconds()
        if state.state in ("checking", "adjudicating"):
            step = "Second opinion pending" if state.state == "adjudicating" else "Reading and checking document"
            self.wait_label.configure(text=f"{step} · {concise_duration(wait)} elapsed")
        elif state.state == "complete":
            self.wait_label.configure(text="Audit report saved. Suggestions need evidence review before changes.")
        elif state.state in ("failed", "stopped", "skipped"):
            self.wait_label.configure(text="Audit incomplete. Restarting the audit checks documents again.")
        else:
            self.wait_label.configure(text="Saving checked results…" if state.state == "writing_report" else "Preparing the next document…")
        eta = state.eta_seconds()
        self.eta_label.configure(text=("About " + concise_duration(eta) + " remaining · estimate") if eta is not None else ("Time remaining unavailable" if state.state not in ("complete", "failed", "stopped", "skipped") else ""))

    def worker_progress(self, done, total):
        if self.progress.phase == "audit":
            return
        self.progress.observe({"phase":"processing", "state":"running", "completed":done, "total":total})
        self.phase_label.configure(text="Document processing")
        self.state_label.configure(text="Running")
        self.refresh_progress()
        self._sync_dashboard_visibility()

    def reset(self):
        self.progress = PhaseProgress()
        self.structured_progress = False
        self._terminal_status = ""
        self._audit_path, self._preview_path = "", ""
        self._preview_generation += 1
        self.review_var.set("—")
        self.eta_label.configure(text="")
        self.document_heading.configure(text="CURRENT DOCUMENT")
        self.app.preview_name.configure(text="Preparing documents…")
        self.app.preview_canvas.configure(image="", text="No current document")
        self.app._preview_ref = None
        self._sync_dashboard_visibility()

    def add_activity(self, msg):
        msg = str(msg).strip()
        if not msg:
            return
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        one_line = " ".join(msg.split())
        self.events.append((stamp, one_line))
        self.activity.insert("", 0, values=(stamp, one_line[:240]))
        children = self.activity.get_children()
        for item in children[6:]:
            self.activity.delete(item)

    def status_changed(self, msg):
        if self.progress.phase != "audit":
            self.wait_label.configure(text=msg)
            if getattr(self, "_terminal_status", ""):
                if self.is_busy():
                    # A new preflight scan starts before its structured event;
                    # do not let an old terminal outcome hide it.
                    self._terminal_status = ""
                else:
                    self.add_activity(msg)
                    return
            if self.structured_progress and self.progress.phase in ("preparing", "scanning", "batch", "followup_plan", "followup_upload"):
                # Structured phase facts outrank broad keyword guesses. The
                # detailed message is still retained in the activity log.
                self.refresh_progress()
            else:
                if "scan" in msg.lower():
                    self.phase_label.configure(text="Checking input folders")
                elif "batch" in msg.lower():
                    self.phase_label.configure(text="Batch processing")
                self.state_label.configure(text="Working" if self.is_busy() else "Idle")
        self.add_activity(msg)
        self._sync_dashboard_visibility()

    def is_busy(self):
        app = self.app
        return bool((app.worker_thread and app.worker_thread.is_alive())
                    or getattr(app, "_scanning", False) or getattr(app, "_recovery_busy", False))

    def update_stats(self, stats):
        self.workers_var.set(str(stats.get("workers", 0)))
        if self.progress.phase == "audit":
            # Structured audit events are the freshest unresolved/error count.
            # Terminal stats can lag live events; a periodic stats refresh must
            # not overwrite the ongoing audit's review count with stale values.
            self.review_var.set(str(self.progress.needs_review))
        elif "audit_flagged" in stats:
            self.review_var.set(str(stats["audit_flagged"]))

    def finish(self, stats, status):
        if self.progress.phase == "audit":
            if self.progress.state not in ("complete", "skipped", "failed", "stopped"):
                self.progress.observe({"phase":"audit", "state":"stopped" if status else "failed"})
            self.refresh_progress()
        else:
            kind = str(status or "").partition(":")[0]
            if (self.structured_progress and self.progress.phase in
                    ("preparing", "scanning", "batch", "followup_plan", "followup_upload")):
                # _done_main() finishes first, then a queued set_status() can
                # arrive.  Seal the structured state so that callback cannot
                # redraw the final UI as the previous active operation.
                if self.progress.state == "attention":
                    # The zero-renderable path is deliberately retryable, not
                    # a provider wait or a generic failure.
                    self.refresh_progress()
                    self._sync_dashboard_visibility()
                    return
                if kind in ("batch_submitted", "batch_followup_submitted", "batch_pending"):
                    if self.progress.state != "submitted":
                        self.progress.observe({"phase": self.progress.phase,
                                               "state": "submitted"})
                elif kind in ("", "batch_applied", "batch_audit_complete", "batch_none", "batch_empty",
                              "batch_none_pending", "batch_nothing_to_submit"):
                    self.progress.observe({"phase": self.progress.phase,
                                           "state": "complete"})
                elif kind == "stopped":
                    self.progress.observe({"phase": self.progress.phase,
                                           "state": "stopped"})
                else:
                    # Errors, limits and ambiguous provider outcomes require
                    # attention, but must never inherit an active wait timer.
                    self.progress.observe({"phase": self.progress.phase,
                                           "state": "failed"})
                self.refresh_progress()
                self._sync_dashboard_visibility()
                return
            # _done_batch queues set_status after every terminal finish.
            # Preserve the final label while idle; release it for new work.
            self._terminal_status = kind or "complete"
            if kind in ("batch_submitted", "batch_followup_submitted", "batch_pending"):
                self.state_label.configure(text="Waiting for provider")
                self.phase_label.configure(text="Batch processing")
            elif kind in ("", "batch_applied", "batch_audit_complete"):
                self.state_label.configure(text="Complete")
            elif kind == "stopped":
                self.state_label.configure(text="Stopped")
            elif kind in ("batch_none", "batch_empty", "batch_none_pending"):
                self.state_label.configure(text="No pending batch")
                self._terminal_status = kind
            elif kind == "batch_nothing_to_submit":
                self.state_label.configure(text="Nothing to submit")
                self._terminal_status = kind
            else:
                self.state_label.configure(text="Needs attention")
        self._sync_dashboard_visibility()

    def refresh_context(self):
        app = self.app
        folder = str(app.care_home_dir or "")
        context = (folder, str(app.move_dest or ""), bool(app.cfg.get("move_mode")))
        if context != self._last_context:
            self._last_context = context
            name = Path(folder).name if folder else "No care home selected"
            for suffix in (" [Files]", " [Processed]"):
                name = name.removesuffix(suffix)
            self._home_full_name = name
            self._fit_home_name()
            if folder:
                count = len(self.ns["worker_dirs_in"](Path(folder)))
                movement = "Files → Processed" if app.cfg.get("move_mode") and app.move_dest else "Process in place"
                self.home_detail.configure(text=f"{count} worker folders · {movement}")
            else:
                self.home_detail.configure(text="Choose the folder containing worker folders.")

    def _tick(self):
        if self._closed:
            return
        self.refresh_context()
        self._sync_banner()
        self._refresh_review_summary()
        self._sync_dashboard_visibility()
        if self.progress.phase == "audit" or (self.is_busy() and self.progress.phase in ("preparing", "scanning", "batch", "followup_plan", "followup_upload")):
            self.refresh_progress()
        busy = self.is_busy()
        self.review_btn.configure(state="disabled" if busy else "normal")
        self.learning_btn.configure(state="disabled" if busy else "normal")
        # The application changes button states directly; repaint only native
        # Stage 2 buttons here so a disabled primary action has the same dark,
        # readable treatment as the surrounding controls.
        for widget in (self.app.start_btn, self.app.batch_btn, self.app.stop_btn,
                       self.review_btn, self.learning_btn, self.report_btn,
                       self.review_config_btn, self.view_session_btn):
            _paint_button(widget, self.palette)
        try:
            while True:
                generation, data = self._preview_queue.get_nowait()
                if generation != self._preview_generation:
                    continue
                if data:
                    from PIL import Image, ImageTk
                    image = Image.open(io.BytesIO(data))
                    image.thumbnail((265, max(180, self.preview_panel.winfo_height()-60)))
                    self.app._preview_ref = ImageTk.PhotoImage(image)
                    self.app.preview_canvas.configure(image=self.app._preview_ref, text="")
                else:
                    self.app.preview_canvas.configure(image="", text="Preview unavailable")
        except queue.Empty:
            pass
        self._tick_id = self.app.after(500, self._tick)
