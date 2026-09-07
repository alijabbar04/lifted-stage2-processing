"""Offline follow-up progress: actual method bodies, no documents/provider calls."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

from test_batch_progress_truthfulness import extract_class, StopRequested, APIError
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from stage2_progress import PhaseProgress


def fixture(*, skip_plan=(), skip_upload=(), post_error=0, persist_error=False, resume=False, stop_plan=False):
    events, statuses, outcomes, sent, rendered = [], [], [], [], []
    requests = {f"synthetic-{n}": {"fhash": str(n), "pages": 1} for n in range(4)}

    class State:
        data = {"version": 4, "est_finishing_gbp": 0, "est_audit_gbp": 0,
                "followup": {"phase": "prepared", "model_id": "synthetic", "requests": requests,
                             "batches": [], "est_gbp": .01}}
        saved_status = ""

        def save(self):
            self.saved_status = self.data["followup"].get("submission", {}).get("status", "")
            return not (persist_error and self.saved_status == "accepted")

        def add_batch(self, bid, count, status, phase, request_ids):
            self.data["followup"]["batches"].append({"id": bid, "request_ids": list(request_ids)})

    state = State()
    if resume:
        state.data["followup"].update(chunk_plan=[["synthetic-0", "synthetic-1"], ["synthetic-2", "synthetic-3"]],
                                     submitted_request_ids=["synthetic-0", "synthetic-1"])
    namespace = dict(globals(), tokens_cost_gbp=lambda *args, **kwargs: .01,
                     EST_OUTPUT_TOKENS_PER_DOC=10)
    Engine = extract_class("Engine", {"_activity", "_phase", "_phase_progress", "_followup_progress", "_submit_followup_batch"}, namespace)
    engine = Engine()
    engine.on_activity, engine.set_status = events.append, statuses.append
    engine.stats = {}
    engine.on_done = lambda stats, outcome: outcomes.append(outcome)
    engine.log = lambda message: None
    engine.resolution, engine.max_budget_gbp = 1, 100
    engine.BATCH_SUBMIT_MAX_REQUESTS, engine.BATCH_SUBMIT_MAX_BYTES = 2, 100000

    def build(api, vocab, cid, meta):
        phase = events[-1]["phase"]
        assert events[-1]["state"] == "rendering"
        assert "follow-up" in statuses[-1]
        assert "Downloading" not in statuses[-1]
        rendered.append((phase, cid))
        if stop_plan and phase == "followup_plan":
            raise StopRequested()
        if (phase == "followup_plan" and cid in skip_plan) or (phase == "followup_upload" and cid in skip_upload):
            return None
        return {"custom_id": cid}, 10

    def post(payload):
        assert events[-1]["phase"] == "followup_upload" and events[-1]["state"] == "submitting"
        assert state.saved_status == "submission_started"
        if len(sent) + 1 == post_error:
            raise OSError("synthetic network failure")
        sent.append(list(payload))
        return {"id": f"synthetic-batch-{len(sent)}"}

    api = SimpleNamespace(model_id="synthetic", submit_batch=post)
    engine.api, engine.escalation_api = api, None
    engine._api_for_model = lambda model: api
    engine._build_followup_request = build
    engine._serialized_request_bytes = lambda request: 20
    engine._partition_followup_request_ids = lambda items: ([list(cid for cid, size in items)[i:i+2] for i in range(0, len(items), 2)], [])
    return engine, state, events, statuses, outcomes, sent, rendered


def run(f):
    return f[0]._submit_followup_batch(f[1], list(f[1].data["followup"]["requests"]), "synthetic vocabulary", .01)


def test_two_render_passes_have_distinct_counts_and_acceptance_after_save():
    f = fixture()
    assert run(f)
    engine, state, events, statuses, outcomes, sent, rendered = f
    assert len(rendered) == 8 and len(sent) == 2
    assert [e["phase"] for e in events if e["kind"] == "phase_started"] == ["followup_plan", "followup_upload"]
    planning = [e for e in events if e.get("phase") == "followup_plan" and e["kind"] == "run_progress"]
    assert planning[-1]["completed"] == planning[-1]["prepared"] == planning[-1]["total"] == 4
    assert all(e["accepted"] == 0 for e in planning)
    assert [e["accepted"] for e in events if e.get("state") == "submitted"] == [2, 4, 4]
    assert events[-1]["completed"] == 4
    assert "Provider processing is separate" in statuses[-1]
    assert outcomes[-1].startswith("batch_followup_submitted:4|")


def test_missing_or_unrenderable_does_not_inflate_prepared_or_accepted():
    f = fixture(skip_plan={"synthetic-0"}, skip_upload={"synthetic-1"})
    assert run(f)
    events = f[2]
    planning = [e for e in events if e.get("phase") == "followup_plan" and e.get("state") == "complete"][-1]
    assert planning["completed"] == 4 and planning["prepared"] == 3
    assert events[-1]["total"] == 3
    assert events[-1]["completed"] == events[-1]["accepted"] == 2


def test_resume_counts_only_remaining_preparation_and_keeps_prior_acceptance():
    f = fixture(resume=True)
    assert run(f)
    assert all(phase == "followup_upload" for phase, cid in f[-1])
    assert {cid for phase, cid in f[-1]} == {"synthetic-2", "synthetic-3"}
    assert f[2][-1]["completed"] == f[2][-1]["total"] == 2
    assert f[2][-1]["accepted"] == 4
    assert len(f[-2]) == 1


@pytest.mark.parametrize("kwargs, accepted", [({"post_error": 1}, 0), ({"post_error": 2}, 2), ({"persist_error": True}, 0)])
def test_ambiguous_or_unpersisted_chunk_never_counts_as_accepted(kwargs, accepted):
    f = fixture(**kwargs)
    with pytest.raises((RuntimeError, OSError)):
        run(f)
    assert max(e.get("accepted", 0) for e in f[2]) == accepted
    assert f[2][-1]["state"] == "submitting"
    assert not f[4]


def test_interrupted_planning_never_claims_finished_or_upload_started():
    f = fixture(stop_plan=True)
    with pytest.raises(StopRequested):
        run(f)
    assert f[2][-1]["completed"] == 0 and f[2][-1]["state"] == "rendering"
    assert not any(e["phase"] == "followup_upload" for e in f[2])
    assert not f[-2]


def test_new_followup_events_are_ui_only_except_phase_milestones():
    f = fixture()
    run(f)
    App = extract_class("App", {"_activity_main"}, {})
    sent = []
    observer = SimpleNamespace(dashboard=SimpleNamespace(activity_event=lambda event: None),
                               _notify=lambda kind, **data: sent.append((kind, data)))
    for event in f[2]:
        App._activity_main(observer, event)
    assert [kind for kind, data in sent] == ["phase_started", "phase_started"]
    assert all(set(data) == {"phase"} for kind, data in sent)


def test_progress_caption_and_operation_clock_distinguish_prepared_from_accepted():
    now = [1]
    p = PhaseProgress(clock=lambda: now[0])
    p.observe({"phase": "followup_plan", "state": "complete", "completed": 4, "total": 4, "prepared": 3})
    assert "4 of 4 candidate checks finished" in p.caption()
    assert "3 requests sized, not submitted" in p.caption()
    p.observe({"phase": "followup_upload", "state": "rendering", "completed": 2, "total": 3, "accepted": 0, "operation": "built:2"})
    assert p.prepared == 0
    now[0] = 601
    p.observe({"phase": "followup_upload", "state": "submitting", "completed": 2, "total": 3, "accepted": 0, "operation": "submit:1"})
    assert p.wait_seconds() == 0
    assert "2 of 3 remaining requests prepared · 0 accepted overall" == p.caption()
    now[0] = 661
    p.observe({"phase": "followup_upload", "state": "submitting", "operation": "submit:1"})
    assert p.wait_seconds() == 60


def test_absent_or_broken_status_observer_does_not_change_submission():
    for broken in (False, True):
        f = fixture()
        if broken:
            f[0].set_status = lambda message: (_ for _ in ()).throw(RuntimeError("synthetic observer failure"))
        else:
            del f[0].set_status
        # Remove the fixture's assertion that the optional status callback ran.
        f[0]._build_followup_request = lambda api, vocab, cid, meta: ({"custom_id": cid}, 10)
        assert run(f)
        assert f[2][-1]["accepted"] == 4
