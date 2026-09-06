"""Compact Obsidian dialogs for AI handoffs, report selection and notifications.

No AI work begins merely by opening a dialog. Account verification, request
preparation, and CLI launch use background workers with a main-thread Tk queue.
"""
from __future__ import annotations

import os
from pathlib import Path
import queue
import re
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import stage2_ai_workflows as workflows
import stage2_notifications as notifications


BG = "#08090b"
PANEL = "#101215"
RAISED = "#191c20"
BORDER = "#2a2e33"
FG = "#edf0f3"
MUTED = "#a1a8b1"
SILVER = "#e3e8ed"


def is_processing_busy(app):
    worker = getattr(app, "worker_thread", None)
    return bool((worker and worker.is_alive()) or getattr(app, "_scanning", False)
                or getattr(app, "_recovery_busy", False))


def latest_audit_report(app, namespace):
    """Use the exact run report when present; never infer that it is complete."""
    def care_key(value):
        # Files and Processed are the same care home. Do not use broad keyword
        # matching here: an audit from another branch is not a safe default.
        name = re.sub(r"\s*\[(?:files|processed)\]\s*$", "", str(value), flags=re.I)
        return " ".join(name.casefold().split())
    try:
        rows = namespace.get("load_processing_reports", lambda: [])()
    except Exception:
        rows = []
    care_home = getattr(app, "care_home_dir", None)
    selected = getattr(app, "_latest_audit_report", None)
    if selected and Path(selected).is_file():
        # The exact report belongs to the most recently completed app audit, but
        # users can subsequently choose a different care-home folder. If its
        # registered identity disagrees, do not carry it across care homes.
        registration = next((row for row in rows if row.get("path")
                             and Path(row["path"]).resolve() == Path(selected).resolve()), None)
        report_home = registration.get("care_home", "") if registration else Path(selected).parent.name
        if not care_home or care_key(report_home) == care_key(Path(care_home).name):
            return Path(selected)
    if care_home:
        rows = [row for row in rows if care_key(row.get("care_home", "")) == care_key(Path(care_home).name)]
    for row in rows:
        value = row.get("path")
        if value and Path(value).is_file() and Path(value).suffix.lower() in (".csv", ".xlsx", ".xls"):
            if "orientation" not in Path(value).stem.casefold():
                return Path(value)
    return None


def _open_path(parent, path):
    target = Path(path)
    if not target.exists():
        messagebox.showinfo("Not available yet", "This file or folder has not been created yet. Complete the relevant review first.\n\n" + str(target), parent=parent)
        return False
    try:
        os.startfile(str(target))
        return True
    except Exception:
        messagebox.showerror("Could not open", "Windows could not open the selected file or folder. Check its location and default application.", parent=parent)
        return False


def _button(parent, text, command, primary=False):
    button = tk.Button(parent, text=text, command=command, bg=SILVER if primary else RAISED,
                       fg=BG if primary else FG, activebackground="#ffffff" if primary else BORDER,
                       activeforeground=BG if primary else FG, disabledforeground="#747b84",
                       relief="flat", bd=0, padx=14, pady=8, cursor="hand2", font=("Segoe UI", 10),
                       highlightthickness=1, highlightbackground=BORDER)
    return button


def _label(parent, text, small=False, bold=False):
    return tk.Label(parent, text=text, bg=BG, fg=MUTED if small else FG,
                    font=("Segoe UI", 9 if small else 10, "bold" if bold else "normal"),
                    anchor="w", justify="left", wraplength=690)


def _entry(parent, variable, show=None):
    return tk.Entry(parent, textvariable=variable, bg=PANEL, fg=FG, insertbackground=FG,
                    relief="flat", highlightthickness=1, highlightbackground=BORDER,
                    highlightcolor=SILVER, font=("Segoe UI", 10), show=show or "")


def _check(parent, text, variable, command=None):
    return tk.Checkbutton(parent, text=text, variable=variable, command=command,
                          bg=BG, fg=FG, selectcolor=PANEL, activebackground=BG,
                          activeforeground=FG, font=("Segoe UI", 10), anchor="w",
                          justify="left", wraplength=650, highlightthickness=0)


