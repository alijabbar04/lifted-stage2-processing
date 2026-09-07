"""Isolated real-method control-flow tests: no app launch, model or real documents.

AST extraction avoids importing application startup during inactive patch QA.
After integration also run the existing full engine/recovery and isolated Tk tests.
"""
import ast
import copy
from collections import defaultdict
import datetime
import hashlib
import json
import os
from pathlib import Path
import time
import traceback
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "src" / "Stage2_Processing.pyw"


class StopRequested(Exception):
    pass


class LimitReached(StopRequested):
    pass


class CreditExhausted(Exception):
    pass


class APIError(Exception):
    pass


def extract_class(name, methods, namespace):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8-sig"))
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name)
    selected = [node for node in original.body if isinstance(node, ast.FunctionDef) and node.name in methods]
    assert len(selected) == len(methods)
    for node in selected:
        node.decorator_list = []
    klass = ast.ClassDef(name=name, bases=[], keywords=[], body=selected, decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[klass], type_ignores=[]))
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace[name]


def actual_batch_state_exists(data):
    BatchState = extract_class("BatchState", {"exists"}, {})
    probe = BatchState.__new__(BatchState)
    probe.data = data
    return BatchState.exists(probe)


def actual_primary_recovery_needed(data):
    App = extract_class("App", {"_primary_recovery_needed"}, {})
    return App._primary_recovery_needed(data)


