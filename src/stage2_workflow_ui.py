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
import stage2_live_output as live_output
import stage2_notifications as notifications

try:  # Shared palette helper; presentation only, no import cycle (theme imports nothing of ours).
    from stage2_theme import resolve_palette as _resolve_palette
except ImportError:  # pragma: no cover - older bundles without the theme module
    _resolve_palette = None

_PALETTE = _resolve_palette("C") if _resolve_palette else None
BG = _PALETTE.bg if _PALETTE else "#08090b"
PANEL = _PALETTE.panel if _PALETTE else "#101215"
RAISED = _PALETTE.raised if _PALETTE else "#191c20"
BORDER = _PALETTE.line if _PALETTE else "#2a2e33"
FG = _PALETTE.text if _PALETTE else "#edf0f3"
MUTED = _PALETTE.muted if _PALETTE else "#a1a8b1"
SILVER = _PALETTE.primary if _PALETTE else "#e3e8ed"
PRIMARY_TEXT = _PALETTE.primary_text if _PALETTE else "#08090b"


def is_processing_busy(app):
    worker = getattr(app, "worker_thread", None)
    return bool((worker and worker.is_alive()) or getattr(app, "_scanning", False)
                or getattr(app, "_recovery_busy", False) or getattr(app, "_review_busy", False))


def _use_app_palette(app):
    """New dialogs inherit the current saved palette instead of import-time C."""
    if _resolve_palette is None:
        return
    palette = _resolve_palette(getattr(app, "cfg", {}).get("ui_palette", "C"))
    globals().update(BG=palette.bg, PANEL=palette.panel, RAISED=palette.raised,
                     BORDER=palette.line, FG=palette.text, MUTED=palette.muted,
                     SILVER=palette.primary, PRIMARY_TEXT=palette.primary_text)


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
                       fg=PRIMARY_TEXT if primary else FG, activebackground="#ffffff" if primary else BORDER,
                       activeforeground=PRIMARY_TEXT if primary else FG, disabledforeground="#747b84",
                       relief="flat", bd=0, padx=14, pady=8, cursor="hand2", font=("Segoe UI", 10),
                       highlightthickness=1, highlightbackground=BORDER,
                       highlightcolor=SILVER, takefocus=True)
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
        _use_app_palette(app)
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
        head.bind("<Configure>", lambda event: [widget.configure(wraplength=max(240, event.width))
                  for widget in head.winfo_children() if isinstance(widget, tk.Label)])
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

    def _selector(self, label, variable, values, callback):
        row = tk.Frame(self.body, bg=BG)
        row.pack(fill="x", pady=(0, 10))
        _label(row, label, small=True).pack(anchor="w", pady=(0, 4))
        style = ttk.Style(self)
        style.configure("Stage2Workflow.TCombobox", fieldbackground=PANEL, background=RAISED,
                        foreground=FG, arrowcolor=FG, bordercolor=BORDER, padding=7)
        style.map("Stage2Workflow.TCombobox", fieldbackground=[("readonly", PANEL), ("disabled", BG)],
                  foreground=[("readonly", FG), ("disabled", MUTED)], selectbackground=[("readonly", PANEL)], selectforeground=[("readonly", FG)])
        self.option_add("*TCombobox*Listbox.background", PANEL)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        box = ttk.Combobox(row, textvariable=variable, values=values, state="readonly",
                           style="Stage2Workflow.TCombobox", font=("Segoe UI", 10))
        box.pack(fill="x")
        box.bind("<<ComboboxSelected>>", lambda _event: callback())
        return box

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