class _Dialog(tk.Toplevel):
    def __init__(self, app, namespace, title, subtitle, width=780, height=780):
        previous_grab = app.grab_current()
        modal_parent = previous_grab.winfo_toplevel() if previous_grab else app
        super().__init__(modal_parent)
        self.app, self.namespace = app, namespace
        self._previous_grab = previous_grab
        self._results = queue.Queue()
        self._operation_busy = False
        self._closed = False
        self._caption_after_ids = []
        self.title(title)
        self.configure(bg=BG)
        self.transient(modal_parent)
        if previous_grab is not None:
            # Settings is modal. Its notification dialog must own the grab while
            # open, then restore Settings' grab; a sibling Toplevel is unusable.
            self.grab_set()
        width = min(width, max(600, self.winfo_screenwidth() - 80))
        height = min(height, max(460, self.winfo_screenheight() - 100))
        self.geometry(f"{width}x{height}")
        self.minsize(min(640, width), min(480, height))
        try:
            icon = app.iconbitmap()
            if icon:
                self.iconbitmap(icon)
        except tk.TclError:
            pass
        self._caption_styler = namespace.get("style_titlebar_black")
        if self._caption_styler:
            self._caption_after_ids.append(self.after_idle(self._style_caption))
            # The decorated HWND is not final before the native top-level maps.
            # Reapply after Map (also after restore) rather than relying on an
            # early idle callback that can style a soon-replaced Tk wrapper.
            self.bind("<Map>", self._mapped, add="+")
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Destroy>", self._destroyed, add="+")
        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", padx=22, pady=(20, 12))
        _label(head, title, bold=True).pack(anchor="w")
        _label(head, subtitle, small=True).pack(fill="x", pady=(5, 0))
        frame = tk.Frame(self, bg=BG)
        frame.pack(fill="both", expand=True, padx=(22, 12))
        self._scroll_frame = frame
        canvas = tk.Canvas(frame, bg=BG, bd=0, highlightthickness=0)
        self._body_canvas = canvas
        scroll = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        scroll.pack(side="right", fill="y")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        self.body = tk.Frame(canvas, bg=BG)
        window = canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        def resized(event):
            canvas.itemconfigure(window, width=event.width)
            pending = list(self.body.winfo_children())
            while pending:
                widget = pending.pop()
                if isinstance(widget, (tk.Label, tk.Checkbutton)):
                    widget.configure(wraplength=max(280, event.width - 18))
                pending.extend(widget.winfo_children())
            if hasattr(self, "status_label"):
                self.status_label.configure(wraplength=max(280, event.width - 18))
        canvas.bind("<Configure>", resized)
        self.bind("<MouseWheel>", lambda event: canvas.yview_scroll(-int(event.delta / 120), "units"))
        self.status = tk.StringVar(value="")
        self.status_label = tk.Label(self, textvariable=self.status, bg=BG, fg=MUTED,
                                    font=("Segoe UI", 9), wraplength=700, justify="left", anchor="w")
        self.status_label.pack(fill="x", padx=22, pady=(10, 6))
        self.footer = tk.Frame(self, bg=BG)
        self.footer.pack(fill="x", padx=22, pady=(0, 18))
        self._footer_pack_options = {}
        self._footer_wrapped = None
        self.footer.bind("<Configure>", self._layout_footer)
        # The fixed actions and status must be allocated before the flexible
        # scroll viewport. Otherwise the Canvas's requested height clips Close /
        # Save / Launch into a thin strip at the default Reports dialog size.
        self.footer.pack_configure(side="bottom", before=frame)
        self.status_label.pack_configure(side="bottom", before=frame)
        self._poll_id = self.after(100, self._poll)

    def _style_caption(self):
        if not self._closed and self._caption_styler and self.winfo_exists():
            self._caption_styler(self)

    def _layout_footer(self, _event=None):
        buttons = [widget for widget in self.footer.winfo_children() if isinstance(widget, tk.Button)]
        if not buttons:
            return
        for widget in buttons:
            if widget not in self._footer_pack_options and widget.winfo_manager() == "pack":
                self._footer_pack_options[widget] = widget.pack_info()
        if len(self._footer_pack_options) != len(buttons):
            return
        # Preserve the approved single ribbon row whenever it fits. At larger
        # font/DPI scales or narrower windows, use two rows rather than clipping
        # an action; all four buttons must remain reachable without scrolling.
        needed = sum(widget.winfo_reqwidth() + 16 for widget in buttons)
        wrapped = needed > self.footer.winfo_width()
        if wrapped == self._footer_wrapped:
            return
        self._footer_wrapped = wrapped
        for widget in buttons:
            widget.pack_forget()
            widget.grid_forget()
        if wrapped:
            left = [widget for widget in buttons if self._footer_pack_options[widget].get("side") == "left"]
            right = [widget for widget in buttons if self._footer_pack_options[widget].get("side") != "left"]
            ordered = left + list(reversed(right))
            self.footer.columnconfigure(0, weight=1)
            self.footer.columnconfigure(1, weight=1)
            for index, widget in enumerate(ordered):
                widget.grid(row=index // 2, column=index % 2,
                            sticky="w" if widget in left else "e",
                            padx=(0, 8) if index % 2 == 0 else (8, 0), pady=(0, 6))
        else:
            for widget in buttons:
                widget.pack(**self._footer_pack_options[widget])

    def _mapped(self, event):
        if event.widget is not self or self._closed:
            return
        for identifier in self._caption_after_ids:
            try:
                self.after_cancel(identifier)
            except tk.TclError:
                pass
        self._caption_after_ids = [self.after(50, self._style_caption),
                                   self.after(250, self._style_caption)]

    def _background(self, work, done, status):
        if self._operation_busy:
            return False
        self._operation_busy = True
        self.status.set(status)
        def execute():
            try:
                result = work()
                self._results.put((done, result, None))
            except (workflows.WorkflowError, RuntimeError, ValueError) as exc:
                # WorkflowError and notification settings errors are deliberately
                # user-facing and redact provider transport data at their source.
                self._results.put((done, None, str(exc) if isinstance(exc, workflows.WorkflowError) else
                                   "The operation could not finish. Check the configured paths, account, and credential store."))
            except Exception:
                self._results.put((done, None, "The operation could not finish. No completion is being claimed; check the configured account and paths, then retry."))
        threading.Thread(target=execute, name="stage2-dialog-operation", daemon=True).start()
        return True

    def _poll(self):
        if self._closed:
            return
        while True:
            try:
                done, value, error = self._results.get_nowait()
            except queue.Empty:
                break
            self._operation_busy = False
            if error:
                self.status.set(error)
                self._on_error()
            else:
                done(value)
        self._poll_extra()
        self._poll_id = self.after(100, self._poll)

    def _poll_extra(self):
        pass

    def _on_error(self):
        pass

    def _close(self):
        if self._operation_busy:
            self.status.set("An operation is still in progress. Wait for its result before closing this dialog.")
            return
        self._closed = True
        self.after_cancel(self._poll_id)
        restore = self._previous_grab
        try:
            if self.grab_current() is self:
                self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
        if restore is not None:
            try:
                if restore.winfo_exists():
                    restore.grab_set()
                    restore.focus_set()
            except tk.TclError:
                pass

    def _destroyed(self, event):
        if event.widget is self:
            self._closed = True
            for identifier in self._caption_after_ids:
                try:
                    self.after_cancel(identifier)
                except tk.TclError:
                    pass
            try:
                self.after_cancel(self._poll_id)
            except (tk.TclError, AttributeError):
                pass
            service = getattr(self, "test_service", None)
            if service:
                service.close()

    def _field(self, label, variable, *, browse=None, show=None):
        row = tk.Frame(self.body, bg=BG)
        row.pack(fill="x", pady=(0, 10))
        _label(row, label, small=True).pack(anchor="w", pady=(0, 4))
        control = tk.Frame(row, bg=BG)
        control.pack(fill="x")
        if browse:
            _button(control, "Browse", browse).pack(side="right", padx=(8, 0))
        entry = _entry(control, variable, show=show)
        entry.pack(side="left", fill="x", expand=True, ipady=7)
        return entry


