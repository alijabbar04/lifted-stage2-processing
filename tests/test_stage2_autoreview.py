import contextlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from stage2_autoreview import AutoReviewError, Stage2AutoReviewController


class FakeAccount:
    id = "codex-personal"
    provider = "codex"
    email = "person@example.test"


class FakeProcess:
    pid = 424242

    def __init__(self, result=None):
        self.result = result

    def poll(self):
        return self.result


class FakeRun:
    def __init__(self, result=None):
        self.process = FakeProcess(result)

    def poll(self):
        return self.process.poll()


class FakeWorkflows:
    RULE_FILES = ()

    def __init__(self, request_root, helper, *, candidate_count=1,
                 start_error=None, valid_outputs=False):
        self.request_root = Path(request_root)
        self.helper = helper
        self.candidate_count = candidate_count
        self.start_error = start_error
        self.valid_outputs = valid_outputs
        self.launches = 0
        self.views = 0
        self.last_run = None

    def discover_accounts(self, codex_homes=()):
        return [FakeAccount()]

    def model_choice(self, key, effort=None, role=None):
        assert (key, effort, role) == ("sol", "high", "audit-review")
        return {"provider": "codex", "id": "gpt-test-sol", "effort": effort}

    def validate_selection(self, account, key, expected_email=None,
                           effort=None, role=None):
        if expected_email and expected_email.casefold() != account.email.casefold():
            raise AutoReviewError("identity mismatch")
        return {"provider": "codex", "email": account.email,
                "model": "gpt-test-sol", "effort": effort, "status": "verified"}

    def prepare_workflow(self, role, account, key, **kwargs):
        request = self.request_root / f"request-{len(list(self.request_root.glob('request-*'))) + 1}"
        request.mkdir(parents=True)
        prepared = SimpleNamespace(request_dir=request, account=account, model_key=key)
        prepared.kwargs = kwargs
        return prepared

    def launch_headless(self, prepared, authorized_unattended=False):
        assert authorized_unattended
        self.launches += 1
        if self.start_error:
            raise RuntimeError(self.start_error)
        if self.valid_outputs:
            self.helper.make_valid_outputs(prepared.request_dir)
        self.last_run = FakeRun()
        return self.last_run

    def open_live_output(self, request_dir, title=None):
        self.views += 1
        return {"request_dir": str(request_dir), "title": title}