def fixture(tmp_path, *, convert=None, orientation=None, submit=None, save_accepted=True,
            save_terminal_marker=True, delete_works=True, worker_count=5,
            documents_per_worker=1):
    events, statuses, sent, saves = [], [], [], []
    state_box = {"stored": {}, "persisted": False, "deletes": 0}
    workers = [tmp_path / f"Synthetic-{index}" for index in range(worker_count)]
    documents = {}
    for worker in workers:
        worker.mkdir()
        documents[worker] = []
        for index in range(documents_per_worker):
            name = "synthetic.pdf" if documents_per_worker == 1 else f"synthetic-{index}.pdf"
            path = worker / name
            path.write_bytes(b"synthetic only")
            documents[worker].append(path)

    class BatchState:
        def __init__(self, root):
            self.path = SimpleNamespace(exists=lambda: state_box["persisted"])
            self.data = copy.deepcopy(state_box["stored"]) if state_box["persisted"] else {}
            self.batches = []

        def exists(self):
            return actual_batch_state_exists(self.data)

        def init(self, *args):
            self.data = {}

        def save(self):
            saves.append(self.data.get("primary_submission", {}).get("status"))
            if not save_accepted and saves[-1] == "accepted":
                return False
            if not save_terminal_marker and saves[-1] == "nothing_renderable":
                return False
            state_box["stored"] = copy.deepcopy(self.data)
            state_box["persisted"] = True
            return True

        def delete(self):
            state_box["deletes"] += 1
            if delete_works:
                state_box["persisted"] = False

        def add_request(self, *args):
            pass

        def add_batch(self, bid, *args, **kwargs):
            self.batches.append(bid)

        def batch_ids(self):
            return self.batches

    def conversion(worker, log, stop):
        # No folder is marked finished while its conversion pass is running.
        last = next(e for e in reversed(events) if e.get("kind") == "run_progress")
        assert last["phase"] == "preparing"
        assert last["completed"] == workers.index(worker)
        if convert:
            return convert(worker, events)
        return {"converted": 0, "failed": 0}

    def local_orientation(path):
        assert events[-1]["phase"] == "scanning"
        assert events[-1]["state"] == "orienting"
        assert any(e.get("phase") == "preparing" and e.get("completed") == worker_count
                   for e in events)
        if orientation:
            return orientation(path, events)
        return {}

    def post(chunk):
        assert events[-1]["state"] == "submitting"
        assert events[-1]["accepted"] == sum(map(len, sent))
        assert saves[-1] == "submission_started"
        if submit:
            submit(chunk)
        sent.append(list(chunk))
        return {"id": f"synthetic-batch-{len(sent)}", "processing_status": "in_progress"}

    namespace = dict(globals(), BatchState=BatchState,
        worker_dirs_in=lambda root: workers,
        PdfConverter=SimpleNamespace(convert_worker=conversion),
        flatten_worker=lambda worker, log: 0,
        list_worker_docs=lambda worker: documents[worker],
        file_hash=lambda path: hashlib.sha256(str(path).encode()).hexdigest(),
        DocRender=SimpleNamespace(page_count=lambda path: 1),
        estimate_pipeline_costs_gbp=lambda *a, **k: defaultdict(float, gbp=1))
    Engine = extract_class("Engine", {"_activity", "_phase", "_phase_progress", "run_batch_submit"}, namespace)
    engine = Engine()
    engine.dir, engine.care_home = tmp_path, "Synthetic"
    engine.max_workers = engine.max_files = engine.max_budget_gbp = 0
    engine.move_mode, engine.move_dest = False, None
    engine.convert_pdf, engine.reprocess = True, False
    engine.orientation_mode = "audit"
    engine.resolution, engine.max_file_mb = 1, 100
    engine.post_run_audit = engine.adaptive_pages = engine.bundle_split = False
    engine.stats = defaultdict(int)
    engine.log = lambda msg: None
    engine.on_activity = events.append
    engine.on_done = lambda stats, status: statuses.append(status)
    engine.set_status = engine.set_preview = engine.set_progress = lambda *args: None
    engine._check_stop = lambda: None
    engine._redact = str
    engine._batch_skip_file = lambda worker, path: None
    engine._orientation_preflight = local_orientation
    engine._batch_classification_view = lambda path: ([], "synthetic text", [0], 1, False)
    engine.manifest = SimpleNamespace(seen=lambda *args: None)
    engine.kb = SimpleNamespace(vocabulary_block=lambda: "Synthetic vocabulary")
    engine.failed_log = SimpleNamespace(record=lambda *args: None)
    engine.api = SimpleNamespace(model_id="synthetic-model", submit_batch=post,
        classify_payload=lambda *a, **k: ("synthetic system", [{"type": "text", "text": "synthetic"}], 10),
        build_batch_request=lambda cid, *args: {"custom_id": cid})
    engine.escalation_api = None
    engine.BATCH_SUBMIT_MAX_BYTES = 100000
    engine.BATCH_SUBMIT_MAX_REQUESTS = 2
    engine._state_probe = state_box
    return engine, events, statuses, sent


def test_five_preparation_passes_then_explicit_scanning_and_durable_acceptance(tmp_path):
    engine, events, statuses, sent = fixture(tmp_path)
    engine.run_batch_submit()
    assert statuses[-1].startswith("batch_submitted:5|3|")
    assert [len(chunk) for chunk in sent] == [2, 2, 1]
    prep = [e for e in events if e.get("phase") == "preparing" and e.get("kind") == "run_progress"]
    assert [e["completed"] for e in prep] == [0, 1, 2, 3, 4, 5, 5]
    assert prep[-1]["state"] == "complete"
    scan = [e for e in events if e.get("phase") == "scanning" and e.get("kind") == "run_progress"]
    assert scan[-1]["completed"] == scan[-1]["total"] == 5
    assert scan[-1]["documents"] == 5
    accepted = [e["accepted"] for e in events if e.get("state") == "submitted"]
    assert accepted == [2, 4, 5, 5]
    # No document/page events are notifications. Only three phase milestones.
    assert [e["phase"] for e in events if e["kind"] == "phase_started"] == ["preparing", "scanning", "batch"]


