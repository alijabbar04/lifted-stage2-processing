#!/usr/bin/env python3
r"""
Stage 2 - Re-check Unknowns & Tidy   (one-off, run AFTER Stage 2)
===================================================================
Overnight Batch mode can return a lot of documents it couldn't classify; Stage 2
files those as "Other - Unknown". This tool gives them a second pass and tidies
the worker folders.

For a chosen CARE-HOME folder, for EACH worker sub-folder it does two things:

  1) TIDY - removes every sub-folder EXCEPT "Bulk" and "Overwrite Documents".
     SAFETY: any real document found inside a doomed sub-folder is first MOVED UP
     into the worker folder (never deleted); only empty / junk-only folders are
     then removed. So nothing is lost - at worst a stray document ends up loose
     in the worker folder where you can see it. Program artefacts (leading-
     underscore / manifest files) are left untouched.

  2) RE-CHECK - finds every document named "Other - Unknown" (any "(NN)" suffix),
     re-classifies it with the Claude API using the EXACT same prompt, controlled
     vocabulary, disambiguation rules and prompt-caching Stage 2 uses, then:
       - if it now matches a real controlled type -> renames it to that type;
         and if that type belongs in Overwrite Documents, MOVES it there (dating
         Certificate of Sponsorship / Share Code Check Result like Stage 2 does);
       - otherwise it stays an Other document (relabelled "Other - <what it is>"
         when the AI can describe it, else left as "Other - Unknown").
     The processed-file manifest is updated so a later Stage 2 run won't revert
     the new names.

It reuses Stage2_Processing.pyw directly, so it can never drift from the main
app. Double-click to run (needs the same Anthropic API key Stage 2 uses, stored
in Windows Credential Manager).
"""
import os
import re
import shutil
import sys
import threading
import traceback
import importlib.machinery
import importlib.util
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox


# --------------------------------------------------------------------------- #
#  Load Stage2_Processing.pyw as an importable module (no GUI is launched -
#  its main() is guarded by __name__ == "__main__").
# --------------------------------------------------------------------------- #
def _find_stage2() -> Path:
    here = Path(__file__).resolve().parent
    candidates = []
    # FROZEN BUILD: Stage2_Processing.pyw is shipped as bundled data, so look
    # inside the PyInstaller extraction dir first (this is what makes the exe
    # work on a machine that has no Python and no project checkout).
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "Stage2_Processing.pyw")
        candidates.append(Path(sys.executable).resolve().parent
                          / "Stage2_Processing.pyw")
    candidates += [
        here / "Stage2_Processing.pyw",
        # Repo checkout run from somewhere else (e.g. cwd = repo root).
        Path.cwd() / "src" / "Stage2_Processing.pyw",
    ]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(
        "Could not find Stage2_Processing.pyw next to this script or at the "
        "known project path. Put this file in the same folder as Stage 2.")


_S2_PATH = _find_stage2()
_loader = importlib.machinery.SourceFileLoader("stage2_mod", str(_S2_PATH))
_spec = importlib.util.spec_from_loader("stage2_mod", _loader)
s2 = importlib.util.module_from_spec(_spec)
_loader.exec_module(s2)

# Executing Stage 2 above branded the shared usage ledger "Stage 2
# Processing"; re-brand so this tool's API calls appear under their own
# scope on Stage 2's analytics page.
if getattr(s2, "_api_usage", None) is not None:
    try:
        s2._api_usage.set_app("Re-check Unknowns")
    except Exception:
        pass


# folders we NEVER delete or descend into for deletion (case-insensitive)
KEEP_FOLDERS = {"bulk", "overwrite documents"}


def _norm(s: str) -> str:
    return re.sub(r"[\s\-]+", " ", (s or "").strip().lower())


def is_unknown_name(stem: str) -> bool:
    """True if a filename stem is the 'Other - Unknown' placeholder, tolerant of
    a trailing ' (NN)' rank/dedup suffix (e.g. 'Other - Unknown (72)')."""
    return s2.base_controlled_name(stem).strip().lower() == "other - unknown"