class FakeHelper:
    def __init__(self, candidate_count=1):
        self.candidate_count = candidate_count

    @contextlib.contextmanager
    def writer_lock(self, root):
        yield

    @staticmethod
    def read_json(path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def prepare(self, audit, care_home, documents_root, source_root, output,
                threshold=80, all_flags=False, processing_root=None, run_id=None):
        audit = Path(audit).resolve()
        root = Path(documents_root).resolve()
        processing = Path(processing_root).resolve()
        digest = __import__("hashlib").sha256(audit.read_bytes()).hexdigest()
        candidates = [{"candidate_id": f"candidate-{i}",
                       "source_relative_path": "Worker One/document.pdf"}
                      for i in range(self.candidate_count)]
        queue = {"run_id": run_id or "queue-run", "care_home": care_home,
                 "audit_workbook": str(audit), "audit_workbook_sha256": digest,
                 "documents_root": str(root), "processing_root": str(processing),
                 "candidates": candidates, "inventory": []}
        Path(output).write_text(json.dumps(queue), encoding="utf-8")
        return queue

    @staticmethod
    def make_valid_outputs(request_dir):
        request = Path(request_dir)
        queue = json.loads((request / "review_queue.json").read_text(encoding="utf-8"))
        rows = [{"record_type": "review_outcome", "entry_id": item["candidate_id"],
                 "decision": "Keep", "apply_error": ""}
                for item in queue["candidates"]]
        plan = {"plan_id": "plan-1", "queue": queue, "rows": rows,
                "decisions": {"reviewer": "person@example.test", "decisions": []},
                "operations": []}
        (request / "apply_plan.json").write_text(json.dumps(plan), encoding="utf-8")
        (request / "REVIEW_TRANSACTION.json").write_text(
            json.dumps({"plan_id": "plan-1", "status": "review_only", "operations": []}),
            encoding="utf-8")

    @staticmethod
    def read_plan(path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    @staticmethod
    def verify_transaction(plan, transaction):
        if plan["plan_id"] != transaction.get("plan_id"):
            raise AutoReviewError("transaction mismatch")

    @staticmethod
    def review_outcomes(plan, transaction):
        return plan["rows"]

    @staticmethod
    def sync_records(request_dir, ledger_root, ledger_path=None, legacy_record=None):
        request = Path(request_dir)
        plan = json.loads((request / "apply_plan.json").read_text(encoding="utf-8"))
        ledger = Path(ledger_path)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_bytes(b"valid-ledger")
        journal = ledger.parent / "review_records.jsonl"
        journal.write_text("".join(json.dumps(row) + "\n" for row in plan["rows"]), encoding="utf-8")
        (request / "REVIEW_DECISIONS.json").write_text(
            json.dumps({"plan_id": plan["plan_id"], "records": plan["rows"]}), encoding="utf-8")
        return {"ledger": str(ledger), "journal": str(journal), "records": len(plan["rows"])}

    @staticmethod
    def read_records(path):
        return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture
def setup(tmp_path):
    processing = tmp_path / "Files"
    documents = tmp_path / "Processed"
    worker = documents / "Worker One"
    source = tmp_path / "source"
    assets = tmp_path / "assets"
    workspace = tmp_path / "workspace"
    for folder in (processing, worker, source / "src", assets, workspace):
        folder.mkdir(parents=True, exist_ok=True)
    (source / "src" / "ai_review.py").write_text("", encoding="utf-8")
    (source / "src" / "Stage2_Processing.pyw").write_text("", encoding="utf-8")
    audit = documents / "Filename_Audit_Report.xlsx"
    audit.write_bytes(b"exact complete report")
    settings = {"enabled": True, "model_key": "sol", "effort": "high",
                "account_id": "codex-personal", "expected_email": "person@example.test",
                "allow_document_changes": True, "review_all_flags": False,
                "source_root": str(source), "assets_root": str(assets),
                "workspace_root": str(workspace),
                "ledger_path": str(tmp_path / "records" / "ledger.xlsx"),
                "misnaming_path": str(tmp_path / "records" / "misnames.xlsx"),
                "codex_homes": [{"path": str(tmp_path / "codex"), "token": "must-not-persist"}]}
    helper = FakeHelper()
    workflows = FakeWorkflows(tmp_path / "requests", helper)
    controller = Stage2AutoReviewController(tmp_path / "registry")
    snapshot = controller.capture_snapshot(
        run_id="run-001", processing_root=processing, document_root=documents,
        care_home="Example Home", worker_names=["Worker One"], settings=settings,
        accuracy_audit_enabled=True, workflows=workflows)
    return SimpleNamespace(controller=controller, snapshot=snapshot, processing=processing,
                           documents=documents, worker=worker, audit=audit, settings=settings,
                           helper=helper, workflows=workflows, tmp_path=tmp_path)


def arm(item):
    item.controller.commit_run(item.snapshot)
    item.controller.record_processing_complete("run-001", processing_root=item.processing,
                                               worker_dirs=[item.worker])
    item.controller.record_audit_receipt("run-001", processing_root=item.processing,
                                        report_path=item.audit, status="complete")


def test_snapshot_is_immutable_and_does_not_persist_secrets(setup):
    setup.controller.commit_run(setup.snapshot)
    setup.settings["effort"] = "low"
    status = setup.controller.get_status("run-001", processing_root=setup.processing)
    assert status["snapshot"]["review"]["effort"] == "high"
    assert status["snapshot"]["review"]["expected_email"] == "person@example.test"
    assert "token" not in status["snapshot"]["review"]["codex_homes"][0]
    changed = dict(setup.snapshot)
    changed["care_home"] = "Tampered"
    with pytest.raises(AutoReviewError, match="changed after preflight"):
        setup.controller.commit_run(changed)


def test_live_and_batch_binding_share_one_durable_state(setup):
    setup.controller.commit_run(setup.snapshot)
    engine = SimpleNamespace(_audit_worker_dirs=[setup.worker],
                             stats={"audit_status": "complete", "audit_report": str(setup.audit)})
    binding = setup.controller.bind_engine(engine, "run-001", processing_root=setup.processing)
    assert binding.record_processing_complete()["state"] == "waiting-audit"
    assert binding.record_audit_receipt()["state"] == "ready"
    # Duplicate batch/live callbacks are idempotent and cannot create a second receipt.
    assert binding.record_processing_complete()["state"] == "ready"
    assert binding.record_audit_receipt()["state"] == "ready"


def test_all_failed_workers_cannot_commit_a_partial_complete_receipt(setup):
    setup.controller.commit_run(setup.snapshot)
    with pytest.raises(AutoReviewError, match="1 missing"):
        setup.controller.record_processing_complete(
            "run-001", processing_root=setup.processing, worker_dirs=[],
            errors=["Worker One failed before completion"])
    result = setup.controller.get_status("run-001", processing_root=setup.processing)
    assert result["processing_receipt"] is None
    assert result["state"] == "waiting-processing"
    # Retrying the same run after its worker succeeds can still create the
    # original complete receipt; no partial immutable receipt blocks recovery.
    recovered = setup.controller.record_processing_complete(
        "run-001", processing_root=setup.processing, worker_dirs=[setup.worker])
    assert recovered["state"] == "waiting-audit"


def test_document_errors_do_not_block_complete_worker_scope(setup):
    setup.controller.commit_run(setup.snapshot)
    result = setup.controller.record_processing_complete(
        "run-001", processing_root=setup.processing, worker_dirs=[setup.worker],
        errors=["One document needs the audit to resolve classification"])
    assert result["processing_receipt"]["errors"]
    assert result["processing_receipt"]["unaudited_scope_worker_names"] == []
    assert result["state"] == "waiting-audit"


def test_five_worker_scope_rejects_four_and_accepts_reordered_recovered_five(setup):
    workers = [setup.worker]
    for number in range(2, 6):
        worker = setup.documents / f"Worker {number}"
        worker.mkdir()
        workers.append(worker)
    snapshot = setup.controller.capture_snapshot(
        run_id="run-five", processing_root=setup.processing,
        document_root=setup.documents, care_home="Example Home",
        worker_names=[worker.name for worker in workers], settings=setup.settings,
        accuracy_audit_enabled=True, workflows=setup.workflows)
    setup.controller.commit_run(snapshot)
    with pytest.raises(AutoReviewError, match="1 missing, 0 unexpected"):
        setup.controller.record_processing_complete(
            "run-five", processing_root=setup.processing, worker_dirs=workers[1:])
    recovered = setup.controller.record_processing_complete(
        "run-five", processing_root=setup.processing, worker_dirs=list(reversed(workers)))
    assert recovered["processing_receipt"]["unaudited_scope_worker_names"] == []
    assert len(recovered["processing_receipt"]["worker_dirs"]) == 5


def test_unexpected_worker_cannot_replace_or_expand_start_scope(setup):
    setup.controller.commit_run(setup.snapshot)
    other = setup.documents / "Other Worker"
    other.mkdir()
    for workers in ([other], [setup.worker, other]):
        with pytest.raises(AutoReviewError, match="unexpected"):
            setup.controller.validate_processing_scope(
                "run-001", processing_root=setup.processing, worker_dirs=workers)
    assert setup.controller.get_status(
        "run-001", processing_root=setup.processing)["processing_receipt"] is None


def test_legacy_partial_receipt_cannot_arm_or_launch_review(setup):
    arm(setup)
    state_path = setup.controller._state_path(setup.processing, "run-001")
    # Simulate a v1.5.0 partial receipt on disk; keep it immutable, do not
    # silently upgrade it into a full completion on reopening.
    def make_partial(state):
        state["processing_receipt"]["worker_dirs"] = []
        state["processing_receipt"]["worker_names"] = []
        state["audit_receipt"]["worker_dirs"] = []
    setup.controller._mutate(state_path, make_partial, "legacy-partial-fixture")
    with pytest.raises(AutoReviewError, match="1 missing"):
        setup.controller.record_audit_receipt(
            "run-001", processing_root=setup.processing,
            report_path=setup.audit, status="complete")
    result = setup.controller.maybe_launch(
        "run-001", processing_root=setup.processing,
        workflows=setup.workflows, review_helper=setup.helper)
    assert result["state"] == "needs-attention"
    assert setup.workflows.launches == 0
    assert result["processing_receipt"]["worker_dirs"] == []


def test_final_scope_must_still_exist_before_launch(setup):
    arm(setup)
    setup.worker.rmdir()
    result = setup.controller.maybe_launch(
        "run-001", processing_root=setup.processing,
        workflows=setup.workflows, review_helper=setup.helper)
    assert result["state"] == "needs-attention"
    assert setup.workflows.launches == 0


@pytest.mark.parametrize("status", ["pending", "failed", "skipped", "disabled"])
def test_noncomplete_audit_never_arms(setup, status):
    setup.controller.commit_run(setup.snapshot)
    setup.controller.record_processing_complete("run-001", processing_root=setup.processing,
                                               worker_dirs=[setup.worker])
    result = setup.controller.record_audit_status("run-001", status,
                                                  processing_root=setup.processing,
                                                  reason="offline test")
    assert result["state"] != "ready"
    assert result["audit_receipt"] is None
    assert setup.controller.maybe_launch("run-001", processing_root=setup.processing,
                                         workflows=setup.workflows,
                                         review_helper=setup.helper)["state"] != "running"
    assert setup.workflows.launches == 0


def test_retried_audit_can_arm_only_after_exact_complete_receipt(setup):
    setup.controller.commit_run(setup.snapshot)
    setup.controller.record_processing_complete("run-001", processing_root=setup.processing,
                                               worker_dirs=[setup.worker])
    setup.controller.record_audit_status("run-001", "failed",
                                         processing_root=setup.processing,
                                         reason="temporary audit error")
    result = setup.controller.record_audit_receipt(
        "run-001", processing_root=setup.processing,
        report_path=setup.audit, status="complete")
    assert result["state"] == "ready"
    assert result["attention"] is None


def test_commit_materializes_ephemeral_rule_assets(setup):
    setup.workflows.RULE_FILES = ("REVIEW_RULES.md",)
    source_asset = Path(setup.settings["assets_root"]) / "REVIEW_RULES.md"
    source_asset.write_text("captured rules", encoding="utf-8")
    snapshot = setup.controller.capture_snapshot(
        run_id="run-assets", processing_root=setup.processing,
        document_root=setup.documents, care_home="Example Home",
        worker_names=["Worker One"], settings=setup.settings,
        accuracy_audit_enabled=True, workflows=setup.workflows)
    setup.controller.commit_run(snapshot)
    source_asset.unlink()
    durable = Path(snapshot["review"]["assets_root"]) / "REVIEW_RULES.md"
    assert durable.read_text(encoding="utf-8") == "captured rules"


def test_empty_queue_does_not_launch_paid_job(setup):
    setup.helper.candidate_count = 0
    arm(setup)
    result = setup.controller.maybe_launch("run-001", processing_root=setup.processing,
                                           workflows=setup.workflows,
                                           review_helper=setup.helper)
    assert result["state"] == "no-candidates"
    assert setup.workflows.launches == 0
    assert "no paid" in result["summary"]


def test_double_signal_launches_once_and_opens_existing_view(setup):
    arm(setup)
    first = setup.controller.maybe_launch("run-001", processing_root=setup.processing,
                                          workflows=setup.workflows,
                                          review_helper=setup.helper)
    second = setup.controller.maybe_launch("run-001", processing_root=setup.processing,
                                           workflows=setup.workflows,
                                           review_helper=setup.helper)
    assert first["state"] == second["state"] == "running"
    assert setup.workflows.launches == 1
    setup.controller.view_existing("run-001", processing_root=setup.processing,
                                   workflows=setup.workflows)
    assert setup.workflows.views == 2


def test_restart_reconciles_outputs_without_resubmitting(setup):
    setup.workflows.valid_outputs = True
    arm(setup)
    setup.controller.maybe_launch("run-001", processing_root=setup.processing,
                                  workflows=setup.workflows, review_helper=setup.helper)
    setup.workflows.last_run.process.result = 0
    restarted = Stage2AutoReviewController(setup.tmp_path / "registry")
    result = restarted.reconcile("run-001", processing_root=setup.processing,
                                 workflows=setup.workflows, review_helper=setup.helper)
    assert result["state"] == "completed"
    assert result["completion"]["outcome_count"] == 1
    assert setup.workflows.launches == 1


def test_restart_recovers_spawn_before_controller_state_write(setup):
    setup.workflows.valid_outputs = True
    arm(setup)
    launched = setup.controller.maybe_launch(
        "run-001", processing_root=setup.processing,
        workflows=setup.workflows, review_helper=setup.helper)
    request = Path(launched["request_dir"])
    (request / "runner-status.json").write_text(
        json.dumps({"state": "outputs-awaiting-verification", "pid": 999999,
                    "process_exit_code": 0}), encoding="utf-8")
    state_path = setup.controller._state_path(setup.processing, "run-001")
    setup.controller._mutate(
        state_path,
        lambda state: state.update(state="preparing", runner=None),
        "simulate-crash-before-launch-record")
    restarted = Stage2AutoReviewController(setup.tmp_path / "registry")
    result = restarted.reconcile("run-001", processing_root=setup.processing,
                                 workflows=setup.workflows,
                                 review_helper=setup.helper)
    assert result["state"] == "completed"
    assert setup.workflows.launches == 1


def test_changed_audit_is_persistent_attention_not_launch(setup):
    arm(setup)
    setup.audit.write_bytes(b"changed after receipt")
    result = setup.controller.maybe_launch("run-001", processing_root=setup.processing,
                                           workflows=setup.workflows,
                                           review_helper=setup.helper)
    assert result["state"] == "needs-attention"
    assert result["request_dir"] is None
    assert setup.workflows.launches == 0


def test_start_failure_keeps_claim_and_never_retries(setup):
    setup.workflows.start_error = "provider could not start"
    arm(setup)
    first = setup.controller.maybe_launch("run-001", processing_root=setup.processing,
                                          workflows=setup.workflows,
                                          review_helper=setup.helper)
    second = setup.controller.maybe_launch("run-001", processing_root=setup.processing,
                                           workflows=setup.workflows,
                                           review_helper=setup.helper)
    assert first["state"] == second["state"] == "needs-attention"
    assert setup.workflows.launches == 1
    assert first["can_view"]


def test_process_zero_with_invalid_outputs_is_not_completed(setup):
    arm(setup)
    setup.controller.maybe_launch("run-001", processing_root=setup.processing,
                                  workflows=setup.workflows, review_helper=setup.helper)
    setup.workflows.last_run.process.result = 0
    result = setup.controller.reconcile("run-001", processing_root=setup.processing,
                                        workflows=setup.workflows,
                                        review_helper=setup.helper)
    assert result["state"] == "outputs-awaiting-verification"
    assert result["completion"] is None
    assert setup.workflows.launches == 1


def test_cancel_only_changes_queued_review(setup):
    setup.controller.commit_run(setup.snapshot)
    result = setup.controller.cancel_queued("run-001", processing_root=setup.processing)
    assert result["state"] == "cancelled"
    assert setup.controller.maybe_launch("run-001", processing_root=setup.processing,
                                         workflows=setup.workflows,
                                         review_helper=setup.helper)["state"] == "cancelled"
    assert setup.workflows.launches == 0


def test_legacy_batch_marker_without_snapshot_cannot_auto_arm(setup):
    (setup.processing / ".docreview_batch_state.json").write_text(
        json.dumps({"processing_complete": True, "audit": {"status": "complete",
                                                               "report": str(setup.audit)}}), encoding="utf-8")
    with pytest.raises(AutoReviewError, match="No committed"):
        setup.controller.get_status("legacy-run", processing_root=setup.processing)


def test_windows_pid_probe_declares_pointer_sized_handle():
    source = (Path(__file__).resolve().parents[1] / "src" / "stage2_autoreview.py").read_text(encoding="utf-8")
    assert "OpenProcess.restype = wintypes.HANDLE" in source
    assert "CloseHandle.argtypes = (wintypes.HANDLE,)" in source
