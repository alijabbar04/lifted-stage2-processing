"""Synthetic repeated recovery-session follow-up; never contacts a provider."""
import copy
import json
from unittest.mock import Mock

import pytest

from test_batch_followup_v131 import app, FakeBatchAPI, StubKB, followup_engine


@pytest.fixture
def scope(tmp_path):
    primary = FakeBatchAPI("claude-haiku-4-5")
    stronger = FakeBatchAPI(app.SECOND_OPINION_MODEL_ID)
    stronger.submit_batch.side_effect = [
        {"id": f"batch-{n}", "processing_status": "in_progress"}
        for n in range(10)]
    state = app.BatchState(tmp_path)
    state.init("Synthetic", primary.model_id, 1.0, {})
    state.data.update(est_finishing_gbp=0, est_audit_gbp=0)
    engine = followup_engine(tmp_path, primary, stronger)
    engine._build_followup_request = Mock(side_effect=lambda api, vocab, cid, meta:
                                         ({"custom_id": cid}, 100))

    def add(index):
        worker = tmp_path / f"Worker-{index}"
        worker.mkdir(exist_ok=True)
        path = worker / "synthetic.pdf"
        path.write_bytes(f"synthetic-{index}".encode())
        cid = f"primary-{index}"
        state.add_request(cid, path, worker, app.file_hash(path), 1)
        state.save()
        return cid

    return engine, state, stronger, add


def finish(state):
    state.data["followup"]["phase"] = "ended"
    state.save()


def test_three_sessions_append_without_replaying_old_request_or_losing_history(scope):
    engine, state, api, add = scope
    unresolved = []
    previous_ids = set()
    previous_batches = []
    previous_plan = []
    for index in range(3):
        unresolved.append(add(index))
        engine._build_followup_request.reset_mock()
        assert engine._submit_followup_batch(state, unresolved, "synthetic", .1)
        followup = state.data["followup"]
        new_ids = set(followup["requests"]) - previous_ids
        assert len(new_ids) == 1
        assert {c.args[2] for c in engine._build_followup_request.call_args_list} == new_ids
        assert followup["batches"][:len(previous_batches)] == previous_batches
        assert followup["chunk_plan"][:len(previous_plan)] == previous_plan
        assert set(followup["submitted_request_ids"]) == set(followup["requests"])
        assert len(api.submit_batch.call_args.args[0]) == 1
        previous_ids = set(followup["requests"])
        previous_batches = copy.deepcopy(followup["batches"])
        previous_plan = copy.deepcopy(followup["chunk_plan"])
        finish(state)
        # A fresh process sees the same whole history and doesn't buy it again.
        restored = app.BatchState(state.dir)
        assert not engine._submit_followup_batch(restored, unresolved, "synthetic", .1)
    assert api.submit_batch.call_count == 3
    assert len(state.data["followup"]["recovery_sessions"]) == 2


@pytest.mark.parametrize("marker", ["submission_started", "ambiguous"])
def test_ended_label_never_overrides_ambiguous_marker(scope, marker):
    engine, state, api, add = scope
    first = add(0)
    assert engine._submit_followup_batch(state, [first], "synthetic", .1)
    finish(state)
    state.data["followup"]["submission"]["status"] = marker
    second = add(1)
    prior = copy.deepcopy(state.data["followup"])
    assert engine._submit_followup_batch(state, [first, second], "synthetic", .1)
    assert state.data["followup"] == prior
    assert api.submit_batch.call_count == 1


def test_pending_old_scope_waits_before_appending_new(scope):
    engine, state, api, add = scope
    first = add(0)
    assert engine._submit_followup_batch(state, [first], "synthetic", .1)
    second = add(1)
    prior = copy.deepcopy(state.data["followup"])
    assert engine._submit_followup_batch(state, [first, second], "synthetic", .1)
    assert state.data["followup"] == prior
    assert api.submit_batch.call_count == 1


def test_supplement_budget_includes_prior_actual_cost_and_retains_it(scope):
    engine, state, api, add = scope
    first = add(0)
    assert engine._submit_followup_batch(state, [first], "synthetic", .1)
    finish(state)
    second = add(1)
    state.data["costs"] = {"followup_actual_gbp": 10.0}
    engine.max_budget_gbp = 5
    assert engine._submit_followup_batch(state, [first, second], "synthetic", .1)
    assert api.submit_batch.call_count == 1
    assert state.data["followup"]["est_gbp"] >= 10
    assert state.data["costs"]["followup_actual_gbp"] == 10


def test_unrenderable_new_source_keeps_prior_results_and_does_not_loop(scope):
    engine, state, api, add = scope
    first = add(0)
    assert engine._submit_followup_batch(state, [first], "synthetic", .1)
    finish(state)
    second = add(1)
    old_requests = copy.deepcopy(state.data["followup"]["requests"])
    engine._build_followup_request.side_effect = lambda *args: None
    assert not engine._submit_followup_batch(state, [first, second], "synthetic", .1)
    assert state.data["followup"]["requests"] == old_requests
    assert not engine._submit_followup_batch(state, [first, second], "synthetic", .1)
    assert api.submit_batch.call_count == 1


def test_later_followup_budget_includes_already_paid_live_finishing(scope):
    engine, state, api, add = scope
    first = add(0)
    assert engine._submit_followup_batch(state, [first], "synthetic", .1)
    finish(state)
    second = add(1)
    state.data["costs"] = {"live_actual_gbp": 10.0}
    engine.max_budget_gbp = 5
    assert engine._submit_followup_batch(state, [first, second], "synthetic", .1)
    assert api.submit_batch.call_count == 1


