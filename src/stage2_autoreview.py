"""Durable, provider-neutral automatic audit-review supervision.

The processing UI owns user confirmation and the processing/audit lifecycle.
This module owns the immutable per-run review snapshot, the one-shot launch
claim, and semantic completion checks.  Importing it has no Tk or provider side
effects, which keeps restart reconciliation and offline tests deterministic.
"""
from __future__ import annotations

import contextlib
from collections.abc import Mapping
import hashlib
import inspect
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone
import uuid


SCHEMA_VERSION = 1
TERMINAL_STATES = {
    "disabled", "cancelled", "no-candidates", "completed",
    "completed-with-unresolved", "failed", "needs-attention",
    "outputs-awaiting-verification",
}


class AutoReviewError(RuntimeError):
    """A safe, actionable automatic-review error."""


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _identity(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _file_hash(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _same_path(left, right):
    return str(Path(left).resolve()).rstrip("\\/").casefold() == str(Path(right).resolve()).rstrip("\\/").casefold()


def _write_json_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".autoreview-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def _file_lock(path):
    """Permanent-file, nonblocking OS lock; never delete/recreate the inode."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    locked = False
    try:
        stream.seek(0, 2)
        if not stream.tell():
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise AutoReviewError("Another Stage 2 process is updating this automatic review; check again shortly.") from exc
        yield
    finally:
        if locked:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def _supported_call(function, *args, **kwargs):
    """Pass evolving workflow kwargs only when the installed helper accepts them."""
    signature = inspect.signature(function)
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()):
        return function(*args, **kwargs)
    return function(*args, **{key: value for key, value in kwargs.items() if key in signature.parameters})


def _public_codex_homes(entries):
    """Persist profile references, never arbitrary config keys or credentials."""
    result = []
    for entry in entries or ():
        if isinstance(entry, (str, os.PathLike)):
            result.append({"path": str(Path(entry).expanduser().resolve())})
            continue
        if not isinstance(entry, dict) or not entry.get("path"):
            continue
        clean = {"path": str(Path(entry["path"]).expanduser().resolve())}
        for key in ("id", "name", "email"):
            if entry.get(key):
                clean[key] = str(entry[key])
        result.append(clean)
    return result


def _choice_value(choice, key, default=None):
    if isinstance(choice, Mapping):
        return choice.get(key, default)
    return getattr(choice, key, default)


class Stage2AutoReviewController:
    """Persist and reconcile automatic review runs for live and batch modes."""

    def __init__(self, registry_dir=None, notify=None):
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        self.registry_dir = Path(registry_dir or local / "Lifted" / "Stage2" / "automatic-review").resolve()
        self.notify = notify
        self._live_runs = {}

    def capture_snapshot(self, *, run_id, processing_root, document_root, care_home,
                         worker_names, settings, accuracy_audit_enabled,
                         workflows=None):
        """Validate and return an uncommitted immutable start-time snapshot."""
        if not str(run_id).strip():
            raise AutoReviewError("The processing run needs a durable run ID before Start.")
        processing = Path(processing_root).resolve(strict=True)
        documents = Path(document_root).resolve(strict=True)
        if not processing.is_dir() or not documents.is_dir():
            raise AutoReviewError("The processing and document roots must be existing folders.")
        names = self._worker_names(worker_names)
        config = dict(settings or {})
        enabled = bool(config.get("enabled"))
        if enabled and not accuracy_audit_enabled:
            raise AutoReviewError("Automatic AI review requires the Accuracy Audit to be enabled for this run.")

        source = self._required_path(config, "source_root", enabled, directory=True)
        assets = self._required_path(config, "assets_root", enabled, directory=True)
        workspace = self._required_path(config, "workspace_root", enabled, directory=False)
        ledger = self._required_path(config, "ledger_path", enabled, directory=False)
        misnaming = self._required_path(config, "misnaming_path", enabled, directory=False)
        homes = _public_codex_homes(config.get("codex_homes", ()))
        selection = {
            "account_id": str(config.get("account_id") or ""),
            "model_key": str(config.get("model_key") or ""),
            "effort": str(config.get("effort") or ""),
            "expected_email": str(config.get("expected_email") or ""),
        }
        resolved = {}
        asset_payloads = {}
        if enabled:
            if not source or not all((source / "src" / name).is_file()
                                     for name in ("ai_review.py", "Stage2_Processing.pyw")):
                raise AutoReviewError("Automatic review needs the selected Stage 2 source containing src/ai_review.py and Stage2_Processing.pyw.")
            wf = workflows or self._workflows()
            rule_files = tuple(getattr(wf, "RULE_FILES", ()))
            if (any(Path(name).name != name for name in rule_files)
                    or not assets or (rule_files and not all((assets / name).is_file() for name in rule_files))):
                raise AutoReviewError("The selected AI review assets folder is incomplete.")
            for name in rule_files:
                content = (assets / name).read_text(encoding="utf-8-sig")
                asset_payloads[name] = {"sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                                         "content": content}
            if not selection["account_id"] or not selection["model_key"] or not selection["effort"]:
                raise AutoReviewError("Choose an AI account, model and effort before processing starts.")
            choice_fn = getattr(wf, "model_choice", None)
            if choice_fn:
                choice = _supported_call(choice_fn, selection["model_key"], effort=selection["effort"], role="audit-review")
            else:
                choice = getattr(wf, "MODEL_CHOICES", {}).get(selection["model_key"])
            if not choice:
                raise AutoReviewError("The selected audit-review model is unavailable.")
            accounts = _supported_call(wf.discover_accounts, codex_homes=homes)
            account = next((item for item in accounts if str(item.id) == selection["account_id"]), None)
            if account is None:
                raise AutoReviewError("The selected AI account profile is no longer registered.")
            verified = _supported_call(
                wf.validate_selection, account, selection["model_key"],
                expected_email=selection["expected_email"] or None,
                effort=selection["effort"], role="audit-review",
            )
            actual_email = str((verified or {}).get("email") or "")
            if not actual_email:
                raise AutoReviewError("The selected AI account did not return a verified email identity.")
            selection["expected_email"] = actual_email
            resolved = {
                "provider": str((verified or {}).get("provider") or _choice_value(choice, "provider", "")),
                "model": str((verified or {}).get("model") or _choice_value(choice, "id", "")),
                "effort": str((verified or {}).get("effort") or selection["effort"]),
                "account_email": actual_email,
                "validation_status": str((verified or {}).get("status") or "verified"),
            }

        durable_assets = self._state_path(processing, str(run_id)).parent / (self._run_digest(run_id) + "-assets")
        snapshot = {
            "schema_version": SCHEMA_VERSION,
            "captured_utc": _utc_now(),
            "run_id": str(run_id),
            "care_home": str(care_home or "").strip(),
            "processing_root": str(processing),
            "document_root": str(documents),
            "scope_worker_names": names,
            "accuracy_audit_enabled": bool(accuracy_audit_enabled),
            "review": {
                "enabled": enabled,
                **selection,
                **resolved,
                "allow_document_changes": bool(config.get("allow_document_changes")),
                "review_all_flags": bool(config.get("review_all_flags")),
                "source_root": str(source) if source else "",
                # PyInstaller's source assets may live under disposable _MEIPASS.
                # Capture their exact text now and materialize this stable path at commit.
                "assets_root": str(durable_assets) if enabled else "",
                "asset_payloads": asset_payloads,
                "workspace_root": str(workspace) if workspace else "",
                "ledger_path": str(ledger) if ledger else "",
                "misnaming_path": str(misnaming) if misnaming else "",
                "codex_homes": homes,
            },
        }
        snapshot["snapshot_id"] = _identity(snapshot)
        return snapshot

    def commit_run(self, snapshot):
        """Commit exactly the confirmed snapshot; an existing run is immutable."""
        self._verify_snapshot(snapshot)
        path = self._state_path(snapshot["processing_root"], snapshot["run_id"])
        with _file_lock(path.with_suffix(path.suffix + ".lock")):
            old = self._read(path)
            if old:
                if old.get("snapshot", {}).get("snapshot_id") == snapshot["snapshot_id"]:
                    return self._public(old)
                raise AutoReviewError("This run ID is already bound to a different immutable review snapshot.")
            state = {
                "schema_version": SCHEMA_VERSION,
                "revision": 1,
                "snapshot": json.loads(json.dumps(snapshot)),
                "state": "waiting-processing" if snapshot["review"]["enabled"] else "disabled",
                "summary": "Waiting for processing and its Accuracy Audit." if snapshot["review"]["enabled"] else "Automatic AI review is off for this run.",
                "created_utc": _utc_now(),
                "updated_utc": _utc_now(),
                "processing_receipt": None,
                "audit_receipt": None,
                "launch_claim": None,
                "request_dir": None,
                "runner": None,
                "completion": None,
                "history": [{"at": _utc_now(), "state": "committed"}],
            }
            self._materialize_assets(snapshot)
            _write_json_atomic(path, state)
        return self._public(state)

    def record_processing_complete(self, run_id, *, processing_root=None,
                                   worker_dirs, skipped_workers=(), errors=()):
        """Record the exact final/moved worker scope after all writers finish."""
        path, state = self._locate(run_id, processing_root)
        snapshot = state["snapshot"]
        error_rows = [str(item) for item in errors or ()]
        skipped = self._worker_names(skipped_workers, allow_empty=True)
        workers = self._worker_dirs(
            worker_dirs, snapshot["document_root"],
            allow_empty=bool(error_rows or skipped))
        worker_names = [Path(item).name for item in workers]
        completed_keys = {name.casefold() for name in worker_names}
        receipt = {
            "status": "complete",
            "completed_utc": _utc_now(),
            "run_id": snapshot["run_id"],
            "processing_root": snapshot["processing_root"],
            "document_root": snapshot["document_root"],
            "worker_dirs": workers,
            "worker_names": worker_names,
            "initial_scope_worker_names": snapshot["scope_worker_names"],
            "unaudited_scope_worker_names": [name for name in snapshot["scope_worker_names"]
                                               if name.casefold() not in completed_keys],
            "skipped_workers": skipped,
            "errors": error_rows,
        }
        receipt["receipt_id"] = _identity({key: value for key, value in receipt.items()
                                            if key != "completed_utc"})

        def update(current):
            old = current.get("processing_receipt")
            if old:
                if old.get("receipt_id") != receipt["receipt_id"]:
                    raise AutoReviewError("Processing completion was already recorded with a different final worker scope.")
                return
            current["processing_receipt"] = receipt
            if current["state"] not in TERMINAL_STATES:
                current["state"] = "waiting-audit"
                current["summary"] = "Processing is complete; waiting for the matching Accuracy Audit receipt."

        result = self._mutate(path, update, "processing-complete")
        self._emit("processing_complete", result)
        return result

    def record_audit_receipt(self, run_id, *, processing_root=None, report_path,
                             status, worker_dirs=None, report_sha256=None):
        """Bind one exact, completely-written audit report to the processing run."""
        if status != "complete":
            raise AutoReviewError("Only an exactly complete Accuracy Audit can arm automatic AI review.")
        path, state = self._locate(run_id, processing_root)
        snapshot = state["snapshot"]
        processing = state.get("processing_receipt")
        if not processing or processing.get("status") != "complete":
            raise AutoReviewError("Record processing completion and its final worker scope before the audit receipt.")
        report = Path(report_path).resolve(strict=True)
        if not report.is_file() or report.suffix.casefold() not in (".csv", ".xlsx"):
            raise AutoReviewError("The completed audit receipt must name its exact CSV or Excel report.")
        actual_hash = _file_hash(report)
        if report_sha256 and str(report_sha256).casefold() != actual_hash:
            raise AutoReviewError("The audit report hash does not match its completion receipt.")
        workers = (self._worker_dirs(worker_dirs, snapshot["document_root"])
                   if worker_dirs is not None else list(processing["worker_dirs"]))
        if [item.casefold() for item in workers] != [item.casefold() for item in processing["worker_dirs"]]:
            raise AutoReviewError("The audit worker scope differs from the recorded final processing scope.")
        receipt = {
            "status": "complete", "completed_utc": _utc_now(),
            "run_id": snapshot["run_id"], "processing_root": snapshot["processing_root"],
            "document_root": snapshot["document_root"], "worker_dirs": workers,
            "worker_names": [Path(item).name for item in workers],
            "report_path": str(report), "report_sha256": actual_hash,
            "processing_receipt_id": processing["receipt_id"],
        }
        receipt["receipt_id"] = _identity({key: value for key, value in receipt.items()
                                            if key != "completed_utc"})

        def update(current):
            old = current.get("audit_receipt")
            if old:
                if old.get("receipt_id") != receipt["receipt_id"]:
                    raise AutoReviewError("A different completed audit is already bound to this run.")
                return
            current["audit_receipt"] = receipt
            can_rearm = (not current.get("launch_claim")
                         and current["state"] in ("waiting-audit", "needs-attention", "ready"))
            if can_rearm:
                current["state"] = "ready"
                current["summary"] = "Accuracy Audit complete; automatic AI review is ready."
                current.pop("attention", None)

        result = self._mutate(path, update, "audit-complete")
        self._emit("audit_complete", result)
        return result

    def record_audit_status(self, run_id, status, *, processing_root=None,
                            reason=None, details=None):
        """Persist a non-complete audit outcome without ever arming review."""
        if status == "complete":
            raise AutoReviewError("Use record_audit_receipt with the exact report path and hash for a complete audit.")
        if status not in ("pending", "running", "failed", "skipped", "disabled"):
            raise AutoReviewError("Unsupported Accuracy Audit status.")
        path, _state = self._locate(run_id, processing_root)
        event = {"status": status, "at": _utc_now(), "reason": str(reason or ""),
                 "details": dict(details or {})}

        def update(current):
            if current.get("audit_receipt"):
                raise AutoReviewError("A complete immutable audit receipt is already bound to this run.")
            current["audit_status"] = event
            if current["state"] not in ("disabled", "cancelled"):
                current["state"] = "waiting-audit" if status in ("pending", "running") else "needs-attention"
                current["summary"] = (f"Accuracy Audit is {status}; automatic AI review is not armed."
                                      + ((" " + str(reason)) if reason else ""))
                if status in ("failed", "skipped", "disabled"):
                    current["attention"] = {"at": event["at"], "reason": "audit-" + status,
                                            "detail": str(reason or "")}

        result = self._mutate(path, update, "audit-" + status)
        self._emit("audit_" + status, result)
        return result

    def maybe_launch(self, run_id, *, processing_root=None, workflows=None,
                     review_helper=None):
        """Claim, prepare, and submit at most one paid request for a run."""
        path, state = self._locate(run_id, processing_root)
        if state["state"] != "ready":
            return self.reconcile(run_id, processing_root=processing_root,
                                  workflows=workflows, review_helper=review_helper)
        try:
            self._verify_ready(state, review_helper=review_helper)
        except Exception as exc:
            detail = str(exc) or exc.__class__.__name__
            result = self._mutate(
                path,
                lambda current: current.update(
                    state="needs-attention",
                    summary="Automatic review pre-launch verification failed; no request was submitted. " + detail,
                    attention={"at": _utc_now(), "reason": "prelaunch-verification", "detail": detail},
                ),
                "prelaunch-verification-failed",
            )
            self._emit("review_needs_attention", result)
            return result
        claim = {"claim_id": uuid.uuid4().hex, "claimed_utc": _utc_now(), "owner_pid": os.getpid()}

        def claim_update(current):
            if current["state"] != "ready" or current.get("launch_claim"):
                raise AutoReviewError("This automatic review has already been claimed; opening/checking it will not submit another request.")
            current["launch_claim"] = claim
            current["state"] = "preparing"
            current["summary"] = "Preparing the exact verified AI review request."

        self._mutate(path, claim_update, "launch-claimed")
        try:
            wf = workflows or self._workflows()
            helper = review_helper or self._review_helper()
            current = self._read(path)
            prepared = self._prepare_workflow(current, wf)
            request_dir = Path(prepared.request_dir).resolve()

            def prepared_update(latest):
                latest["request_dir"] = str(request_dir)
                latest["summary"] = "Prepared request; verifying the actual review queue before launch."

            self._mutate(path, prepared_update, "request-prepared")
            queue = self._prepare_queue(current, prepared, helper)
            self._verify_queue_binding(current, queue)
            candidates = queue.get("candidates")
            if not isinstance(candidates, list):
                raise AutoReviewError("The review helper returned an invalid candidate queue.")
            if not candidates:
                def no_candidates(latest):
                    latest["state"] = "no-candidates"
                    latest["summary"] = "Accuracy Audit completed with no candidates in the configured review scope; no paid AI review was started."
                    latest["completion"] = {"completed_utc": _utc_now(), "candidate_count": 0,
                                              "queue_run_id": queue.get("run_id"), "request_dir": str(request_dir)}
                result = self._mutate(path, no_candidates, "no-candidates")
                self._emit("no_candidates", result)
                return result
            run = wf.launch_headless(prepared, authorized_unattended=True)
            self._live_runs[str(path)] = run
            runner = self._runner_details(prepared, run, len(candidates), queue)

            def launched(latest):
                latest["state"] = "running"
                latest["summary"] = f"AI Document Review is running for {len(candidates)} candidate(s)."
                latest["runner"] = runner

            result = self._mutate(path, launched, "launched")
            viewer_error = None
            try:
                opener = self._output_opener(wf)
                if opener:
                    _supported_call(opener, request_dir,
                                    title=f"AI Document Review · {current['snapshot']['review']['model']} · {current['snapshot']['review']['expected_email']}")
            except Exception as exc:  # Viewer failure must not disguise/cancel a running provider job.
                viewer_error = str(exc)
            if viewer_error:
                result = self._mutate(path, lambda latest: latest["runner"].update(viewer_error=viewer_error), "viewer-error")
            self._emit("review_launched", result)
            return result
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__

            def failed(latest):
                latest["state"] = "needs-attention"
                latest["summary"] = "Automatic review was claimed but could not be safely launched. It will not retry automatically. " + message
                latest["attention"] = {"at": _utc_now(), "reason": "prepare-or-start-failed", "detail": message}

            result = self._mutate(path, failed, "launch-needs-attention")
            self._emit("review_needs_attention", result)
            return result

    def reconcile(self, run_id, *, processing_root=None, workflows=None,
                  review_helper=None):
        """Reconcile a running/exited request without ever resubmitting it."""
        path, state = self._locate(run_id, processing_root)
        if state["state"] in ("running", "outputs-awaiting-verification", "needs-attention") and state.get("request_dir"):
            request_dir = Path(state["request_dir"])
            live = self._live_runs.get(str(path))
            exit_code = None
            if live is not None:
                exit_code = live.poll()
                if exit_code is None:
                    return self._public(state)
            runner_status = self._read(request_dir / "runner-status.json")
            if exit_code is None and runner_status:
                exit_code = runner_status.get("process_exit_code")
                output_set = all((request_dir / name).is_file() for name in (
                    "apply_plan.json", "REVIEW_TRANSACTION.json", "REVIEW_DECISIONS.json"))
                if (not output_set and runner_status.get("state") == "running"
                        and self._pid_alive(runner_status.get("pid"))):
                    if state["state"] != "running":
                        return self._mutate(path, lambda latest: latest.update(state="running", summary="AI Document Review is still running."), "running")
                    return self._public(state)
            helper = review_helper or self._review_helper()
            try:
                completion = self._verify_outputs(state, helper)
            except Exception as exc:
                detail = str(exc) or exc.__class__.__name__
                if exit_code not in (None, 0):
                    new_state, reason = "failed", f"Provider process exited with code {exit_code}; {detail}"
                elif exit_code == 0:
                    new_state, reason = "outputs-awaiting-verification", detail
                elif runner_status.get("state") == "running":
                    new_state, reason = "needs-attention", "The recorded process is no longer running and its exit outcome is ambiguous; " + detail
                else:
                    return self._public(state)

                def invalid(latest):
                    latest["state"] = new_state
                    latest["summary"] = reason
                    latest["attention"] = {"at": _utc_now(), "reason": new_state, "detail": reason}
                    latest.setdefault("runner", {})["process_exit_code"] = exit_code

                result = self._mutate(path, invalid, new_state)
                self._emit("review_needs_attention", result)
                return result

            result_state = "completed-with-unresolved" if completion["unresolved_count"] else "completed"
            def complete(latest):
                latest["state"] = result_state
                latest["summary"] = ("AI Document Review completed with unresolved items."
                                     if completion["unresolved_count"] else "AI Document Review and ledger reconciliation completed.")
                latest["completion"] = completion
                latest.pop("attention", None)
            result = self._mutate(path, complete, result_state)
            self._emit("review_completed", result)
            return result
        if state["state"] == "preparing" and state.get("launch_claim"):
            # Recover a launch that crossed the process/state-write boundary.
            # runner-status is written by launch_headless only after submission;
            # its presence is evidence to supervise, never a reason to resubmit.
            request_dir = Path(state["request_dir"]) if state.get("request_dir") else None
            runner_status = self._read(request_dir / "runner-status.json") if request_dir else {}
            if runner_status:
                def recovered(latest):
                    latest["state"] = ("running" if runner_status.get("state") == "running"
                                       else "outputs-awaiting-verification")
                    latest["summary"] = "Recovered the already-submitted AI review; no second request was launched."
                    latest["runner"] = {**(latest.get("runner") or {}), **runner_status,
                                        "request_dir": str(request_dir)}
                self._mutate(path, recovered, "launched-recovered")
                return self.reconcile(run_id, processing_root=processing_root,
                                      workflows=workflows, review_helper=review_helper)
            # A restart between claim and any proof of launch is deliberately not retried.
            def ambiguous(latest):
                latest["state"] = "needs-attention"
                latest["summary"] = "A launch claim exists but launch completion was not durably recorded. Review the retained request; automatic retry is blocked."
                latest["attention"] = {"at": _utc_now(), "reason": "ambiguous-launch"}
            return self._mutate(path, ambiguous, "ambiguous-launch")
        return self._public(state)

    def cancel_queued(self, run_id, *, processing_root=None):
        path, _state = self._locate(run_id, processing_root)
        def cancel(current):
            if current.get("launch_claim") or current["state"] not in ("waiting-processing", "waiting-audit", "ready"):
                raise AutoReviewError("Only an unclaimed queued automatic review can be cancelled.")
            current["state"] = "cancelled"
            current["summary"] = "Queued automatic AI review was cancelled; processing and audit records were not changed."
            current["cancelled_utc"] = _utc_now()
        result = self._mutate(path, cancel, "cancelled")
        self._emit("review_cancelled", result)
        return result

    def view_existing(self, run_id, *, processing_root=None, workflows=None):
        _path, state = self._locate(run_id, processing_root)
        if not state.get("request_dir"):
            raise AutoReviewError("This run has no prepared AI session to view yet.")
        wf = workflows or self._workflows()
        opener = self._output_opener(wf)
        if not opener:
            raise AutoReviewError("The installed workflow viewer is unavailable.")
        return _supported_call(opener, Path(state["request_dir"]), title="Stage 2 AI Document Review")

    def get_status(self, run_id, *, processing_root=None):
        _path, state = self._locate(run_id, processing_root)
        return self._public(state)

    def find_runs(self, processing_root):
        root_dir = self._root_dir(processing_root) / "runs"
        results = []
        for path in sorted(root_dir.glob("*.json")) if root_dir.is_dir() else ():
            state = self._read(path)
            if state:
                results.append(self._public(state))
        return sorted(results, key=lambda item: item.get("updated_utc", ""), reverse=True)

    def bind_engine(self, engine, run_id, *, processing_root=None):
        """Return a thin adapter for live/batch engines; no callbacks are replaced."""
        return EngineAutoReviewBinding(self, engine, run_id, processing_root)

    # ---- implementation -------------------------------------------------

    @staticmethod
    def _required_path(config, key, required, *, directory):
        raw = config.get(key)
        if not raw:
            if required:
                raise AutoReviewError(f"Automatic review setting '{key}' is required.")
            return None
        path = Path(raw).expanduser().resolve()
        if directory and not path.is_dir():
            raise AutoReviewError(f"Automatic review folder does not exist: {path}")
        if not directory and key == "workspace_root":
            parent = path if path.exists() else path.parent
            if not parent.is_dir():
                raise AutoReviewError(f"Automatic review workspace parent does not exist: {parent}")
        return path

    @staticmethod
    def _worker_names(values, allow_empty=False):
        names = []
        seen = set()
        for value in values or ():
            name = Path(str(value)).name if isinstance(value, os.PathLike) else str(value).strip()
            if not name or name in (".", "..") or Path(name).name != name:
                raise AutoReviewError("Worker scope must contain exact immediate folder names.")
            key = name.casefold()
            if key in seen:
                raise AutoReviewError("Worker scope contains duplicate folder names.")
            seen.add(key)
            names.append(name)
        if not names and not allow_empty:
            raise AutoReviewError("Select at least one worker folder for this processing run.")
        return names

    def _worker_dirs(self, values, document_root, *, allow_empty=False):
        root = Path(document_root).resolve(strict=True)
        paths = []
        seen = set()
        for value in values or ():
            path = Path(value).resolve(strict=True)
            if not path.is_dir() or path.parent != root or path.is_symlink():
                raise AutoReviewError("Final audit scope must contain existing immediate worker folders under the captured document root.")
            key = str(path).casefold()
            if key in seen:
                raise AutoReviewError("Final worker scope contains duplicates.")
            seen.add(key)
            paths.append(str(path))
        if not paths and not allow_empty:
            raise AutoReviewError("The completed processing receipt has no final worker folders to audit.")
        return paths

    @staticmethod
    def _verify_snapshot(snapshot):
        if not isinstance(snapshot, dict) or snapshot.get("schema_version") != SCHEMA_VERSION:
            raise AutoReviewError("Unsupported automatic-review snapshot.")
        clean = dict(snapshot)
        expected = clean.pop("snapshot_id", None)
        if not expected or _identity(clean) != expected:
            raise AutoReviewError("The automatic-review snapshot changed after preflight.")

    def _root_dir(self, processing_root):
        resolved = str(Path(processing_root).resolve()).rstrip("\\/")
        return self.registry_dir / "roots" / hashlib.sha256(resolved.casefold().encode("utf-8")).hexdigest()

    def _state_path(self, processing_root, run_id):
        return self._root_dir(processing_root) / "runs" / (self._run_digest(run_id) + ".json")

    @staticmethod
    def _run_digest(run_id):
        return hashlib.sha256(str(run_id).encode("utf-8")).hexdigest()

    @staticmethod
    def _materialize_assets(snapshot):
        review = snapshot.get("review", {})
        if not review.get("enabled"):
            return
        root = Path(review["assets_root"])
        root.mkdir(parents=True, exist_ok=True)
        for name, record in review.get("asset_payloads", {}).items():
            if Path(name).name != name or not isinstance(record, dict):
                raise AutoReviewError("The captured review assets are invalid.")
            content = record.get("content")
            if not isinstance(content, str) or hashlib.sha256(content.encode("utf-8")).hexdigest() != record.get("sha256"):
                raise AutoReviewError("A review asset changed during snapshot commit.")
            target = root / name
            if target.exists():
                if target.read_text(encoding="utf-8") != content:
                    raise AutoReviewError("The durable review asset folder contains different bytes for this run.")
            else:
                handle, temporary = tempfile.mkstemp(prefix=".asset-", suffix=".tmp", dir=root)
                try:
                    with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, target)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)

    @staticmethod
    def _read(path):
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _locate(self, run_id, processing_root=None):
        if processing_root is not None:
            path = self._state_path(processing_root, run_id)
            state = self._read(path)
            if state and state.get("snapshot", {}).get("run_id") == str(run_id):
                return path, state
            raise AutoReviewError("No committed automatic-review snapshot matches this processing run.")
        matches = []
        if self.registry_dir.is_dir():
            digest = hashlib.sha256(str(run_id).encode("utf-8")).hexdigest() + ".json"
            for path in self.registry_dir.glob(f"roots/*/runs/{digest}"):
                state = self._read(path)
                if state.get("snapshot", {}).get("run_id") == str(run_id):
                    matches.append((path, state))
        if len(matches) != 1:
            raise AutoReviewError("The run ID is missing or ambiguous; supply its exact processing root.")
        return matches[0]

    def _mutate(self, path, function, event):
        path = Path(path)
        with _file_lock(path.with_suffix(path.suffix + ".lock")):
            current = self._read(path)
            if not current:
                raise AutoReviewError("The automatic-review run registry is unavailable.")
            self._verify_snapshot(current.get("snapshot"))
            function(current)
            current["revision"] = int(current.get("revision", 0)) + 1
            current["updated_utc"] = _utc_now()
            current.setdefault("history", []).append({"at": current["updated_utc"], "state": event})
            _write_json_atomic(path, current)
        return self._public(current)

    @staticmethod
    def _public(state):
        snapshot = state.get("snapshot", {})
        return {
            "run_id": snapshot.get("run_id"), "processing_root": snapshot.get("processing_root"),
            "state": state.get("state"), "summary": state.get("summary", ""),
            "updated_utc": state.get("updated_utc"), "revision": state.get("revision"),
            "request_dir": state.get("request_dir"), "can_view": bool(state.get("request_dir")),
            "attention": state.get("attention"), "completion": state.get("completion"),
            "snapshot": snapshot, "processing_receipt": state.get("processing_receipt"),
            "audit_receipt": state.get("audit_receipt"), "audit_status": state.get("audit_status"),
            "runner": state.get("runner"),
        }

    def _verify_ready(self, state, review_helper=None):
        snapshot, processing, audit = state["snapshot"], state.get("processing_receipt"), state.get("audit_receipt")
        self._verify_snapshot(snapshot)
        if not snapshot["review"].get("enabled") or not snapshot.get("accuracy_audit_enabled"):
            raise AutoReviewError("Automatic review was not enabled with the Accuracy Audit at Start.")
        if not processing or processing.get("status") != "complete" or processing.get("run_id") != snapshot["run_id"]:
            raise AutoReviewError("Processing completion is missing or belongs to another run.")
        if not audit or audit.get("status") != "complete" or audit.get("run_id") != snapshot["run_id"]:
            raise AutoReviewError("The matching Accuracy Audit is not exactly complete.")
        if audit.get("processing_receipt_id") != processing.get("receipt_id"):
            raise AutoReviewError("The audit is not bound to this processing-completion receipt.")
        report = Path(audit["report_path"])
        if not report.is_file() or _file_hash(report) != audit.get("report_sha256"):
            raise AutoReviewError("The completed Accuracy Audit report changed or is unavailable.")
        if [p.casefold() for p in audit.get("worker_dirs", [])] != [p.casefold() for p in processing.get("worker_dirs", [])]:
            raise AutoReviewError("The completed audit does not cover the recorded final worker scope.")
        for name, record in snapshot["review"].get("asset_payloads", {}).items():
            target = Path(snapshot["review"]["assets_root"]) / name
            if (not target.is_file() or _file_hash(target) != record.get("sha256")):
                raise AutoReviewError("The durable review rules changed or are unavailable.")
        helper = review_helper or self._review_helper()
        roots = sorted({snapshot["processing_root"], snapshot["document_root"]}, key=str.casefold)
        with contextlib.ExitStack() as stack:
            for root in roots:
                stack.enter_context(helper.writer_lock(root))
        return True

    def _account(self, snapshot, wf):
        review = snapshot["review"]
        accounts = _supported_call(wf.discover_accounts, codex_homes=review.get("codex_homes", ()))
        account = next((item for item in accounts if str(item.id) == review["account_id"]), None)
        if not account:
            raise AutoReviewError("The captured AI account is no longer registered; no fallback account was used.")
        verified = _supported_call(wf.validate_selection, account, review["model_key"],
                                   expected_email=review["expected_email"], effort=review["effort"], role="audit-review")
        if str((verified or {}).get("email", "")).casefold() != review["expected_email"].casefold():
            raise AutoReviewError("The AI account identity differs from the captured run snapshot.")
        if str((verified or {}).get("model", review["model"])).casefold() != review["model"].casefold():
            raise AutoReviewError("The selected model differs from the captured run snapshot.")
        if str((verified or {}).get("effort", review["effort"])).casefold() != review["effort"].casefold():
            raise AutoReviewError("The selected effort differs from the captured run snapshot.")
        return account

    def _prepare_workflow(self, state, wf):
        snapshot, receipt = state["snapshot"], state["audit_receipt"]
        review = snapshot["review"]
        account = self._account(snapshot, wf)
        return _supported_call(
            wf.prepare_workflow, "audit-review", account, review["model_key"],
            audit_report=receipt["report_path"], document_root=snapshot["document_root"],
            care_home=snapshot["care_home"], source_root=review["source_root"],
            assets_root=review["assets_root"], workspace_root=review["workspace_root"],
            ledger_path=review["ledger_path"], misnaming_path=review["misnaming_path"],
            allow_document_changes=review["allow_document_changes"], allow_code_changes=False,
            completed_audit=True, preflight=True, expected_email=review["expected_email"],
            processing_root=snapshot["processing_root"], review_all_flags=review["review_all_flags"],
            effort=review["effort"],
        )

    def _prepare_queue(self, state, prepared, helper):
        request_dir = Path(prepared.request_dir)
        queue_path = request_dir / "review_queue.json"
        if queue_path.is_file():
            return helper.read_json(queue_path)
        snapshot, audit, review = state["snapshot"], state["audit_receipt"], state["snapshot"]["review"]
        function = getattr(helper, "prepare_review_queue", None) or getattr(helper, "prepare", None)
        if not function:
            raise AutoReviewError("The installed review helper cannot prepare a candidate queue.")
        queue = _supported_call(
            function, audit=audit["report_path"], care_home=snapshot["care_home"],
            documents_root=snapshot["document_root"], source_root=review["source_root"],
            output=queue_path, threshold=80.0, all_flags=review["review_all_flags"],
            processing_root=snapshot["processing_root"], run_id=snapshot["run_id"],
        )
        return queue if isinstance(queue, dict) else helper.read_json(queue_path)

    def _verify_queue_binding(self, state, queue):
        snapshot, audit, processing = state["snapshot"], state["audit_receipt"], state["processing_receipt"]
        if not isinstance(queue, dict):
            raise AutoReviewError("The review helper did not return a queue object.")
        if queue.get("audit_workbook_sha256") != audit["report_sha256"]:
            raise AutoReviewError("The review queue is not derived from this run's exact audit bytes.")
        if not _same_path(queue.get("audit_workbook", ""), audit["report_path"]):
            raise AutoReviewError("The review queue names a different audit report.")
        if not _same_path(queue.get("documents_root", ""), snapshot["document_root"]):
            raise AutoReviewError("The review queue names a different document root.")
        if not _same_path(queue.get("processing_root", ""), snapshot["processing_root"]):
            raise AutoReviewError("The review queue names a different processing root.")
        allowed = {name.casefold() for name in processing["worker_names"]}
        for candidate in queue.get("candidates", []):
            relative = candidate.get("source_relative_path")
            if not relative or not Path(relative).parts:
                raise AutoReviewError("A review candidate cannot be bound to a document in the exact completed worker scope.")
            if Path(relative).parts[0].casefold() not in allowed:
                raise AutoReviewError("The review queue contains a worker outside the exact completed audit scope.")

    @staticmethod
    def _runner_details(prepared, run, count, queue):
        process = getattr(run, "process", None)
        return {"pid": getattr(process, "pid", None), "candidate_count": count,
                "queue_run_id": queue.get("run_id"), "request_dir": str(Path(prepared.request_dir).resolve()),
                "launched_utc": _utc_now()}

    def _verify_outputs(self, state, helper):
        request = Path(state["request_dir"])
        plan = helper.read_plan(request / "apply_plan.json")
        transaction = helper.read_json(request / "REVIEW_TRANSACTION.json")
        helper.verify_transaction(plan, transaction)
        if transaction.get("status") not in ("complete", "review_only"):
            raise AutoReviewError("The document transaction is not completely reconciled.")
        self._verify_queue_binding(state, plan.get("queue", {}))
        rows = helper.review_outcomes(plan, transaction)
        if not rows or len({row.get("entry_id") for row in rows}) != len(rows):
            raise AutoReviewError("Review outcomes are empty or contain duplicate identities.")
        review = state["snapshot"]["review"]
        result = helper.sync_records(request, Path(review["ledger_path"]).parent,
                                     ledger_path=review["ledger_path"], legacy_record=review["misnaming_path"])
        journal = Path(result["journal"])
        records = helper.read_records(journal)
        present = {row.get("entry_id") for row in records if row.get("record_type") == "review_outcome"}
        expected = {row["entry_id"] for row in rows}
        if not expected.issubset(present):
            raise AutoReviewError("Not all semantic review outcomes reached the durable journal.")
        ledger = Path(result["ledger"])
        if not ledger.is_file():
            raise AutoReviewError("The reconciled master review ledger is missing.")
        decisions = helper.read_json(request / "REVIEW_DECISIONS.json")
        if decisions.get("plan_id") != plan.get("plan_id") or {r.get("entry_id") for r in decisions.get("records", [])} != expected:
            raise AutoReviewError("The final review decision receipt does not match the verified plan and journal.")
        unresolved = sum(1 for row in rows if row.get("decision") in ("Defer", "Error") or row.get("apply_error"))
        return {"completed_utc": _utc_now(), "transaction_status": transaction["status"],
                "outcome_count": len(rows), "unresolved_count": unresolved,
                "ledger_path": str(ledger.resolve()), "ledger_sha256": _file_hash(ledger),
                "journal_path": str(journal.resolve()), "journal_sha256": _file_hash(journal),
                "plan_id": plan["plan_id"], "queue_run_id": plan["queue"].get("run_id")}

    @staticmethod
    def _pid_alive(pid):
        try:
            pid = int(pid)
            if pid <= 0:
                return False
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL,
                                                  wintypes.DWORD)
                kernel32.OpenProcess.restype = wintypes.HANDLE
                kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
                kernel32.CloseHandle.restype = wintypes.BOOL
                handle = kernel32.OpenProcess(0x1000, False, pid)
                if not handle:
                    return False
                kernel32.CloseHandle(handle)
                return True
            os.kill(pid, 0)
            return True
        except (OSError, TypeError, ValueError):
            return False

    def _emit(self, event, status):
        if not self.notify:
            return
        try:
            self.notify(event, status)
        except TypeError:
            self.notify({"event": event, **status})

    @staticmethod
    def _workflows():
        import stage2_ai_workflows
        return stage2_ai_workflows

    @staticmethod
    def _review_helper():
        import ai_review
        return ai_review

    @staticmethod
    def _output_opener(workflows):
        opener = getattr(workflows, "open_live_output", None)
        if opener:
            return opener
        try:
            from stage2_live_output import open_live_output
            return open_live_output
        except ImportError:
            return None


class EngineAutoReviewBinding:
    """Small explicit bridge used by either a live run or resumed batch run."""

    def __init__(self, controller, engine, run_id, processing_root=None):
        self.controller = controller
        self.engine = engine
        self.run_id = run_id
        self.processing_root = processing_root

    def record_processing_complete(self, *, worker_dirs=None, skipped_workers=(), errors=()):
        workers = worker_dirs if worker_dirs is not None else getattr(self.engine, "_audit_worker_dirs", ())
        return self.controller.record_processing_complete(
            self.run_id, processing_root=self.processing_root, worker_dirs=workers,
            skipped_workers=skipped_workers, errors=errors,
        )

    def record_audit_receipt(self, *, report_path=None, status=None, worker_dirs=None):
        stats = getattr(self.engine, "stats", {}) or {}
        return self.controller.record_audit_receipt(
            self.run_id, processing_root=self.processing_root,
            report_path=report_path or stats.get("audit_report"),
            status=status or stats.get("audit_status"),
            worker_dirs=worker_dirs,
        )

    def record_audit_status(self, status, *, reason=None, details=None):
        return self.controller.record_audit_status(
            self.run_id, status, processing_root=self.processing_root,
            reason=reason, details=details,
        )