def test_stop_during_conversion_never_claims_five_passes_or_starts_scan(tmp_path):
    def conversion(worker, events):
        if worker.name == "Synthetic-2":
            raise StopRequested()
        return {"converted": 0, "failed": 0}
    engine, events, statuses, sent = fixture(tmp_path, convert=conversion)
    engine.run_batch_submit()
    assert statuses == ["stopped"]
    assert max(e.get("completed", 0) for e in events) == 2
    assert not any(e["phase"] == "scanning" for e in events)
    assert sent == []


def test_conversion_failures_are_retained_without_claiming_converted_documents(tmp_path):
    engine, events, statuses, sent = fixture(tmp_path, convert=lambda *args: {"converted": 0, "failed": 1})
    engine.run_batch_submit()
    assert engine.stats["convert_failed"] == 5
    assert engine.stats["converted"] == 0
    assert all("converted" not in e for e in events)


def test_file_limit_credits_a_completed_worker_but_remains_limited_for_later_workers(tmp_path):
    engine, events, statuses, sent = fixture(tmp_path)
    engine.max_files = 1
    engine.run_batch_submit()
    scan = [e for e in events if e.get("phase") == "scanning" and e.get("kind") == "run_progress"]
    assert scan[-1]["state"] == "limited"
    assert scan[-1]["completed"] == 1 and scan[-1]["total"] == 5
    assert scan[-1]["documents"] == 1
    assert statuses[-1].startswith("batch_submitted:1|1|")


def test_file_limit_inside_worker_does_not_credit_partial_worker(tmp_path):
    engine, events, statuses, sent = fixture(tmp_path, worker_count=2,
                                              documents_per_worker=2)
    engine.max_files = 1
    engine.run_batch_submit()
    scan = [e for e in events if e.get("phase") == "scanning" and e.get("kind") == "run_progress"]
    assert scan[-1]["state"] == "limited"
    assert scan[-1]["completed"] == 0 and scan[-1]["total"] == 2
    assert scan[-1]["documents"] == 1
    assert statuses[-1].startswith("batch_submitted:1|1|")


def test_file_limit_at_last_eligible_document_completes_final_worker(tmp_path):
    engine, events, statuses, sent = fixture(tmp_path, worker_count=2,
                                              documents_per_worker=2)
    engine.max_files = 4
    engine.run_batch_submit()
    scan = [e for e in events if e.get("phase") == "scanning" and e.get("kind") == "run_progress"]
    assert scan[-1]["state"] == "complete"
    assert scan[-1]["completed"] == scan[-1]["total"] == 2
    assert scan[-1]["documents"] == 4
    assert statuses[-1].startswith("batch_submitted:4|2|")


def test_stop_during_orientation_does_not_complete_scan_or_submit(tmp_path):
    def stopped(*args):
        raise StopRequested()
    engine, events, statuses, sent = fixture(tmp_path, orientation=stopped)
    engine.run_batch_submit()
    assert events[-1]["phase"] == "scanning" and events[-1]["state"] == "orienting"
    assert events[-1]["completed"] == 0
    assert statuses == ["stopped"] and sent == []


@pytest.mark.parametrize("save_accepted, post_error", [(False, False), (True, True)])
def test_ambiguous_or_unpersisted_submission_never_claims_accepted(tmp_path, save_accepted, post_error):
    def submit(chunk):
        if post_error:
            raise OSError("synthetic connection failure")
    engine, events, statuses, sent = fixture(tmp_path, submit=submit, save_accepted=save_accepted)
    engine.run_batch_submit()
    assert not any(e.get("accepted", 0) > 0 for e in events)
    assert not any(e.get("state") == "submitted" for e in events)
    assert not statuses[-1].startswith("batch_submitted:")


def test_outbound_dispatch_ignores_all_per_document_progress(tmp_path):
    engine, events, statuses, sent = fixture(tmp_path)
    engine.run_batch_submit()
    App = extract_class("App", {"_activity_main"}, {})
    outbound = []
    observer = SimpleNamespace(dashboard=SimpleNamespace(activity_event=lambda event: None),
                               _notify=lambda kind, **data: outbound.append((kind, data)))
    for event in events:
        App._activity_main(observer, event)
    assert [kind for kind, data in outbound] == ["phase_started"] * 3
    assert all(set(data) == {"phase"} for kind, data in outbound)