class AIWorkflowDialog(_Dialog):
    def __init__(self, app, namespace, role):
        if role not in workflows.ROLES:
            raise ValueError("Unknown AI workflow role.")
        self.role = role
        title = "Review the filename audit" if role == "audit-review" else "Learn from review corrections"
        subtitle = ("Step 1 · Check the actual documents, make evidence-backed corrections when allowed, and record every decision."
                    if role == "audit-review" else
                    "Step 2 · Critically review the accumulated correction evidence, test general fixes, and improve Stage 2 only when justified.")
        super().__init__(app, namespace, title, subtitle)
        saved = app.cfg.get("ai_workflows", {})
        if not isinstance(saved, dict):
            saved = {}
        self._saved = saved
        self.prepared = None
        self._revision = 0
        self.accounts = []
        self.filtered_accounts = []
        self.model_keys = [key for key, choice in workflows.MODEL_CHOICES.items() if choice["role"] == role]
        selected = saved.get(role + "_model", "luna" if role == "audit-review" else "fable")
        if selected not in self.model_keys:
            selected = self.model_keys[0]
        self.model_var = tk.StringVar(value=workflows.MODEL_CHOICES[selected]["label"])
        self.account_var = tk.StringVar()
        self.expected_email = tk.StringVar(value=str(saved.get(role + "_expected_email", "")))
        audit = latest_audit_report(app, namespace)
        care_dir = getattr(app, "care_home_dir", None)
        documents = getattr(app, "move_dest", None) or care_dir
        self.audit_var = tk.StringVar(value=str(audit or ""))
        self.documents_var = tk.StringVar(value=str(documents or saved.get("document_root", "")))
        self.processing_var = tk.StringVar(value=str(care_dir or saved.get("processing_root", "")))
        self.care_var = tk.StringVar(value=Path(care_dir).name if care_dir else str(saved.get("care_home", "")))
        local_repo = Path.home() / "repos" / "lifted-stage2-processing"
        self.source_var = tk.StringVar(value=str(saved.get("source_root") or (local_repo if local_repo.is_dir() else "")))
        self.ledger_var = tk.StringVar(value=str(saved.get("ledger_path") or workflows.default_ledger_path()))
        self.workspace_var = tk.StringVar(value=str(saved.get("workspace_root") or workflows.default_workspace_root()))
        self.allow_var = tk.BooleanVar(value=False)
        self.complete_var = tk.BooleanVar(value=bool(audit and getattr(app, "_latest_audit_completed", False)
            and str(audit) == str(getattr(app, "_latest_audit_report", ""))))
        self._selector("Model and effort", self.model_var, [workflows.MODEL_CHOICES[key]["label"] for key in self.model_keys], self._model_changed)
        self.account_box = self._selector("Account · identity will be verified before launch", self.account_var, [], self._invalidate)
        self._field("Expected account email (optional cross-check)", self.expected_email)
        if role == "audit-review":
            self._field("Completed filename audit · CSV or Excel", self.audit_var, browse=lambda: self._browse_report(self.audit_var))
            self._field("Care-home name", self.care_var)
            self._field("Audited document folder · usually the Processed folder", self.documents_var,
                        browse=lambda: self._browse_folder(self.documents_var))
            self._field("Original processing folder · the Files folder to lock during corrections", self.processing_var,
                        browse=lambda: self._browse_folder(self.processing_var))
        self._field("Master AI review ledger · shared across all reviewers", self.ledger_var,
                    browse=lambda: self._browse_report(self.ledger_var, save=role == "audit-review"))
        self._field("Stage 2 source checkout · official naming and ranking implementation", self.source_var,
                    browse=lambda: self._browse_folder(self.source_var))
        self._field("Persistent shared-context root · separate memory folder for each review role", self.workspace_var,
                    browse=lambda: self._browse_folder(self.workspace_var))
        _label(self.body, "Both models for this step receive the same rules, exact request and file-based memory. Their native chat histories remain separate.", small=True).pack(fill="x", pady=(0, 9))
        if role == "audit-review":
            _check(self.body, "The selected post-run accuracy audit has finished. File existence alone is not confirmation.", self.complete_var).pack(fill="x", pady=3)
            _check(self.body, "Allow evidence-backed filename corrections. Re-rank every peer in ranked families; use the next free number only for unranked families.", self.allow_var).pack(fill="x", pady=3)
            _label(self.body, "Every flagged/error row is in scope, including lower-confidence rows. The reviewer cannot edit application code.", small=True).pack(fill="x", pady=(3, 10))
        else:
            _check(self.body, "Allow verified source-code changes with regression tests and quality checks. This does not authorize renaming documents, publishing, or installation.", self.allow_var).pack(fill="x", pady=3)
        self.prepare_button = _button(self.footer, "Verify & prepare", self._prepare, primary=True)
        self.prepare_button.pack(side="right", padx=(8, 0))
        self.launch_button = _button(self.footer, "Open selected AI", self._launch)
        self.launch_button.pack(side="right", padx=(8, 0))
        self.launch_button.configure(state="disabled")
        self.copy_button = _button(self.footer, "Copy command", self._copy)
        self.copy_button.pack(side="left")
        self.copy_button.configure(state="disabled")
        _button(self.footer, "Open context", self._open_context).pack(side="left", padx=(8, 0))
        for variable in (self.expected_email, self.documents_var, self.processing_var, self.care_var,
                         self.source_var, self.ledger_var, self.workspace_var, self.allow_var, self.complete_var):
            variable.trace_add("write", lambda *_: self._invalidate())
        self.audit_var.trace_add("write", self._audit_changed)
        self.status.set("Choose a model and account, check the exact report and scope, then verify. No AI task is running yet.")
        self._load_accounts()

    def _selector(self, label, variable, values, callback):
        row = tk.Frame(self.body, bg=BG)
        row.pack(fill="x", pady=(0, 10))
        _label(row, label, small=True).pack(anchor="w", pady=(0, 4))
        style = ttk.Style(self)
        style.configure("Stage2Workflow.TCombobox", fieldbackground=PANEL, background=RAISED,
                        foreground=FG, arrowcolor=FG, bordercolor=BORDER, padding=7)
        style.map("Stage2Workflow.TCombobox", fieldbackground=[("readonly", PANEL)],
                  foreground=[("readonly", FG)], selectbackground=[("readonly", PANEL)], selectforeground=[("readonly", FG)])
        self.option_add("*TCombobox*Listbox.background", PANEL)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        box = ttk.Combobox(row, textvariable=variable, values=values, state="readonly",
                           style="Stage2Workflow.TCombobox", font=("Segoe UI", 10))
        box.pack(fill="x")
        box.bind("<<ComboboxSelected>>", lambda _event: callback())
        return box

    def _model_key(self):
        return next(key for key in self.model_keys if workflows.MODEL_CHOICES[key]["label"] == self.model_var.get())

    def _load_accounts(self):
        # Metadata-only discovery is still off the UI thread in case a registered
        # profile location is temporarily slow or unavailable.
        self.prepare_button.configure(state="disabled")
        self._background(lambda: workflows.discover_accounts(codex_homes=self._saved.get("codex_homes", [])),
                         self._accounts_loaded, "Reading registered account profiles…")

    def _accounts_loaded(self, accounts):
        self.accounts = accounts
        self._filter_accounts()
        self.prepare_button.configure(state="normal")
        self.status.set("Profiles loaded. Identity and model availability will be checked during preparation; no AI task has started.")

    def _filter_accounts(self):
        provider = workflows.MODEL_CHOICES[self._model_key()]["provider"]
        self.filtered_accounts = [account for account in self.accounts if account.provider == provider]
        labels = [account.label + " [" + account.id[-8:] + "]" for account in self.filtered_accounts]
        self.account_box.configure(values=labels)
        saved_id = self._saved.get(self.role + "_account")
        selected = next((i for i, account in enumerate(self.filtered_accounts) if account.id == saved_id), 0)
        self.account_var.set(labels[selected] if labels else "No registered account for this provider")

    def _model_changed(self):
        self._filter_accounts()
        self._invalidate()

    def _invalidate(self):
        self._revision += 1
        self.prepared = None
        self.launch_button.configure(state="disabled")
        self.copy_button.configure(state="disabled")

    def _audit_changed(self, *_):
        self.complete_var.set(False)
        self._invalidate()

    def _browse_report(self, variable, save=False):
        options = {"parent": self, "title": "Select the exact report", "filetypes": [("Spreadsheet reports", "*.csv *.xlsx *.xls"), ("All files", "*.*")]}
        result = (filedialog.asksaveasfilename(defaultextension=".xlsx", **options) if save else filedialog.askopenfilename(**options))
        if result:
            variable.set(result)

    def _browse_folder(self, variable):
        result = filedialog.askdirectory(parent=self, title="Choose the exact folder", initialdir=variable.get() or None)
        if result:
            variable.set(result)

    def _collect(self):
        index = self.account_box.current()
        if index < 0 or index >= len(self.filtered_accounts):
            raise workflows.WorkflowError("Choose a registered account for this model. Add/sign in to an account through AI Account Manager first.")
        if self.role == "audit-review" and not self.complete_var.get():
            raise workflows.WorkflowError("Confirm that this exact post-run accuracy audit finished before preparing its review.")
        if self.role == "audit-review" and self.allow_var.get() and (
                not self.processing_var.get().strip() or not Path(self.processing_var.get()).is_dir()):
            raise workflows.WorkflowError("Choose the original Files folder so document corrections can be locked against another processing run.")
        assets = self.namespace.get("bundled_resource", lambda *parts: Path(__file__).resolve().parent.parent.joinpath(*parts))("docs", "ai-review")
        return dict(role=self.role, account=self.filtered_accounts[index], model_key=self._model_key(),
                    audit_report=self.audit_var.get() or None if self.role == "audit-review" else None,
                    document_root=self.documents_var.get() or None if self.role == "audit-review" else None,
                    processing_root=self.processing_var.get() or None if self.role == "audit-review" else None,
                    care_home=self.care_var.get(), source_root=self.source_var.get() or None,
                    assets_root=assets, workspace_root=self.workspace_var.get() or None,
                    ledger_path=self.ledger_var.get() or None,
                    misnaming_path=Path(self.namespace.get("APP_DIR", workflows.default_misnaming_path().parent)) / "Misnaming Record.xlsx",
                    allow_document_changes=self.role == "audit-review" and self.allow_var.get(),
                    allow_code_changes=self.role == "code-learning" and self.allow_var.get(),
                    completed_audit=self.role == "audit-review" and self.complete_var.get(),
                    expected_email=self.expected_email.get().strip() or None, review_all_flags=True)

    def _prepare(self):
        if is_processing_busy(self.app):
            self.status.set("Stage 2 is processing or scanning. Wait for it to finish before preparing an AI review.")
            return
        try:
            values = self._collect()
        except workflows.WorkflowError as exc:
            self.status.set(str(exc))
            return
        revision = self._revision
        self.prepare_button.configure(state="disabled")
        self.launch_button.configure(state="disabled")
        self.copy_button.configure(state="disabled")
        self._background(lambda: workflows.prepare_workflow(**values),
                         lambda result: self._prepared(result, revision, values),
                         "Verifying the selected account and model, then preparing the exact shared-context request. No AI review is running yet…")

    def _prepared(self, prepared, revision, values):
        self.prepare_button.configure(state="normal")
        if revision != self._revision:
            self.status.set("The form changed during preparation. The old request is retained, but cannot launch from this form. Verify & prepare again.")
            return
        self.prepared = prepared
        self.launch_button.configure(state="normal")
        self.copy_button.configure(state="normal")
        saved = dict(self.app.cfg.get("ai_workflows") or {})
        saved.update({self.role + "_model": values["model_key"], self.role + "_account": values["account"].id,
                      self.role + "_expected_email": prepared.preflight.get("email", ""),
                      "source_root": self.source_var.get(), "ledger_path": self.ledger_var.get(),
                      "workspace_root": self.workspace_var.get(), "care_home": self.care_var.get(),
                      "document_root": self.documents_var.get(), "processing_root": self.processing_var.get()})
        self.app.cfg["ai_workflows"] = saved
        persisted = self.namespace.get("save_config", lambda _config: True)(self.app.cfg)
        verified = prepared.preflight
        self.status.set(f"Verified: {verified.get('email', 'account')} · {verified.get('model', '')} · {verified.get('effort', '')} effort. "
                        "Request prepared, not launched. Open selected AI to continue." + (" Preferences could not be saved." if not persisted else ""))

    def _launch(self):
        if not self.prepared:
            return
        if is_processing_busy(self.app):
            self.status.set("Stage 2 is processing or scanning. Launch is blocked until it finishes.")
            return
        prepared = self.prepared
        self.prepare_button.configure(state="disabled")
        self.launch_button.configure(state="disabled")
        self._background(lambda: workflows.launch_workflow(prepared), lambda proc: self._launched(prepared, proc),
                         "Rechecking account identity and opening the account-isolated AI terminal…")

    def _launched(self, prepared, _process):
        self.prepare_button.configure(state="normal")
        self.launch_button.configure(state="disabled")
        self.status.set(f"Opened {prepared.preflight.get('model', 'AI')} for {prepared.preflight.get('email', 'the selected account')} with its command prefilled. "
                        "This confirms launch only—not review completion. Its request and shared memory are retained.")
        service = getattr(self.app, "notification_service", None)
        if service:
            service.emit("review_started", run_id=str(prepared.request_dir),
                         phase="audit_review" if self.role == "audit-review" else "improvement_review")

    def _on_error(self):
        self.prepare_button.configure(state="normal")
        if self.prepared:
            self.launch_button.configure(state="normal")

    def _copy(self):
        if self.prepared:
            self.clipboard_clear()
            self.clipboard_append(self.prepared.command)
            self.status.set("Command copied. Use it only in the selected account's terminal opened in this request's shared-context folder.")

    def _open_context(self):
        path = self.prepared.workspace if self.prepared else Path(self.workspace_var.get()) / self.role
        _open_path(self, path)


