"""Operator UI for local preflight, delayed passwords and exact paid scopes."""
import json
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import stage2_source_recovery as recovery
from stage2_locking import writer_lock


class RecoveryWindow(tk.Toplevel):
    def __init__(self, app, engine, state_factory, version, build, estimate):
        super().__init__(app)
        self.app, self.engine, self.state_factory = app, engine, state_factory
        self.version, self.build, self.estimate = version, build, estimate
        self.title("Stage 2 — Source recovery / Locked documents")
        self.geometry("1100x720")
        self.minsize(850, 580)
        self.busy = False
        self.results = queue.Queue()
        self.controller = None
        self.transient(app)
        self.grab_set()
        self.summary_text = tk.StringVar(value="Inspecting the saved scope locally…")
        ttk.Label(self, textvariable=self.summary_text, wraplength=1000).pack(fill="x", padx=14, pady=10)
        ttk.Label(self, text="Default: Wait for all sources. You may close this window and resume days later. "
            "Accepted work is retained. Do not resubmit the entire batch.", wraplength=1000).pack(fill="x", padx=14)
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=14, pady=10)
        self.tree = ttk.Treeview(frame, columns=("worker", "file", "cause", "state"), show="headings", selectmode="extended")
        for field, width in (("worker", 180), ("file", 310), ("cause", 270), ("state", 250)):
            self.tree.heading(field, text=field.replace("_", " ").title())
            self.tree.column(field, width=width, minwidth=80)
        bar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        horizontal = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(xscrollcommand=horizontal.set)
        horizontal.pack(fill="x", padx=14)
        self.password = tk.StringVar()
        password_row = ttk.Frame(self)
        password_row.pack(fill="x", padx=14)
        ttk.Label(password_row, text="Password (memory only):").pack(side="left")
        self.password_entry = ttk.Entry(password_row, textvariable=self.password, show="•", width=28)
        self.password_entry.pack(side="left", padx=6)
        self.buttons = []
        self.button(password_row, "Try selected files", lambda: self.unlock(False))
        self.button(password_row, "Try all remaining locked files", lambda: self.unlock(True))
        for specs in (
            [("Retry local inspection", lambda: self.run("inspect")),
             ("Import unprotected replacement", self.replace),
             ("Remove from run (quarantine)", self.quarantine),
             ("Reinstate selected", self.reinstate)],
            [("Process ready documents now", lambda: self.submit(False)),
             ("Submit unlocked documents", lambda: self.submit(True)),
             ("Apply accepted / recover final checks", self.apply_accepted)],
            [("Reconcile uncertain submission (no resubmit)", lambda: self.run("reconcile"))],
            [("Export complete issue list", self.export), ("Open containing folder", self.open_folder),
             ("Wait for all sources / close", self.close)]):
            row = ttk.Frame(self)
            row.pack(fill="x", padx=14, pady=5)
            for label, command in specs:
                self.button(row, label, command)
        self.status = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status, wraplength=1000).pack(fill="x", padx=14, pady=8)
        self.cancel_button = ttk.Button(self, text="Pause operation (retain saved state)", command=self.pause)
        self.cancel_button.pack(anchor="e", padx=14, pady=(0, 8))
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.run("inspect")

    def button(self, parent, label, command):
        b = ttk.Button(parent, text=label, command=command)
        b.pack(side="left", padx=(0, 6))
        self.buttons.append(b)

    def selected(self):
        ids = list(self.tree.selection())
        if not ids:
            messagebox.showinfo("Select documents", "Select one or more documents in the list.", parent=self)
        return ids

    def confirmation(self, title, ids, extra=""):
        summary = self.controller.summary()
        workers = len({self.controller.data["records"][cid]["worker"] for cid in ids})
        return messagebox.askyesno(title,
            f"{len(ids)} documents across {workers} workers.\n"
            f"Cost already incurred: £{summary['cost_incurred_gbp']:.2f}.\n"
            f"Current scope: {summary['scope_outcome']}.\n\n{extra}", parent=self, default="no")

    def unlock(self, all_locked):
        ids = ([cid for cid, r in self.controller.data["records"].items() if r["state"] in recovery.LOCKED]
               if all_locked else self.selected())
        if not ids:
            return
        if not self.confirmation("Try password locally", ids,
                "Each file is authenticated separately. No additional provider cost. "
                "Files that fail remain locked. Digital signatures can be invalidated by decryption; "
                "signed files will remain locked for separate review."):
            return
        password = self.password.get()
        self.password.set("")
        self.run("unlock", ids, secret=[password])
        password = None

    def replace(self):
        ids = self.selected()
        if len(ids) != 1:
            return
        path = filedialog.askopenfilename(title="Select an unprotected replacement", parent=self)
        if path and self.confirmation("Replace exact source", ids,
                "The original is preserved in the recovery archive. The replacement must pass full local inspection. "
                "No provider submission occurs yet."):
            self.run("replace", ids, replacement=path)

    def quarantine(self):
        ids = self.selected()
        if ids and self.confirmation("Remove from run (quarantine)", ids,
                f"{len(ids)} documents will be deliberately excluded and NOT processed or reviewed. "
                "The run becomes partial scope. Verified originals are retained; reinstatement is available."):
            self.run("quarantine", ids)

    def reinstate(self):
        ids = self.selected()
        if ids and self.confirmation("Reinstate quarantined sources", ids,
                "Restore the exact retained originals to the run. Existing active files will never be overwritten."):
            self.run("reinstate", ids)

    def submit(self, unlocked_only):
        ids = [cid for cid, r in self.controller.data["records"].items()
               if r["state"] == "unlocked_validated" or (not unlocked_only and r["state"] == "ready")]
        if not ids:
            self.status.set("No newly validated, unsubmitted documents are ready.")
            return
        estimate = self.estimate(len(ids))
        unresolved = len(self.controller.unresolved())
        if self.confirmation("Confirm exact document submission", ids,
                f"Additional estimated total: £{estimate:.2f} including finishing/reserves.\n"
                f"{unresolved} unresolved sources will remain in the recovery queue. "
                "Affected workers cannot finish until their sources are resolved or explicitly excluded. "
                "This confirmation submits only the validated documents listed as ready."):
            self.run("submit", ids, allow_partial=bool(unresolved))

    def apply_accepted(self):
        if not self.close():
            return
        self.app.after(0, lambda: self.app._batch_check_status(source_bypass=True))

    def export(self):
        path = filedialog.asksaveasfilename(parent=self, title="Save complete source issue list",
            defaultextension=".json", filetypes=[("JSON", "*.json")])
        if path:
            self.controller.export(path)
            self.status.set("Complete issue list and resolution ledger saved.")

    def open_folder(self):
        ids = self.selected()
        if ids:
            folder = Path(self.controller.data["records"][ids[0]]["path"]).parent
            if folder.is_dir():
                os.startfile(folder)

    def run(self, action, ids=None, **options):
        if self.busy:
            return
        if action != "inspect" and not self.controller:
            self.status.set("Retry local inspection before choosing a resolution.")
            return
        self.engine._stop.clear()
        ids = ids or []
        token = self.controller.token(ids) if self.controller and ids else ""
        self.busy = self.app._recovery_busy = True
        self.app._refresh_run_controls(busy=True)
        for button in self.buttons:
            button.configure(state="disabled")
        self.password_entry.configure(state="disabled")
        self.status.set("Working… saved state and originals are retained.")

        def work():
            result = ""
            try:
                with writer_lock(self.engine.dir):
                    state = self.state_factory(self.engine.dir)
                    controller = recovery.Recovery(state, self.version, self.build)
                    if action == "inspect":
                        controller.resume(self.engine._batch_classification_view)
                        controller.preflight(self.engine._batch_classification_view,
                            self.engine._check_stop)
                        result = "Whole-run local preflight complete. No provider request was submitted."
                    elif action == "unlock":
                        secret = options.pop("secret")
                        try:
                            outcome = controller.unlock(ids, secret.pop(), token, self.engine._batch_classification_view,
                                stop=self.engine._check_stop)
                        finally:
                            secret.clear()
                        result = (f"{outcome['unlocked']} unlocked; {outcome['still_locked']} still locked; "
                            f"{outcome['unreadable']} unreadable after decryption; "
                            f"{outcome['signature_warning']} need signature review.")
                    elif action == "replace":
                        controller.replace(ids[0], options["replacement"], token, self.engine._batch_classification_view)
                        result = ("Replacement validated. Prior source was missing; no original archive could be created."
                            if controller.data["records"][ids[0]].get("original_unavailable")
                            else "Replacement validated. Original and lineage retained.")
                    elif action == "quarantine":
                        controller.quarantine(ids, token)
                        result = recovery.exclusion_summary(state)["statement"]
                    elif action == "reinstate":
                        controller.reinstate(ids, token)
                        controller.preflight(self.engine._batch_classification_view, self.engine._check_stop)
                        result = "Selected documents reinstated and inspected."
                    elif action == "submit":
                        if not self.engine.api.api_key:
                            raise recovery.RecoveryError("Set the provider API key in Settings before submitting.")
                        extra = self.estimate(len(ids))
                        incurred = controller.summary()["cost_incurred_gbp"]
                        budget = float(state.data.get("settings", {}).get("max_budget_gbp", 0) or 0)
                        if budget > 0 and incurred + extra > budget:
                            raise recovery.RecoveryError("Additional submission exceeds the saved run budget.")
                        submitted = recovery.submit_ready(controller, ids, token,
                            self.engine.api, state.data.get("primary_vocabulary") or self.engine.kb.vocabulary_block(),
                            allow_partial=options["allow_partial"], stop=self.engine._check_stop)
                        result = f"{len(submitted)} new document requests accepted. Use Apply accepted when results are ready."
                    elif action == "reconcile":
                        if not self.engine.api.api_key:
                            raise recovery.RecoveryError("Set the provider API key before reconciliation.")
                        result = self.engine.reconcile_source_submission(state)
                        controller = recovery.Recovery(state, self.version, self.build)
                self.results.put((controller, result))
            except Exception as exc:
                # Third-party exceptions can contain document content. Only
                # controlled RecoveryError messages are shown, never raw errors.
                safe = str(exc) if isinstance(exc, recovery.RecoveryError) else "Operation stopped; saved state retained. Retry local inspection to resume."
                self.results.put((None, safe))
            finally:
                if "secret" in options:
                    options.pop("secret").clear()
        threading.Thread(target=work, daemon=True).start()
        self.after(100, self.poll)

    def poll(self):
        try:
            controller, result = self.results.get_nowait()
        except queue.Empty:
            self.after(100, self.poll)
            return
        self.busy = self.app._recovery_busy = False
        for button in self.buttons:
            button.configure(state="normal" if controller or self.controller or button.cget("text") in
                ("Retry local inspection", "Wait for all sources / close") else "disabled")
        self.password_entry.configure(state="normal" if controller or self.controller else "disabled")
        self.app._refresh_folder_state()
        self.app._refresh_run_controls()
        if controller:
            self.controller = controller
            summary = controller.summary()
            self.summary_text.set(f"{summary['completed_workers']} workers completed | "
                f"{summary['locked']} locked | {summary['unlocked']} newly unlocked | "
                f"{summary['ready']} ready | {summary['submitted']} submitted | "
                f"{summary['corrupt_after_decryption']} corrupt after decryption | "
                f"{summary['excluded']} excluded | Cost incurred £{summary['cost_incurred_gbp']:.2f} | "
                f"{summary['scope_outcome']}")
            self.tree.delete(*self.tree.get_children())
            for cid, r in sorted(controller.data["records"].items(), key=lambda item:
                    (item[1].get("cause", ""), item[1]["worker"], item[1]["path"])):
                self.tree.insert("", "end", iid=cid, values=(r["worker"], Path(r["path"]).name,
                    r.get("cause") or "validated", r["state"]))
        self.status.set(result)

    def close(self):
        if self.busy:
            self.status.set("A durable operation is in progress; wait for its checkpoint before closing.")
            return False
        self.password.set("")
        self.destroy()
        return True

    def pause(self):
        self.password.set("")
        if self.busy:
            self.engine.stop()
            self.status.set("Pausing at the next safe boundary. Accepted requests and saved resolutions are retained.")
        else:
            self.close()