def test_zero_renderable_outcome_notifies_attention_not_provider_wait():
    App = extract_class("App", {"_notify_done"}, {})
    outbound = []
    observer = SimpleNamespace(_notify=lambda kind, **data: outbound.append((kind, data)))
    App._notify_done(observer, {}, "batch_no_renderable:5")
    assert outbound == [("blocked", {"reason": "general"})]


def test_benign_no_work_results_do_not_emit_blocked_notification():
    App = extract_class("App", {"_notify_done"}, {})
    for status in ("batch_none_pending", "batch_nothing_to_submit"):
        outbound = []
        observer = SimpleNamespace(_notify=lambda kind, **data: outbound.append((kind, data)))
        App._notify_done(observer, {}, status)
        assert outbound == []


def test_start_resets_dashboard_before_preflight_statuses():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8-sig"))
    app_node = next(node for node in tree.body
                    if isinstance(node, ast.ClassDef) and node.name == "App")
    start = next(node for node in app_node.body
                 if isinstance(node, ast.FunctionDef) and node.name == "_start")
    reset_lines = [node.lineno for node in ast.walk(start)
                   if isinstance(node, ast.Call)
                   and isinstance(node.func, ast.Attribute)
                   and isinstance(node.func.value, ast.Attribute)
                   and node.func.value.attr == "dashboard"
                   and node.func.attr == "reset"]
    scan_lines = [node.lineno for node in ast.walk(start)
                  if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Attribute)
                          and target.attr == "_scanning" for target in node.targets)]
    assert reset_lines and scan_lines and min(reset_lines) < min(scan_lines)


def test_skip_completed_destination_still_finishes_preparation_and_scan_passes(tmp_path):
    engine, events, statuses, sent = fixture(tmp_path)
    engine.move_dest = tmp_path / "Processed"
    engine.move_dest.mkdir()
    (engine.move_dest / "Synthetic-4").mkdir()
    engine.move_mode = True
    engine.run_batch_submit()
    assert engine.stats["skipped_done"] == 1
    assert statuses[-1].startswith("batch_submitted:4|2|")
    scan = [e for e in events if e.get("phase") == "scanning" and e.get("kind") == "run_progress"]
    assert scan[-1]["completed"] == 5 and scan[-1]["documents"] == 4


def test_unrenderable_document_does_not_count_as_prepared_or_accepted(tmp_path):
    engine, events, statuses, sent = fixture(tmp_path)
    engine._batch_classification_view = lambda path: (
        ([], "", [0], 1, False) if path.parent.name == "Synthetic-4"
        else ([], "synthetic text", [0], 1, False))
    engine.run_batch_submit()
    assert statuses[-1].startswith("batch_submitted:4|2|")
    assert events[-1]["completed"] == 4 and events[-1]["total"] == 5
    assert events[-1]["accepted"] == 4


def test_all_unrenderable_documents_need_attention_without_pending_or_submission(tmp_path):
    engine, events, statuses, sent = fixture(tmp_path)
    engine._batch_classification_view = lambda path: ([], "", [0], 1, False)
    engine.run_batch_submit()
    assert statuses == ["batch_no_renderable:5"]
    assert sent == []
    assert engine._state_probe["deletes"] == 1
    assert not engine._state_probe["persisted"]
    assert engine._state_probe["stored"]["phase"] == "primary_nothing_renderable"
    assert engine._state_probe["stored"]["primary_submission"]["status"] == "nothing_renderable"
    assert "primary_submission_complete" not in engine._state_probe["stored"]
    assert events[-1].get("state") == "attention"
    assert events[-1].get("accepted") == 0
    assert not any(event.get("state") == "submitted" for event in events)