class ModelSelector:
    """Shared Model → Account → Effort form with a read-only inferred provider.

    Used identically by AI Document Review, Improve Stage 2 and the pre-run
    automatic review setup. Changing the model updates the compatible accounts
    and efforts, preserves a still-valid choice, and visibly requires a new
    account choice when the old one belongs to another provider. Nothing here
    verifies identity; that happens in the preflight before launch.
    """

    NO_ACCOUNT = "No compatible {provider} profile registered · sign in through AI Account Manager"
    CHOOSE_ACCOUNT = "Choose a {provider} account · {count} available"

    def __init__(self, dialog, role, *, model_key=None, effort=None, account_id=None, on_change=None):
        if role not in workflows.ROLES:
            raise ValueError("Unknown AI workflow role.")
        self.dialog, self.role, self.on_change = dialog, role, on_change
        self.accounts, self.filtered_accounts = [], []
        self.model_keys = list(workflows.model_keys_for_role(role))
        if model_key not in self.model_keys:
            model_key = workflows.DEFAULT_MODEL[role]
        self._model_labels = {}
        values = []
        for index, key in enumerate(self.model_keys):
            entry = workflows.MODEL_CATALOG[key]
            label = f"{entry['family']} · {entry['provider_label']}" + (" · recommended" if index == 0 else "")
            self._model_labels[label] = key
            values.append(label)
        self._effort_labels = {}
        self._preferred_account = str(account_id or "")
        self._pending_effort = str(effort or "").strip().casefold() or None
        self._selected_account_id = None
        self.model_var = tk.StringVar(value=next(label for label, key in self._model_labels.items() if key == model_key))
        self.account_var = tk.StringVar(value="")
        self.effort_var = tk.StringVar(value="")
        self.model_box = dialog._selector("Model · ranked by practical suitability for this step; the provider is inferred from the model",
                                          self.model_var, values, self._model_changed)
        self.provider_label = _label(dialog.body, "", small=True)
        self.provider_label.pack(fill="x", pady=(0, 10))
        self.account_box = dialog._selector("Account · identity is verified before launch, never inferred from the profile name",
                                            self.account_var, [], self._account_changed)
        self.effort_box = dialog._selector("Effort · recommended level first, then alternatives; advanced levels are marked",
                                           self.effort_var, [], self._effort_changed)
        self._sync(initial=True)

    # ---- state -------------------------------------------------------------
    def model_key(self):
        return self._model_labels.get(self.model_var.get(), self.model_keys[0])

    def choice(self):
        return workflows.model_choice(self.model_key(), self.effort(), self.role)

    def effort(self):
        return self._effort_labels.get(self.effort_var.get()) or workflows.model_choice(self.model_key(), None, self.role)["recommended_effort"]

    def account(self):
        index = self.account_box.current()
        if 0 <= index < len(self.filtered_accounts) and self.account_var.get() == self._account_label(self.filtered_accounts[index]):
            return self.filtered_accounts[index]
        return None

    def provider(self):
        return workflows.MODEL_CATALOG[self.model_key()]["provider"]

    def set_accounts(self, accounts):
        self.accounts = list(accounts)
        self._sync(initial=True)

    def set_enabled(self, enabled):
        state = "readonly" if enabled else "disabled"
        for box in (self.model_box, self.account_box, self.effort_box):
            box.configure(state=state)

    # ---- internals ---------------------------------------------------------
    @staticmethod
    def _account_label(account):
        return account.label + " [" + account.id[-8:] + "]"

    def _sync(self, initial=False):
        choice = workflows.model_choice(self.model_key(), None, self.role)
        self.provider_label.configure(text=f"Provider (inferred): {choice['provider_label']} · model ID {choice['id']} · {choice['note']}")
        previous_id = self._selected_account_id if not initial else (self._selected_account_id or self._preferred_account)
        self.filtered_accounts = [account for account in self.accounts if account.provider == choice["provider"]]
        labels = [self._account_label(account) for account in self.filtered_accounts]
        self.account_box.configure(values=labels)
        keep = next((account for account in self.filtered_accounts if account.id == previous_id), None)
        if keep is None and initial and not self._preferred_account and self.filtered_accounts:
            keep = self.filtered_accounts[0]
        if keep is not None:
            self.account_var.set(self._account_label(keep))
            self._selected_account_id = keep.id
        elif labels:
            # The old account belongs to another provider: require a visible new choice.
            self.account_var.set(self.CHOOSE_ACCOUNT.format(provider=choice["provider_label"], count=len(labels)))
            self._selected_account_id = None
        else:
            self.account_var.set(self.NO_ACCOUNT.format(provider=choice["provider_label"]))
            self._selected_account_id = None
        efforts = choice["efforts"]
        self._effort_labels = {}
        values = []
        for level in efforts:
            label = workflows.EFFORT_LABELS.get(level, level)
            if level == choice["recommended_effort"]:
                label += " · recommended"
            elif level in choice["advanced_efforts"]:
                label += " · advanced"
            self._effort_labels[label] = level
            values.append(label)
        self.effort_box.configure(values=values)
        current = self._effort_labels.get(self.effort_var.get()) or self._pending_effort
        self._pending_effort = None
        if current not in efforts:
            current = choice["recommended_effort"]
        self.effort_var.set(next(label for label, level in self._effort_labels.items() if level == current))

    def _model_changed(self):
        self._sync()
        if self.on_change:
            self.on_change("model")

    def _account_changed(self):
        account = self.account()
        self._selected_account_id = account.id if account else None
        if self.on_change:
            self.on_change("account")

    def _effort_changed(self):
        if self.on_change:
            self.on_change("effort")