class NotificationSettingsDialog(_Dialog):
    def __init__(self, app, namespace):
        super().__init__(app, namespace, "Notifications", "Receive concise progress and outcome updates. Worker names and document contents are never sent.", height=760)
        saved = notifications.normalize_settings(app.cfg.get("notifications"))
        self.test_service = None
        self._test_statuses = []
        self.channel_vars = {}
        for channel in ("discord", "telegram"):
            values = saved[channel]
            variables = {key: tk.StringVar(value=values[key]) for key in ("destination", "credential_file", "token_env", "credential_ref")}
            variables["enabled"] = tk.BooleanVar(value=values["enabled"])
            variables["new_token"] = tk.StringVar()
            self.channel_vars[channel] = variables
            _check(self.body, f"Send updates to {channel.title()}", variables["enabled"]).pack(fill="x", pady=(6, 6))
            self._field("Discord channel ID" if channel == "discord" else "Telegram chat ID or @channel", variables["destination"])
            self._field("Existing local .env credential file (optional)", variables["credential_file"],
                        browse=lambda name=channel: self._browse_credential(name))
            row = tk.Frame(self.body, bg=BG)
            row.pack(fill="x", pady=(0, 10))
            _label(row, "Token environment-variable name", small=True).pack(anchor="w", pady=(0, 4))
            _entry(row, variables["token_env"]).pack(fill="x", ipady=7)
            self._field("New bot token · optional, stored only in the OS credential store", variables["new_token"], show="•")
            _label(self.body, "Leave the new token blank to retain the existing credential. A saved OS credential takes precedence over environment and .env references.", small=True).pack(fill="x", pady=(0, 10))
        self.progress_var = tk.BooleanVar(value=saved["progress_enabled"])
        self.wait_var = tk.StringVar(value=str(saved["wait_minutes"]))
        _check(self.body, "Include aggregate progress at 25%, 50%, 75% and 100%", self.progress_var).pack(fill="x", pady=5)
        self._field("First long-wait update after this many minutes (1–120; then at most every 30 minutes)", self.wait_var)
        _label(self.body, "Phase changes, submitted batches, attention needed and completion remain enabled. Delivery is best-effort; failures never stop processing. Telegram needs an explicit recipient—none is guessed.", small=True).pack(fill="x", pady=(0, 8))
        self.save_button = _button(self.footer, "Save settings", self._save, primary=True)
        self.save_button.pack(side="right")
        self.test_button = _button(self.footer, "Test enabled channels", self._test)
        self.test_button.pack(side="left")
        self.status.set("Changes are not saved until Save settings. Test reports delivery asynchronously, not just queue acceptance.")

    def _browse_credential(self, channel):
        value = filedialog.askopenfilename(parent=self, title="Select an existing local bot credential file", filetypes=[("Environment files", "*.env .env"), ("All files", "*.*")])
        if value:
            self.channel_vars[channel]["credential_file"].set(value)

    def _values(self):
        try:
            minutes = int(self.wait_var.get())
        except (ValueError, TypeError):
            raise workflows.WorkflowError("Enter a whole number of minutes between 1 and 120.")
        if not 1 <= minutes <= 120:
            raise workflows.WorkflowError("Enter a wait interval between 1 and 120 minutes.")
        raw = {"progress_enabled": self.progress_var.get(), "wait_minutes": minutes}
        tokens = {}
        for channel, variables in self.channel_vars.items():
            raw[channel] = {key: variable.get() for key, variable in variables.items() if key != "new_token"}
            if raw[channel]["enabled"] and not notifications._valid_destination(channel, raw[channel]["destination"]):
                raise workflows.WorkflowError(f"Enter a valid {channel.title()} destination before enabling it.")
            tokens[channel] = variables["new_token"].get().strip()
        return notifications.normalize_settings(raw), tokens

    def _store_tokens(self, values, tokens):
        for channel, token in tokens.items():
            if token:
                notifications.set_bot_token(channel, token, values[channel]["credential_ref"])
        return values

    def _save(self):
        try:
            values, tokens = self._values()
        except workflows.WorkflowError as exc:
            self.status.set(str(exc))
            return
        self.save_button.configure(state="disabled")
        self.test_button.configure(state="disabled")
        self._background(lambda: self._store_tokens(values, tokens), self._saved_values, "Saving any new bot token in the OS credential store…")

    def _saved_values(self, values):
        previous = self.app.cfg.get("notifications")
        self.app.cfg["notifications"] = values
        if not self.namespace.get("save_config", lambda _config: True)(self.app.cfg):
            if previous is None:
                self.app.cfg.pop("notifications", None)
            else:
                self.app.cfg["notifications"] = previous
            self.status.set("Settings could not be saved. Any newly stored bot token remains in the OS credential store; notification settings were not changed.")
        else:
            service = getattr(self.app, "notification_service", None)
            if service:
                service.configure(values)
            for variables in self.channel_vars.values():
                variables["new_token"].set("")
            self.status.set("Notification settings saved. Enabled channels will receive future lifecycle updates.")
        self._on_error()

    def _test(self):
        try:
            values, tokens = self._values()
        except workflows.WorkflowError as exc:
            self.status.set(str(exc))
            return
        if not any(values[channel]["enabled"] for channel in ("discord", "telegram")):
            self.status.set("Enable at least one channel and enter its destination to send a test.")
            return
        if any(tokens.values()):
            self.status.set("Save the new bot token first, then Test. A test never silently changes stored credentials.")
            return
        if self.test_service:
            self.test_service.close()
        self.test_service = notifications.NotificationService(values)
        self._test_statuses = []
        self.test_button.configure(state="disabled")
        queued = self.test_service.emit("test", run_id="settings-test")
        self.status.set("Test queued. Waiting for the actual delivery result…" if queued else "No test was queued. Check enabled channels.")

    def _poll_extra(self):
        if self.test_service:
            statuses = self.test_service.drain_statuses()
            if statuses:
                self._test_statuses.extend(statuses)
                self.status.set("\n".join(self._test_statuses[-4:]))
            if self.test_service.is_idle():
                self.test_button.configure(state="normal")

    def _on_error(self):
        self.save_button.configure(state="normal")
        self.test_button.configure(state="normal")

    def _close(self):
        if not self._operation_busy and self.test_service:
            self.test_service.close()
        super()._close()