def _real_doc(p: Path) -> bool:
    return (p.is_file() and p.suffix.lower() in s2.DOC_EXT
            and not s2._is_junk(p) and not s2.is_program_file(p))


def looks_like_worker(d: Path) -> bool:
    """A folder is treated as a single worker folder (rather than a care-home
    folder full of workers) if it directly contains a Bulk / Overwrite Documents
    sub-folder or any loose document."""
    try:
        for c in d.iterdir():
            if c.is_dir() and _norm(c.name) in KEEP_FOLDERS:
                return True
            if _real_doc(c):
                return True
    except Exception:
        pass
    return False


# --------------------------------------------------------------------------- #
#  Core operations (pure functions; take a log callback)
# --------------------------------------------------------------------------- #
def tidy_worker(worker_dir: Path, log) -> dict:
    """Remove every sub-folder except Bulk / Overwrite Documents. Real documents
    inside a doomed sub-folder are moved up first. Returns counts."""
    removed = relocated = kept = 0
    subdirs = [d for d in worker_dir.iterdir() if d.is_dir()]
    subdirs.sort(key=lambda d: s2.natural_key(d.name))
    for child in subdirs:
        if _norm(child.name) in KEEP_FOLDERS or s2.is_program_file(child):
            continue
        # 1) move any real documents up into the worker folder
        for p in list(child.rglob("*")):
            if _real_doc(p):
                dest = s2.unique_path(worker_dir, p.stem, p.suffix)
                try:
                    shutil.move(str(p), str(dest))
                    relocated += 1
                    log(f"      moved up: {p.name}  ->  {dest.name}")
                except Exception as e:
                    log(f"      ! could not move {p.name}: {e}")
        # 2) delete the folder only if no real documents remain
        leftover = [p for p in child.rglob("*") if _real_doc(p)]
        if leftover:
            kept += 1
            log(f"      ! kept '{child.name}' - still holds "
                f"{len(leftover)} document(s) that could not be moved")
            continue
        shutil.rmtree(child, ignore_errors=True)
        if child.exists():
            s2._force_rmtree(child, log)
        if child.exists():
            kept += 1
            log(f"      ! could not delete '{child.name}' (locked / in use)")
        else:
            removed += 1
            log(f"      removed sub-folder: {child.name}")
    return {"removed": removed, "relocated": relocated, "kept": kept}


def _dated_overwrite_label(name: str, api, imgs, text, log) -> str:
    """CoS and Share Code Check Result carry a date suffix in Overwrite
    Documents, matching Stage 2. Returns the label to file under."""
    try:
        if name == "Certificate of Sponsorship":
            d = s2.parse_date(api.cos_issue_date(imgs, text))
            if d:
                return f"{name} - ({d.strftime('%d-%m-%Y')})"
        elif name == "Share Code Check Result":
            d = s2.parse_date(api.share_code_check(imgs, text).get("check_date", ""))
            if d:
                return f"{name} - ({d.strftime('%d-%m-%Y')})"
    except Exception as e:
        log(f"      (could not read date: {e})")
    return name