def test_all_missing_documents_need_attention_and_can_retry(tmp_path):
    engine, events, statuses, sent = fixture(tmp_path)
    original_progress = engine._phase_progress
    removed = False

    def remove_sources_before_render(phase, done, total, **details):
        nonlocal removed
        if phase == "batch" and details.get("state") == "rendering" and not removed:
            removed = True
            for worker in tmp_path.glob("Synthetic-*"):
                (worker / "synthetic.pdf").unlink()
        return original_progress(phase, done, total, **details)

    engine._phase_progress = remove_sources_before_render
    engine.run_batch_submit()
    assert statuses == ["batch_no_renderable:5"]
    assert sent == [] and engine._state_probe["deletes"] == 1
    for worker in tmp_path.glob("Synthetic-*"):
        (worker / "synthetic.pdf").write_bytes(b"restored synthetic only")
    engine._phase_progress = original_progress
    engine.run_batch_submit()
    assert statuses[-1].startswith("batch_submitted:5|3|")
    assert [len(chunk) for chunk in sent] == [2, 2, 1]


def test_retained_nothing_renderable_marker_is_nonpending_and_retryable(tmp_path):
    engine, events, statuses, sent = fixture(tmp_path, delete_works=False)
    engine._batch_classification_view = lambda path: ([], "", [0], 1, False)
    engine.run_batch_submit()
    retained = engine._state_probe["stored"]
    assert statuses == ["batch_no_renderable:5"]
    assert engine._state_probe["persisted"] and engine._state_probe["deletes"] == 1
    assert retained["primary_submission"]["status"] == "nothing_renderable"
    assert not actual_batch_state_exists(retained)
    assert not actual_primary_recovery_needed(retained)
    engine._batch_classification_view = lambda path: ([], "synthetic text", [0], 1, False)
    engine.run_batch_submit()
    assert statuses[-1].startswith("batch_submitted:5|3|")
    assert [len(chunk) for chunk in sent] == [2, 2, 1]


def test_terminal_marker_save_failure_never_emits_success_or_provider_wait(tmp_path):
    engine, events, statuses, sent = fixture(tmp_path, save_terminal_marker=False)
    engine._batch_classification_view = lambda path: ([], "", [0], 1, False)
    engine.run_batch_submit()
    retained = engine._state_probe["stored"]
    assert statuses == ["batch_state_write_failed"]
    assert sent == []
    assert retained["primary_submission_complete"] is False
    assert actual_batch_state_exists(retained)
    assert actual_primary_recovery_needed(retained)
    assert not any(event.get("state") in ("submitted", "attention") for event in events)
    App = extract_class("App", {"_notify_done"}, {})
    outbound = []
    App._notify_done(SimpleNamespace(_notify=lambda kind, **data: outbound.append((kind, data))),
                     {}, statuses[0])
    assert outbound == [("blocked", {"reason": "general"})]


def test_mixed_renderable_and_unrenderable_documents_still_submit_actual_chunk(tmp_path):
    engine, events, statuses, sent = fixture(tmp_path)
    engine._batch_classification_view = lambda path: (
        ([], "", [0], 1, False) if path.parent.name in ("Synthetic-1", "Synthetic-3")
        else ([], "synthetic text", [0], 1, False))
    engine.run_batch_submit()
    assert statuses[-1].startswith("batch_submitted:3|2|")
    assert [len(chunk) for chunk in sent] == [2, 1]
    assert events[-1]["state"] == "submitted"
    assert events[-1]["accepted"] == 3


def test_zero_eligible_documents_finishes_scan_without_batch_submission(tmp_path):
    engine, events, statuses, sent = fixture(tmp_path)
    engine._batch_skip_file = lambda *args: "synthetic skip"
    engine.run_batch_submit()
    assert events[-1]["phase"] == "scanning" and events[-1]["completed"] == 5
    assert events[-1]["documents"] == 5
    assert statuses == ["batch_nothing_to_submit"]
    assert not sent