class ReportsMenuDialog(_Dialog):
    def __init__(self, app, namespace):
        super().__init__(app, namespace, "Which report would you like?", "The automatic audit and the AI review record serve different purposes.", width=690, height=490)
        _label(self.body, "1 · Post-run filename audit", bold=True).pack(fill="x", pady=(4, 5))
        _label(self.body, "The report generated by Stage 2's accuracy check. Browse the exact care home and run before opening it.", small=True).pack(fill="x")
        _button(self.body, "Browse audit reports", self._audits, primary=True).pack(anchor="w", pady=(9, 20))
        _label(self.body, "2 · AI review / correction ledger", bold=True).pack(fill="x", pady=(0, 5))
        _label(self.body, "The master spreadsheet that Opus or Luna updates with checked cases, corrected names, evidence and unresolved decisions. Fable or Sol uses this to assess software improvements.", small=True).pack(fill="x")
        saved = app.cfg.get("ai_workflows") or {}
        self.ledger = Path(saved.get("ledger_path") or workflows.default_ledger_path())
        _button(self.body, "Open AI review ledger", lambda: _open_path(self, self.ledger)).pack(anchor="w", pady=(9, 12))
        _label(self.body, str(self.ledger), small=True).pack(fill="x")
        legacy = Path(namespace.get("APP_DIR", workflows.default_misnaming_path().parent)) / "Misnaming Record.xlsx"
        if legacy.is_file():
            _button(self.body, "Open legacy Misnaming Record", lambda: _open_path(self, legacy)).pack(anchor="w", pady=(12, 4))
        _button(self.footer, "Close", self._close).pack(side="right")

    def _audits(self):
        dialog = self.namespace.get("ReportsDialog")
        if dialog:
            dialog(self.app)
            self._close()
        else:
            self.status.set("The audit report browser is unavailable in this installation.")