def recheck_unknowns(worker_dir: Path, kb, api, manifest, cfg, log,
                     should_stop, on_cost, esc_api=None) -> dict:
    """Re-classify every 'Other - Unknown' document under worker_dir.

    Runs each document through Stage 2's SHARED classification core
    (classify_document_core: full pages, rotation retry, optional stronger-
    model second opinion) and interprets the result with the same
    resolve_auto_review policy the in-app double-check uses - so this tool
    and the pipeline can never disagree about a document. Sideways scans are
    also physically rewritten upright (auto_rotate setting)."""
    zoom = float(cfg.get("resolution", 1.5))
    max_file_mb = float(cfg.get("max_file_mb", 25.0))
    auto_rotate = bool(cfg.get("auto_rotate", True))
    vocab = kb.vocabulary_block()
    stats = {"checked": 0, "matched": 0, "to_overwrite": 0,
             "relabelled": 0, "still_unknown": 0, "errors": 0, "skipped": 0,
             "rotated": 0}

    targets = [p for p in worker_dir.rglob("*")
               if p.is_file() and p.suffix.lower() in s2.DOC_EXT
               and not s2.is_program_file(p) and is_unknown_name(p.stem)]
    targets.sort(key=lambda p: s2.natural_key(p.name))
    if not targets:
        log("    no 'Other - Unknown' documents to re-check")
        return stats

    log(f"    re-checking {len(targets)} 'Other - Unknown' document(s)")
    for f in targets:
        if should_stop():
            break
        if not f.exists():
            continue
        if s2.file_too_big(f, max_file_mb) or s2.is_cloud_only_placeholder(f) \
                or not s2.content_matches_ext(f):
            stats["skipped"] += 1
            log(f"      - {f.name}: skipped (oversized / cloud-only / bad type)")
            continue
        try:
            fhash = s2.file_hash(f)
        except Exception:
            fhash = ""

        try:
            core = s2.classify_document_core(
                api, vocab, f, resolution=zoom, adaptive_pages=False,
                emit_cost=on_cost, escalation_api=esc_api)
        except s2.CreditExhausted:
            raise
        except Exception as e:
            stats["errors"] += 1
            log(f"      ! {f.name}: API error - left unchanged ({e})")
            continue
        stats["checked"] += 1
        result = core["result"]

        # physically fix a sideways scan: free local text-direction check
        # first, then the model's (confidence-gated) report
        rot = s2.detect_pdf_text_rotation(f)
        if not rot:
            rot = s2._rot_of(result)
            if rot and s2._conf_int(result) < 60:
                rot = 0
        if auto_rotate and rot and s2.fix_file_rotation(f, rot):
            stats["rotated"] += 1
            log(f"      · {f.name}: scan was rotated - saved upright ({rot} deg)")
            try:
                fhash = s2.file_hash(f)
            except Exception:
                fhash = ""

        new_name, group = s2.resolve_auto_review(kb, result)
        if not new_name:
            stats["still_unknown"] += 1
            log(f"      = {f.name}: still unrecognised, left as Other - Unknown")
            continue

        if group != "Other" and s2.is_overwrite_type(new_name):
            stats["matched"] += 1
            label = _dated_overwrite_label(new_name, api,
                                           core.get("used_imgs") or [],
                                           core.get("used_text") or "", log)
            odir = worker_dir / "Overwrite Documents"
            odir.mkdir(exist_ok=True)
            dest = s2.unique_path(odir, s2.safe_stem(label), f.suffix)
            try:
                shutil.move(str(f), str(dest))
                stats["to_overwrite"] += 1
                log(f"      + {f.name}  ->  Overwrite Documents/{dest.name}")
            except Exception as e:
                stats["errors"] += 1
                log(f"      ! could not move {f.name} to Overwrite "
                    f"Documents: {e}")
                continue
        else:
            dest = s2.unique_path(f.parent, s2.safe_stem(new_name), f.suffix)
            try:
                f.rename(dest)
                if group == "Other":
                    stats["relabelled"] += 1
                    log(f"      ~ {f.name}  ->  {dest.name}  (still Other)")
                else:
                    stats["matched"] += 1
                    log(f"      + {f.name}  ->  {dest.name}  (stays in "
                        f"{f.parent.name})")
            except Exception as e:
                stats["errors"] += 1
                log(f"      ! could not rename {f.name}: {e}")
                continue
        if fhash:
            manifest.record(fhash, api.model_id, zoom, new_name, group)
    return stats


