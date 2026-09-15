"""Synthetic end-to-end recovery across actual Engine submit/apply boundaries."""
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from test_source_recovery import pdf, render
from test_release_blockers_v131 import app, make_engine, SubmitAPI
import stage2_source_recovery as recovery


class Provider(SubmitAPI):
    def __init__(self):
        super().__init__()
        self.chunks = {}
        self.submit_batch.side_effect = self.submit
        self.get_batch = Mock(side_effect=self.status)
        self.list_batches = Mock(return_value={"data": [], "has_more": False})

    def submit(self, chunk):
        bid = "synthetic-" + str(len(self.chunks))
        self.chunks[bid] = [r["custom_id"] for r in chunk]
        return {"id": bid, "processing_status": "in_progress"}

    def status(self, bid):
        return {"id": bid, "processing_status": "ended", "results_url": bid,
                "request_counts": {"succeeded": len(self.chunks[bid])}}

    def batch_results(self, bid):
        for cid in self.chunks[bid]:
            yield {"custom_id": cid, "result": {"type": "succeeded", "message": {
                "usage": {"input_tokens": 12, "output_tokens": 4},
                "content": [{"type": "text", "text": json.dumps({"match": True,
                    "name": "Passport", "group": "Crucial", "confidence": 99})}]}}}

    @staticmethod
    def _json_from(raw):
        return json.loads(raw)


def engine_for(root, api, events):
    engine = make_engine(root, api, lambda stats, status: events.append((dict(stats), status)))
    engine.max_workers = 0
    engine.max_files = 0
    engine._batch_classification_view = render
    engine._finish_worker = Mock(return_value={"deferred": [], "organised": True})
    engine._record_roster_handover = Mock()
    engine._maybe_fix_rotation = Mock(return_value=None)
    return engine


def controller(root):
    return recovery.Recovery(app.BatchState(root), "test", "integration")


def submit_selected(c, api, ids, partial=False):
    return recovery.submit_ready(c, ids, c.token(ids), api, "Passport", allow_partial=partial)


def test_engine_98_locked_sources_never_reach_provider(tmp_path):
    tmp_path = tmp_path / "Synthetic care home"
    for i in range(98):
        pdf(tmp_path / f"Worker {i % 40:02}" / f"locked-{i}.pdf", "private-password", text=f"Synthetic {i}")
    api, events = Provider(), []
    engine_for(tmp_path, api, events).run_batch_submit()
    c = controller(tmp_path)
    assert c.summary()["locked"] == 98
    assert c.data["preflight_complete"] is True
    assert len(c.state.data["submitted_worker_scope"]) == 40
    api.submit_batch.assert_not_called()
    api.classify.assert_not_called()
    assert events[-1][1].startswith("batch_source_attention:")


def test_two_sessions_apply_accepted_then_unlock_remaining_exactly_once(tmp_path):
    tmp_path = tmp_path / "Synthetic care home"
    pdf(tmp_path / "Ready worker" / "one.pdf", text="Synthetic ready")
    pdf(tmp_path / "Mixed worker" / "two.pdf", text="Synthetic mixed ready")
    pdf(tmp_path / "Mixed worker" / "three.pdf", "later-password", text="Synthetic later")
    api, events = Provider(), []
    engine_for(tmp_path, api, events).run_batch_submit()
    c = controller(tmp_path)
    ready = [cid for cid, r in c.data["records"].items() if r["state"] == "ready"]
    with pytest.raises(recovery.RecoveryError, match="Waiting"):
        submit_selected(c, api, ready)
    submit_selected(c, api, ready, partial=True)
    engine_for(tmp_path, api, events).run_batch_apply()
    c = controller(tmp_path)
    complete = {k: json.dumps(w, sort_keys=True) for k, w in c.state.data["workers"].items() if w.get("completed")}
    assert len(complete) == 1
    locked = [cid for cid, r in c.data["records"].items() if r["state"] in recovery.LOCKED]
    c.unlock(locked, "later-password", c.token(locked), render)
    c = controller(tmp_path)
    submit_selected(c, api, locked)
    engine_for(tmp_path, api, events).run_batch_apply()
    assert not app.BatchState(tmp_path).exists(), events[-1]
    receipts = sorted(tmp_path.glob("*.terminal-*.bak"))
    assert receipts
    receipt = json.loads(receipts[-1].read_text(encoding="utf-8"))
    assert receipt["terminal_outcome"] == "completed"
    assert all(w["completed"] for w in receipt["workers"].values())
    assert all(json.dumps(receipt["workers"][k], sort_keys=True) == value for k, value in complete.items())
    ids = [cid for chunk in api.chunks.values() for cid in chunk]
    assert len(ids) == len(set(ids)) == 3
    assert receipt["costs"]["primary_actual_gbp"] > 0