def open_ai_workflow(app, namespace, role):
    if is_processing_busy(app):
        messagebox.showinfo("Stage 2 is busy", "Wait for document processing, recovery or scanning to finish before launching an AI review. The current run has not been interrupted.", parent=app)
        return None
    return _open_single_dialog(app, "ai-" + role, lambda: AIWorkflowDialog(app, namespace, role))


def open_notification_settings(app, namespace):
    return _open_single_dialog(app, "notifications", lambda: NotificationSettingsDialog(app, namespace))


def open_reports_menu(app, namespace):
    return _open_single_dialog(app, "reports", lambda: ReportsMenuDialog(app, namespace))


def _open_single_dialog(app, key, factory):
    """Repeated ribbon clicks focus the existing form instead of duplicating it."""
    dialogs = getattr(app, "_stage2_workflow_dialogs", None)
    if dialogs is None:
        dialogs = {}
        app._stage2_workflow_dialogs = dialogs
    existing = dialogs.get(key)
    if existing is not None:
        try:
            if existing.winfo_exists():
                grab_getter = getattr(app, "grab_current", None)
                active_grab = grab_getter() if grab_getter else None
                if active_grab is not None and active_grab is not existing:
                    existing._previous_grab = active_grab
                    existing.transient(active_grab.winfo_toplevel())
                    existing.grab_set()
                existing.deiconify()
                existing.lift()
                existing.focus_set()
                return existing
        except tk.TclError:
            pass
    dialog = factory()
    dialogs[key] = dialog
    return dialog