# --------------------------------------------------------------------------- #
#  GUI
# --------------------------------------------------------------------------- #
class ReCheckApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.cfg = s2.load_config()
        s2.FX_RATE[0] = self.cfg.get("fx", 0.79)
        self.kb = None
        self.care_home = None
        self.workers = []
        self._stop = threading.Event()
        self._thread = None

        self.title("Stage 2 - Re-check Unknowns & Tidy")
        self.configure(bg=s2.BG)
        ui = 1.0
        if getattr(s2, "_api_usage", None) is not None:
            try:
                ui = s2._api_usage.tk_scale(self)
            except Exception:
                ui = 1.0
        self.geometry(f"{int(900*ui)}x{int(640*ui)}")
        self.minsize(int(760*ui), int(520*ui))

        top = tk.Frame(self, bg=s2.RIBBON, height=58)
        top.pack(side="top", fill="x")
        top.pack_propagate(False)
        tk.Label(top, text="Re-check Unknowns & Tidy", bg=s2.RIBBON,
                 fg=s2.RIBBON_FG, font=("Segoe UI", 15, "bold")).pack(
            side="left", padx=16)
        tk.Label(top, text="Run AFTER Stage 2  ·  removes stray sub-folders  ·  "
                           "re-classifies 'Other - Unknown'",
                 bg=s2.RIBBON, fg="#9aa4b0", font=("Segoe UI", 9)).pack(
            side="left")

        ctl = tk.Frame(self, bg=s2.BG)
        ctl.pack(fill="x", padx=14, pady=(12, 4))
        self.pick_btn = tk.Button(ctl, text="\U0001F4C1  Choose care-home folder & start",
                                  command=self._pick)
        s2.style_button(self.pick_btn, s2.ACCENT, "#6fb6ff")
        self.pick_btn.pack(side="left")
        self.stop_btn = tk.Button(ctl, text="■  Stop", command=self._stop_run,
                                  state="disabled")
        s2.style_button(self.stop_btn, s2.RED, s2.RED_HI)
        self.stop_btn.pack(side="left", padx=(8, 0))

        self.folder_lbl = tk.Label(self, text="No folder selected.", bg=s2.BG,
                                   fg=s2.FG_DIM, font=s2.UI, anchor="w",
                                   justify="left", wraplength=840)
        self.folder_lbl.pack(fill="x", padx=16, pady=(2, 4))

        info = tk.Frame(self, bg=s2.PANEL)
        info.pack(fill="x", padx=14, pady=(0, 6))
        self.status_lbl = tk.Label(info, text="Idle.", bg=s2.PANEL, fg=s2.FG,
                                   font=s2.UI, anchor="w")
        self.status_lbl.pack(side="left", padx=12, pady=8)
        self.cost_lbl = tk.Label(info, text="£0.0000", bg=s2.PANEL,
                                 fg=s2.GREEN_HI, font=("Segoe UI", 13, "bold"))
        self.cost_lbl.pack(side="right", padx=12)

        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", padx=16, pady=(0, 6))

        tk.Label(self, text="Activity log", bg=s2.BG, fg=s2.FG_DIM,
                 font=s2.UI_B).pack(anchor="w", padx=16)
        wrap = tk.Frame(self, bg=s2.BG)
        wrap.pack(fill="both", expand=True, padx=14, pady=(2, 12))
        self.log_text = tk.Text(wrap, bg="#0a0c0f", fg=s2.FG, font=s2.MONO,
                                relief="flat", wrap="word", state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(wrap, command=self.log_text.yview)
        sb.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=sb.set)

        if not s2.get_api_key():
            self.log("WARNING: no Anthropic API key found. This machine must "
                     "have had the Stage 2 installer run on it (the key lives "
                     "in Windows Credential Manager).")

    # ---- thread-safe UI helpers ----
    def log(self, msg):
        self.after(0, self._log_main, msg)

    def _log_main(self, msg):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def set_status(self, msg):
        self.after(0, lambda: self.status_lbl.configure(text=msg))

    def set_cost(self, gbp):
        self.after(0, lambda: self.cost_lbl.configure(text=f"£{gbp:.4f}"))

    def set_progress(self, done, total):
        self.after(0, lambda: self.progress.configure(
            maximum=max(total, 1), value=done))

    # ---- run control ----
    def _stop_run(self):
        self._stop.set()
        self.set_status("Stopping after the current document…")

    def _pick(self):
        if self._thread and self._thread.is_alive():
            return
        if not s2.get_api_key():
            messagebox.showwarning(
                "API key needed",
                "No Anthropic API key is stored on this machine. Run the Stage 2 "
                "installer first (it writes the key to Windows Credential "
                "Manager), then try again.")
            return
        start = str(s2.desktop_path())
        d = filedialog.askdirectory(
            title="Choose the CARE-HOME folder (the one containing the worker "
                  "folders)", initialdir=start)
        if not d:
            return
        picked = Path(d)

        # care-home (many workers) vs a single worker folder
        if looks_like_worker(picked):
            self.workers = [picked]
            self.care_home = picked.parent
        else:
            self.workers = s2.worker_dirs_in(picked)
            self.care_home = picked
        if not self.workers:
            messagebox.showinfo("Nothing to do",
                                "No worker sub-folders (or documents) were found "
                                "in that folder.")
            return

        # ---- local preview (no API, no changes) ----
        self.set_status("Scanning (no changes yet)…")
        doomed = relocate = unknowns = 0
        for w in self.workers:
            try:
                for c in w.iterdir():
                    if c.is_dir() and _norm(c.name) not in KEEP_FOLDERS \
                            and not s2.is_program_file(c):
                        doomed += 1
                        relocate += sum(1 for p in c.rglob("*") if _real_doc(p))
                unknowns += sum(1 for p in w.rglob("*")
                                if p.is_file()
                                and p.suffix.lower() in s2.DOC_EXT
                                and not s2.is_program_file(p)
                                and is_unknown_name(p.stem))
            except Exception:
                traceback.print_exc()

        model_name = self.cfg.get("model", s2.DEFAULT_MODEL)
        model_id = s2.MODELS.get(model_name, {}).get("id", "claude-haiku-4-5")
        if self.kb is None:
            self.kb = s2.KnowledgeBase()
        est = s2.estimate_run_cost_gbp(
            unknowns, model_id, float(self.cfg.get("resolution", 1.5)),
            adaptive=False, vocab_block=self.kb.vocabulary_block(),
            batch=False, include_second_pass=False, cached_prefix=True)

        self.folder_lbl.configure(
            text=f"Care home: {self.care_home}\nWorkers: {len(self.workers)}",
            fg=s2.FG)

        msg = (f"About to process {len(self.workers)} worker folder(s) in:\n"
               f"{self.care_home}\n\n"
               f"TIDY: {doomed} sub-folder(s) will be removed (everything except "
               f"'Bulk' and 'Overwrite Documents').\n")
        if relocate:
            msg += (f"      {relocate} document(s) inside those sub-folders will "
                    f"be MOVED UP into the worker folder first (never deleted).\n")
        else:
            msg += "      (those sub-folders look empty.)\n"
        msg += (f"\nRE-CHECK: {unknowns} 'Other - Unknown' document(s) will be "
                f"sent to the Claude API for re-classification.\n"
                f"      Model: {model_name}\n"
                f"      Estimated cost: ~£{est['gbp']:.2f} (billed on real usage)\n"
                f"      Matches that are overwrite-types move to 'Overwrite "
                f"Documents'; the rest stay put, renamed.\n\n"
                f"Nothing is deleted except empty sub-folders. Proceed?")
        if not messagebox.askyesno("Confirm re-check & tidy", msg, icon="question"):
            self.set_status("Cancelled.")
            return

        self._stop.clear()
        self.pick_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._thread = threading.Thread(
            target=self._run, args=(model_id,), daemon=True)
        self._thread.start()

    def _run(self, model_id):
        totals = {"removed": 0, "relocated": 0, "kept": 0, "checked": 0,
                  "matched": 0, "to_overwrite": 0, "relabelled": 0,
                  "still_unknown": 0, "errors": 0, "skipped": 0}
        status = "done"
        try:
            api = s2.ClaudeAPI(s2.get_api_key(), model_id)
            # stronger-model second opinion for unsettled documents, exactly
            # as Stage 2's Engine builds it
            esc_api = None
            if bool(self.cfg.get("second_opinion", True)):
                prim = s2.MODELS_BY_ID.get(model_id, {})
                strong = s2.MODELS_BY_ID.get(s2.SECOND_OPINION_MODEL_ID, {})
                if (model_id != s2.SECOND_OPINION_MODEL_ID
                        and prim.get("in", 99.0) < strong.get("in", 0.0)):
                    esc_api = s2.ClaudeAPI(s2.get_api_key(),
                                           s2.SECOND_OPINION_MODEL_ID)
            manifest = s2.ProcessedManifest(self.care_home)
            max_budget = float(self.cfg.get("max_budget_gbp", 25.0))

            def cost_gbp():
                gbp = s2.tokens_cost_gbp(api.model_id, api.in_tokens,
                                         api.out_tokens, batch=False)
                if esc_api is not None:
                    gbp += s2.tokens_cost_gbp(esc_api.model_id,
                                              esc_api.in_tokens,
                                              esc_api.out_tokens, batch=False)
                return gbp

            def on_cost():
                self.set_cost(cost_gbp())

            def should_stop():
                if self._stop.is_set():
                    return True
                if max_budget > 0 and cost_gbp() >= max_budget:
                    self.log(f"\n*** Budget limit £{max_budget:.2f} reached - "
                             f"stopping. Raise 'Max spend / run' in Stage 2 "
                             f"Settings to continue. ***")
                    self._stop.set()
                    return True
                return False

            total = len(self.workers)
            for idx, w in enumerate(self.workers, 1):
                if should_stop():
                    status = "stopped"
                    break
                self.set_progress(idx - 1, total)
                self.set_status(f"Worker {idx}/{total}: {w.name}")
                self.log(f"\n=== Worker {idx}/{total}: {w.name} ===")

                self.log("  [tidy] removing stray sub-folders")
                t = tidy_worker(w, self.log)
                for k in ("removed", "relocated", "kept"):
                    totals[k] += t[k]
                self.log(f"  tidied: {t['removed']} folder(s) removed, "
                         f"{t['relocated']} document(s) moved up"
                         + (f", {t['kept']} kept (not empty)" if t["kept"] else ""))

                self.log("  [re-check] re-classifying 'Other - Unknown'")
                r = recheck_unknowns(w, self.kb, api, manifest, self.cfg,
                                     self.log, should_stop, on_cost,
                                     esc_api=esc_api)
                for k in r:
                    totals[k] = totals.get(k, 0) + r[k]
                manifest.save()
                on_cost()

            self.set_progress(total, total)
            manifest.save()
        except s2.CreditExhausted as e:
            status = "credit"
            self.log(f"\n*** STOPPED: API credit exhausted. {e.detail} ***")
            try:
                s2.ProcessedManifest(self.care_home).save()
            except Exception:
                pass
        except Exception as e:
            status = "error"
            self.log(f"\n! fatal error: {e}")
            traceback.print_exc()
        self.after(0, self._done, totals, status)

    def _done(self, totals, status):
        self.pick_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        head = {"done": "Re-check complete",
                "stopped": "Stopped",
                "credit": "Stopped - API credit ran out",
                "error": "Finished with an error"}.get(status, "Finished")
        self.set_status(head + ".")
        summary = (
            f"{head}\n\n"
            f"Sub-folders removed   : {totals['removed']}\n"
            f"Documents moved up    : {totals['relocated']}\n"
            f"Sub-folders kept (not empty): {totals['kept']}\n\n"
            f"Unknowns re-checked   : {totals['checked']}\n"
            f"  -> matched a type   : {totals['matched']}\n"
            f"  -> moved to Overwrite Documents: {totals['to_overwrite']}\n"
            f"  -> relabelled (still Other): {totals['relabelled']}\n"
            f"  -> still Unknown    : {totals['still_unknown']}\n"
            f"  -> saved upright (was rotated): {totals.get('rotated', 0)}\n"
            f"  -> skipped          : {totals['skipped']}\n"
            f"Errors                : {totals['errors']}\n\n"
            f"Estimated API cost this run: {self.cost_lbl.cget('text')}")
        if totals["kept"]:
            summary += ("\n\nNote: some sub-folders were kept because documents "
                        "inside them could not be moved (locked / cloud-only). "
                        "Check the log.")
        messagebox.showinfo("Re-check Unknowns & Tidy", summary)


def main():
    if getattr(s2, "_api_usage", None) is not None:
        try:
            s2._api_usage.enable_high_dpi()
        except Exception:
            pass
    app = ReCheckApp()
    app.mainloop()


if __name__ == "__main__":
    main()