def test_completed_worker_cannot_acquire_a_new_followup(scope):
    engine, state, api, add = scope
    first = add(0)
    assert engine._submit_followup_batch(state, [first], "synthetic", .1)
    finish(state)
    second = add(1)
    source = state.request_for(second)["worker_dir"]
    state.data["workers"] = {source.casefold(): {"source_path": source, "completed": True}}
    prior = copy.deepcopy(state.data["workers"])
    assert not engine._submit_followup_batch(state, [first, second], "synthetic", .1)
    assert state.data["workers"] == prior
    assert api.submit_batch.call_count == 1


def test_verified_save_accepts_void_return_from_real_durable_writer(scope):
    engine, state, api, add = scope
    first = add(0)
    real_save = state.save

    def void_save():
        real_save()

    state.save = void_save
    assert engine._submit_followup_batch(state, [first], "synthetic", .1)
    assert api.submit_batch.call_count == 1
    assert app.BatchState(state.dir).data["followup"]["submission"]["status"] == "accepted"


@pytest.mark.parametrize("save_return", [None, True, False])
def test_noop_save_blocks_before_paid_request_regardless_of_return(scope, save_return):
    engine, state, api, add = scope
    first = add(0)
    state.save = Mock(return_value=save_return)
    with pytest.raises(RuntimeError, match="verification"):
        engine._submit_followup_batch(state, [first], "synthetic", .1)
    api.submit_batch.assert_not_called()


def test_failed_accepted_marker_keeps_durable_started_and_blocks_restart(scope):
    engine, state, api, add = scope
    first = add(0)
    real_save = state.save

    def fail_accepted():
        if state.data.get("followup", {}).get("submission", {}).get("status") == "accepted":
            return True  # A lying/no-op writer cannot authorize acceptance.
        return real_save()

    state.save = fail_accepted
    with pytest.raises(RuntimeError, match="verification"):
        engine._submit_followup_batch(state, [first], "synthetic", .1)
    restored = app.BatchState(state.dir)
    assert restored.data["followup"]["submission"]["status"] == "submission_started"
    assert engine._submit_followup_batch(restored, [first], "synthetic", .1)
    assert api.submit_batch.call_count == 1


def test_apply_accounts_ended_prior_followup_before_supplement_budget_gate(scope):
    initial, state, stronger, add = scope
    first = add(0)
    assert initial._submit_followup_batch(state, [first], "synthetic", .1)
    old_followup = copy.deepcopy(state.data["followup"])
    old_cid = next(iter(old_followup["requests"]))
    second = add(1)  # Recovered while the earlier follow-up was still pending.
    primary = initial.api
    state.add_batch("synthetic-primary", 2, "in_progress", request_ids=[first, second])
    state.data["costs"] = {"followup_actual_gbp": 0.0}
    state.save()

    def result(cid, input_tokens=100, meaningful=False):
        parsed = ({"match": True, "name": "Passport", "confidence": 95}
                  if meaningful else {"match": False, "other_label": "Unknown", "confidence": 20})
        return {"custom_id": cid, "result": {"type": "succeeded", "message": {
            "usage": {"input_tokens": input_tokens, "output_tokens": 100},
            "content": [{"type": "text", "text": json.dumps(parsed)}]}}}

    primary.status_by_id["synthetic-primary"] = {
        "id": "synthetic-primary", "processing_status": "ended",
        "results_url": "synthetic-primary-results", "request_counts": {"succeeded": 2}}
    primary.results_by_url["synthetic-primary-results"] = [result(first), result(second)]
    stronger.status_by_id["batch-0"] = {
        "id": "batch-0", "processing_status": "ended",
        "results_url": "synthetic-followup-results", "request_counts": {"succeeded": 1}}
    stronger.results_by_url["synthetic-followup-results"] = [
        result(old_cid, input_tokens=10_000_000, meaningful=True)]
    actual_old_cost = app.tokens_cost_gbp(stronger.model_id, 10_000_000, 100, batch=True)
    assert actual_old_cost > 5
    assert old_followup["est_gbp"] < 5
    outcomes = []
    engine = app.Engine(state.dir, StubKB(), primary, "Synthetic", log=lambda *_: None,
        set_status=lambda *_: None, set_progress=lambda *_: None,
        set_preview=lambda *_: None, ask_unknown=lambda *_: None, on_cost=lambda *_: None,
        on_done=lambda stats, status: outcomes.append(status),
        resolution=1, escalation_api=stronger, bundle_split=False,
        post_run_audit=False, max_budget_gbp=5)
    engine._build_followup_request = Mock(side_effect=lambda api, vocab, cid, meta:
                                         ({"custom_id": cid}, 100))
    engine._finish_worker = Mock()
    engine.run_batch_apply()
    assert outcomes[-1].startswith("batch_followup_over_budget:"), outcomes
    assert stronger.submit_batch.call_count == 1  # Only the old accepted request.
    engine._finish_worker.assert_not_called()
    restored = app.BatchState(state.dir)
    assert restored.data["costs"]["followup_actual_gbp"] == pytest.approx(actual_old_cost, abs=1e-6)
    assert restored.data["costs"]["primary_accounted_batch_ids"] == ["synthetic-primary"]
    assert restored.data["costs"]["followup_accounted_batch_ids"] == ["batch-0"]
    assert restored.data["followup"]["batches"] == old_followup["batches"]
    assert restored.data["followup"]["est_gbp"] >= actual_old_cost
    assert app.batch_result_cache.load(state.dir, "batch-0", [old_cid],
                                       "synthetic-followup-results") is not None
    # Another check must not buy anything, regardless of whether its earlier
    # hard-budget stop occurs before the already-available cache is needed.
    stronger.batch_results = Mock(side_effect=AssertionError("old paid results must be cached"))
    engine.run_batch_apply()
    assert stronger.submit_batch.call_count == 1