class AIWorkflowDialog(_Dialog):
    def __init__(self, app, namespace, role):
        if role not in workflows.ROLES:
            raise ValueError("Unknown AI workflow role.")
        self.role = role
        title = workflows.ROLE_TITLES[role]
        subtitle = ("Open the actual document pages for each audit flag, judge whether the flag is justified, record Keep / Rename / Defer, "
                    "and apply supported filename corrections only when permitted. The accuracy audit itself never renames files."
                    if role == "audit-review" else
                    "Critically review the accumulated correction evidence, reproduce defects, test general fixes, and change Stage 2 code only when justified and authorized. "
                    "This never renames worker documents.")
        super().__init__(app, namespace, title, subtitle)
        saved = app.cfg.get("ai_workflows", {})
        if not isinstance(saved, dict):
            saved = {}
        self._saved = saved
        self.prepared = None
        self._revision = 0
        self.expected_email = tk.StringVar(value=str(saved.get(role + "_expected_email") or workflows.DEFAULT_EXPECTED_EMAIL))
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
        # Document corrections default ON (approved); code-change authority is
        # never implied and must be ticked explicitly for each learning run.
        self.allow_var = tk.BooleanVar(value=role == "audit-review")
        self.complete_var = tk.BooleanVar(value=bool(audit and getattr(app, "_latest_audit_completed", False)
            and str(audit) == str(getattr(app, "_latest_audit_report", ""))))
        self.selector = ModelSelector(self, role, model_key=saved.get(role + "_model", workflows.DEFAULT_MODEL[role]),
                                      effort=saved.get(role + "_effort") or "high", account_id=saved.get(role + "_account"),
                                      on_change=self._selection_changed)
        self.accounts = self.selector.accounts
        self.filtered_accounts = self.selector.filtered_accounts
        self.model_var, self.account_var, self.effort_var = self.selector.model_var, self.selector.account_var, self.selector.effort_var
        self.account_box = self.selector.account_box
        self._field("Expected account email · the verified identity must match this before launch", self.expected_email)
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
        _label(self.body, "Every model for this step receives the same rules, exact request and file-based memory. Native chat histories stay separate, and this role's history stays separate from the other role.", small=True).pack(fill="x", pady=(0, 9))
        if role == "audit-review":
            _check(self.body, "This exact post-run accuracy audit has finished. File existence alone is not confirmation.", self.complete_var).pack(fill="x", pady=3)
            _check(self.body, "Apply supported filename corrections. Off records proposals only; on permits evidence-backed, backed-up renames. Ranked families are re-ranked together; unranked collisions take the next free number.", self.allow_var).pack(fill="x", pady=3)
            _label(self.body, "Every flagged/error row is in scope, including lower-confidence rows. This permission never authorizes application-code changes.", small=True).pack(fill="x", pady=(3, 10))
        else:
            _check(self.body, "Implement justified software changes, with regression tests and quality checks. This does not authorize renaming documents, publishing, or installation.", self.allow_var).pack(fill="x", pady=3)
        _label(self.body, "Manual path: Open native AI terminal starts the provider's own interactive session with the request prefilled; closing that window ends that session. "
                          "The automatic review after processing instead runs supervised, with a read-only live output window.", small=True).pack(fill="x", pady=(6, 10))
        self.prepare_button = _button(self.footer, "Verify & prepare", self._prepare, primary=True)
        self.prepare_button.pack(side="right", padx=(8, 0))
        self.launch_button = _button(self.footer, "Open native AI terminal", self._launch)
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
        self.status.set("Choose a model, account and effort, check the exact report and scope, then verify. No AI task is running yet.")
        self._load_accounts()

    def _model_key(self):
        return self.selector.model_key()

    def _load_accounts(self):
        # Metadata-only discovery is still off the UI thread in case a registered
        # profile location is temporarily slow or unavailable.
        self.prepare_button.configure(state="disabled")
        self._background(lambda: workflows.discover_accounts(codex_homes=self._saved.get("codex_homes", [])),
                         self._accounts_loaded, "Reading registered account profiles…")

    def _accounts_loaded(self, accounts):
        self.selector.set_accounts(accounts)
        self.accounts = self.selector.accounts
        self.filtered_accounts = self.selector.filtered_accounts
        self.prepare_button.configure(state="normal")
        self.status.set("Profiles loaded. Identity, model and effort availability are checked during preparation; no AI task has started.")

    def _filter_accounts(self):
        self.selector._sync()
        self.filtered_accounts = self.selector.filtered_accounts

    def _model_changed(self):
        self.selector._model_changed()

    def _selection_changed(self, what):
        self.filtered_accounts = self.selector.filtered_accounts
        if what == "account":
            account = self.selector.account()
            if account is not None and account.email:
                self.expected_email.set(account.email)
        self._invalidate()
        choice = self.selector.choice()
        account = self.selector.account()
        if account is None:
            self.status.set(f"{choice['family']} runs on {choice['provider_label']}. Choose a compatible account before verifying.")
        else:
            self.status.set(f"Selected {choice['family']} · {choice['provider_label']} · {choice['effort_label']} effort. Verify & prepare to check identity and availability.")

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
        account = self.selector.account()
        if account is None:
            raise workflows.WorkflowError("Choose a registered account for this model's provider. Add/sign in to an account through AI Account Manager first.")
        if self.role == "audit-review" and not self.complete_var.get():
            raise workflows.WorkflowError("Confirm that this exact post-run accuracy audit finished before preparing its review.")
        if self.role == "audit-review" and self.allow_var.get() and (
                not self.processing_var.get().strip() or not Path(self.processing_var.get()).is_dir()):
            raise workflows.WorkflowError("Choose the original Files folder so document corrections can be locked against another processing run.")
        assets = self.namespace.get("bundled_resource", lambda *parts: Path(__file__).resolve().parent.parent.joinpath(*parts))("docs", "ai-review")
        return dict(role=self.role, account=account, model_key=self.selector.model_key(), effort=self.selector.effort(),
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
                         "Verifying the selected account, model and effort, then preparing the exact shared-context request. No AI review is running yet…")

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
                      self.role + "_effort": values["effort"],
                      self.role + "_expected_email": prepared.preflight.get("email", ""),
                      "source_root": self.source_var.get(), "ledger_path": self.ledger_var.get(),
                      "workspace_root": self.workspace_var.get(), "care_home": self.care_var.get(),
                      "document_root": self.documents_var.get(), "processing_root": self.processing_var.get()})
        self.app.cfg["ai_workflows"] = saved
        persisted = self.namespace.get("save_config", lambda _config: True)(self.app.cfg)
        verified = prepared.preflight
        self.status.set(f"Verified: {verified.get('email', 'account')} · {verified.get('model', '')} · {verified.get('effort', '')} effort. "
                        "Request prepared, not launched. Open native AI terminal to continue." + (" Preferences could not be saved." if not persisted else ""))

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
                         "Rechecking account identity, model and effort, then opening the account-isolated native AI terminal…")

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