def test_exclusion_only_scope_finishes_truthfully_without_any_provider_request(tmp_path):
    tmp_path = tmp_path / "Synthetic care home"
    path = pdf(tmp_path / "Excluded worker" / "locked.pdf", "later", text="Synthetic excluded")
    api, events = Provider(), []
    engine_for(tmp_path, api, events).run_batch_submit()
    c = controller(tmp_path)
    ids = list(c.data["records"])
    c.quarantine(ids, c.token(ids))
    engine = engine_for(tmp_path, api, events)
    engine._record_review_processing = Mock()
    engine.run_batch_apply()
    assert not app.BatchState(tmp_path).exists(), events[-1]
    receipt = json.loads(next(tmp_path.glob("*.terminal-*.bak")).read_text(encoding="utf-8"))
    assert receipt["terminal_outcome"] == "completed_with_exclusions"
    assert receipt["source_exclusions"]["count"] == 1
    assert receipt["automatic_review"]["status"] == "blocked_partial_scope"
    assert not engine._audit_worker_dirs
    assert "deliberately excluded and not processed" in Path(receipt["source_exclusions_report"]).read_text()
    assert events[-1][0]["terminal_outcome"] == "completed_with_exclusions"
    assert not path.exists()
    api.submit_batch.assert_not_called()


@pytest.mark.parametrize("raw", ['{"check_date": 1, "work_permitted": true}',
    '{"check_date":"2026-09-15","work_permitted":"true"}',
    '{"check_date":"20260915","work_permitted":true}',
    '{"check_date":"2026-02-30","work_permitted":true}',
    '{"check_date":"","work_permitted":true,"extra":1}', 'prose {"check_date":"","work_permitted":true}'])
def test_sharecode_strict_schema_and_no_coercion(raw):
    api = app.ClaudeAPI("offline", "claude-haiku-4-5")
    api._post = Mock(return_value=raw)
    with pytest.raises(ValueError, match="malformed share-code"):
        api.share_code_check([], "Synthetic evidence")
    kwargs = api._post.call_args.kwargs
    assert kwargs["single_attempt"] is True
    assert kwargs["output_schema"]["additionalProperties"] is False


def test_sharecode_network_uncertainty_is_not_retried():
    api = app.ClaudeAPI("offline", "claude-haiku-4-5")
    with patch.object(app.urllib.request, "urlopen", side_effect=TimeoutError("sensitive document text")) as request:
        with pytest.raises(app.FinishingAmbiguous) as error:
            api.share_code_check([], "Synthetic evidence")
    assert request.call_count == 1
    assert "sensitive" not in str(error.value)


def test_uncertain_supplement_requires_positive_match_and_never_reposts(tmp_path):
    tmp_path = tmp_path / "Synthetic care home"
    pdf(tmp_path / "Worker" / "one.pdf")
    api, events = Provider(), []
    engine = engine_for(tmp_path, api, events)
    engine.run_batch_submit()
    c = controller(tmp_path)
    api.submit_batch.side_effect = TimeoutError("synthetic")
    ids = list(c.data["records"])
    with pytest.raises(recovery.RecoveryError):
        submit_selected(c, api, ids)
    state = app.BatchState(tmp_path)
    with pytest.raises(recovery.RecoveryError, match="positive provider match"):
        engine.reconcile_source_submission(state)
    assert api.submit_batch.call_count == 1
    assert app.BatchState(tmp_path).data["primary_submission"]["status"] == "ambiguous"