class AutoReviewSettingsDialog(_Dialog):
    """Configure the automatic AI Document Review BEFORE any report exists.

    Saving stores defaults under cfg['ai_workflows']['auto_review']; nothing
    verifies or runs unless the optional read-only account check is pressed.
    A run already in progress keeps its captured snapshot; saved changes apply
    to the next run only.
    """

    def __init__(self, app, namespace):
        super().__init__(app, namespace, "Review after processing",
                         "Defaults for the automatic AI Document Review that follows a completed accuracy audit. "
                         "Nothing starts from this dialog; the snapshot is captured at Start of the next run.")
        values = workflows.auto_review_defaults(app.cfg)
        self._values = values
        self.enabled_var = tk.BooleanVar(value=values["enabled"])
        self.expected_email = tk.StringVar(value=values["expected_email"])
        self.allow_var = tk.BooleanVar(value=values["allow_document_changes"])
        self.all_flags_var = tk.BooleanVar(value=values["review_all_flags"])
        self.source_var = tk.StringVar(value=values["source_root"])
        self.workspace_var = tk.StringVar(value=values["workspace_root"] or str(workflows.default_workspace_root()))
        self.ledger_var = tk.StringVar(value=values["ledger_path"] or str(workflows.default_ledger_path()))
        _check(self.body, "Run AI Document Review automatically after the accuracy audit completes", self.enabled_var, command=self._toggled).pack(fill="x", pady=(0, 8))
        audit_on = bool(app.cfg.get("post_run_audit", False))
        self.dependency_label = _label(self.body, "", small=True)
        self.dependency_label.pack(fill="x", pady=(0, 10))
        self._dependency_text(audit_on)
        self.selector = ModelSelector(self, "audit-review", model_key=values["model_key"], effort=values["effort"],
                                      account_id=values["account_id"], on_change=self._selection_changed)
        self._field("Expected account email · the run is blocked, not switched, if the signed-in identity differs", self.expected_email)
        _check(self.body, "Apply supported filename corrections. Off records proposals only; on permits evidence-backed, backed-up renames.", self.allow_var).pack(fill="x", pady=3)
        _check(self.body, "Review every flagged/error row, including lower-confidence rows", self.all_flags_var).pack(fill="x", pady=3)
        _label(self.body, "These permissions never authorize application-code changes. Improve Stage 2 remains separate and manual.", small=True).pack(fill="x", pady=(3, 10))
        self._field("Stage 2 source checkout · required for queue preparation and records", self.source_var,
                    browse=lambda: self._browse_folder(self.source_var))
        self._field("Master AI review ledger", self.ledger_var, browse=lambda: self._browse_ledger())
        self._field("Persistent shared-context root", self.workspace_var, browse=lambda: self._browse_folder(self.workspace_var))
        if is_processing_busy(app):
            _label(self.body, "A run is in progress. It keeps the review setup captured at its Start; anything saved here applies to the next run only.", small=True).pack(fill="x", pady=(0, 8))
        self.save_button = _button(self.footer, "Save defaults", self._save, primary=True)
        self.save_button.pack(side="right", padx=(8, 0))
        self.verify_button = _button(self.footer, "Check account now", self._verify)
        self.verify_button.pack(side="right")
        _button(self.footer, "Close", self._close).pack(side="left")
        self.status.set("Changes are not saved until Save defaults. Checking the account is read-only and optional; identity is re-verified at launch.")
        self.save_button.configure(state="disabled")
        self._background(lambda: workflows.discover_accounts(codex_homes=(app.cfg.get("ai_workflows") or {}).get("codex_homes", [])),
                         self._accounts_loaded, "Reading registered account profiles…")

    def _dependency_text(self, audit_on):
        if self.enabled_var.get():
            self.dependency_label.configure(text=("Requires the automatic accuracy audit. It is currently on." if audit_on else
                                                  "Requires the automatic accuracy audit, which is currently OFF. Saving with review enabled turns the audit on as well."))
        else:
            self.dependency_label.configure(text="Off: the accuracy audit still runs if enabled in Settings; no AI review starts and no documents are renamed.")

    def _toggled(self):
        self._dependency_text(bool(self.app.cfg.get("post_run_audit", False)))
        self.selector.set_enabled(self.enabled_var.get())

    def _accounts_loaded(self, accounts):
        self.selector.set_accounts(accounts)
        self.selector.set_enabled(self.enabled_var.get())
        self.save_button.configure(state="normal")
        account = self.selector.account()
        if account is not None and not account.email:
            self.status.set("Profiles loaded. The current Codex login has no saved email metadata; its identity is verified at launch, not assumed absent.")
        else:
            self.status.set("Profiles loaded. Save defaults to store them; nothing runs from here.")

    def _selection_changed(self, what):
        if what == "account":
            account = self.selector.account()
            if account is not None and account.email:
                self.expected_email.set(account.email)
        choice = self.selector.choice()
        self.status.set(f"{choice['family']} · {choice['provider_label']} · {choice['effort_label']} effort selected. Not saved yet.")

    def _browse_folder(self, variable):
        result = filedialog.askdirectory(parent=self, title="Choose the exact folder", initialdir=variable.get() or None)
        if result:
            variable.set(result)

    def _browse_ledger(self):
        result = filedialog.asksaveasfilename(parent=self, title="Select the master ledger", defaultextension=".xlsx",
                                              filetypes=[("Excel workbook", "*.xlsx"), ("All files", "*.*")])
        if result:
            self.ledger_var.set(result)

    def _collect(self):
        enabled = bool(self.enabled_var.get())
        account = self.selector.account()
        choice = self.selector.choice()
        source = self.source_var.get().strip()
        if enabled:
            if account is None:
                raise workflows.WorkflowError("Choose a registered account for this model's provider before enabling automatic review.")
            if not source or not all((Path(source) / "src" / name).is_file() for name in ("ai_review.py", "Stage2_Processing.pyw")):
                raise workflows.WorkflowError("Choose the current Stage 2 source checkout (with src/ai_review.py and Stage2_Processing.pyw). It is required for queue preparation and records; none is guessed.")
        return {"enabled": enabled, "model_key": choice["key"], "effort": choice["effort"],
                "account_id": account.id if account else "", "expected_email": self.expected_email.get().strip(),
                "allow_document_changes": bool(self.allow_var.get()), "review_all_flags": bool(self.all_flags_var.get()),
                "source_root": source, "workspace_root": self.workspace_var.get().strip(),
                "ledger_path": self.ledger_var.get().strip(), "misnaming_path": self._values.get("misnaming_path", "")}

    def _verify(self):
        account = self.selector.account()
        if account is None:
            self.status.set("Choose an account to check.")
            return
        choice = self.selector.choice()
        expected = self.expected_email.get().strip() or None
        self.verify_button.configure(state="disabled")
        self._background(lambda: workflows.validate_selection(account, choice["key"], expected, effort=choice["effort"], role="audit-review"),
                         self._verified, "Read-only account, model and effort check. No AI task is started…")

    def _verified(self, result):
        self.verify_button.configure(state="normal")
        self.status.set(f"Verified now: {result.get('email')} · {result.get('model')} · {result.get('effort')} effort ({result.get('status')}). "
                        "This is re-checked at launch; save to keep these defaults.")

    def _save(self):
        try:
            values = self._collect()
        except workflows.WorkflowError as exc:
            self.status.set(str(exc))
            return
        saved = dict(self.app.cfg.get("ai_workflows") or {})
        previous = saved.get("auto_review")
        previous_audit = self.app.cfg.get("post_run_audit")
        saved["auto_review"] = values
        self.app.cfg["ai_workflows"] = saved
        note = ""
        if values["enabled"] and not bool(self.app.cfg.get("post_run_audit", False)):
            self.app.cfg["post_run_audit"] = True
            note = " The automatic accuracy audit was turned on because automatic review depends on it."
        persisted = self.namespace.get("save_config", lambda _config: True)(self.app.cfg)
        if not persisted:
            if previous is None:
                saved.pop("auto_review", None)
            else:
                saved["auto_review"] = previous
            if previous_audit is None:
                self.app.cfg.pop("post_run_audit", None)
            else:
                self.app.cfg["post_run_audit"] = previous_audit
            self.status.set("Defaults could not be saved. Nothing changed.")
            return
        refresh = getattr(self.app, "_refresh_ai_review_summary", None)
        if callable(refresh):
            try:
                refresh()
            except Exception:
                pass
        self._dependency_text(bool(self.app.cfg.get("post_run_audit", False)))
        self.status.set(("Saved. " + workflows.auto_review_summary(self.app.cfg) + note +
                         (" The current run keeps its captured setup; these apply to the next run." if is_processing_busy(self.app) else "")))

    def _on_error(self):
        self.verify_button.configure(state="normal")
        self.save_button.configure(state="normal")


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
        super().__init__(app, namespace, "Which report would you like?", "The accuracy audit report and the AI review/correction ledger serve different purposes. Opening either never starts a check or a review.", width=740, height=670)
        _label(self.body, "1 · Audit report", bold=True).pack(fill="x", pady=(4, 5))
        _label(self.body, "Written by Stage 2's automatic accuracy audit after processing: it flags possible misnaming but corrects nothing. Browse the exact care home and run before opening it.", small=True).pack(fill="x")
        _button(self.body, "View Audit Report", self._audits, primary=True).pack(anchor="w", pady=(9, 20))
        _label(self.body, "2 · Review / Correction Ledger", bold=True).pack(fill="x", pady=(0, 5))
        _label(self.body, "The master spreadsheet the AI Document Review updates with checked cases, Keep / Rename / Defer decisions, corrected names, evidence and unresolved items. Improve Stage 2 reads it to assess software changes.", small=True).pack(fill="x")
        saved = app.cfg.get("ai_workflows") or {}
        self.ledger = Path(saved.get("ledger_path") or workflows.default_ledger_path())
        _button(self.body, "Open Review / Correction Ledger", lambda: _open_path(self, self.ledger)).pack(anchor="w", pady=(9, 12))
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
        messagebox.showinfo("Stage 2 is busy", "Wait for processing, recovery, scanning or the active AI Document Review to finish before launching another review. Use View AI session to watch the current review. The current run has not been interrupted.", parent=app)
        return None
    return _open_single_dialog(app, "ai-" + role, lambda: AIWorkflowDialog(app, namespace, role))


def open_notification_settings(app, namespace):
    return _open_single_dialog(app, "notifications", lambda: NotificationSettingsDialog(app, namespace))


def open_reports_menu(app, namespace):
    return _open_single_dialog(app, "reports", lambda: ReportsMenuDialog(app, namespace))


def configure_auto_review(app, namespace):
    """Pre-run automatic review setup. Usable from Settings or the main window.

    Never starts AI work. Saves cfg['ai_workflows']['auto_review'] through
    namespace['save_config'] and refreshes app._refresh_ai_review_summary when present.
    """
    return _open_single_dialog(app, "auto-review", lambda: AutoReviewSettingsDialog(app, namespace))


def current_ai_request_dir(app, namespace=None):
    """The request folder of the run the View AI session button should show, if any."""
    for name in ("_ai_review_request_dir", "_latest_ai_request_dir", "ai_review_request_dir"):
        value = getattr(app, name, None)
        if callable(value):
            value = value()
        if value:
            return Path(value)
    getter = (namespace or {}).get("current_ai_request_dir")
    if callable(getter):
        value = getter()
        if value:
            return Path(value)
    return None


def open_ai_session(app, namespace, request_dir=None):
    """Show the existing supervised run's live output; never start a second run.

    Returns the viewer process or None. The viewer is read-only and can be
    reopened any number of times for a saved run; closing it does not cancel
    the provider process.
    """
    target = Path(request_dir) if request_dir else current_ai_request_dir(app, namespace)
    if target is None:
        messagebox.showinfo("No AI session", "No supervised AI review has been launched for this run yet. Configure automatic review before Start; it launches after the completed accuracy audit. Opening this viewer does not start one.", parent=app)
        return None
    try:
        return live_output.open_live_output(target)
    except workflows.WorkflowError as exc:
        messagebox.showerror("Could not open the AI session", str(exc), parent=app)
        return None


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
