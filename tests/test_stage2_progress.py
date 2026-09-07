import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from stage2_progress import PhaseProgress


def test_failed_requests_are_not_presented_as_successful_checks():
    progress = PhaseProgress(clock=lambda: 100)
    progress.observe({"phase": "audit", "state": "complete", "completed": 20, "total": 20, "errors": 3})
    assert "audit attempts finished" in progress.caption()
    assert "3 check failures" in progress.caption()
    assert "documents checked" not in progress.caption()


def test_audit_notification_discloses_failed_check_attempts():
    from stage2_notifications import format_event
    text = format_event("audit_complete", completed=20, total=20, errors=3, needs_review=4)
    assert "3 failed check(s)" in text
    assert "20 document(s) checked" not in text


def test_audit_resets_worker_progress_and_wait_does_not_advance():
    progress = PhaseProgress(clock=lambda:100)
    progress.observe({"phase":"processing", "state":"running", "completed":5, "total":5})
    progress.observe({"phase":"audit", "state":"started", "completed":0, "total":600})
    assert progress.completed == 0
    progress.observe({"phase":"audit", "state":"checking", "completed":384})
    progress.observe({"phase":"audit", "state":"adjudicating", "completed":384})
    assert progress.percent == 64
    progress.observe({"phase":"audit", "state":"stopped"})
    assert progress.completed == 384 and "incomplete" in progress.caption()


def test_report_save_is_not_premature_completion():
    progress = PhaseProgress(clock=lambda:100)
    progress.observe({"phase":"audit", "state":"started", "total":2})
    progress.observe({"phase":"audit", "state":"writing_report", "completed":2})
    assert "saving report" in progress.caption()
    assert "ready" not in progress.caption()
    progress.observe({"phase":"audit", "state":"complete", "report":"report.csv"})
    assert "report ready" in progress.caption()


def test_eta_requires_samples_and_disappears_during_long_wait():
    now = [0]
    progress = PhaseProgress(clock=lambda:now[0])
    progress.observe({"phase":"audit", "state":"started", "total":100})
    assert progress.eta_seconds() is None
    now[0] = 30
    progress.observe({"phase":"audit", "state":"checking", "completed":3})
    assert progress.eta_seconds() == 970
    now[0] = 121
    assert progress.eta_seconds() is None


def test_new_run_and_empty_audit_do_not_inherit_previous_counts():
    progress = PhaseProgress(clock=lambda:100)
    progress.observe({"phase":"audit", "state":"complete", "total":100, "completed":100})
    progress.observe({"phase":"audit", "state":"started", "total":0})
    assert progress.completed == 0 and progress.percent == 0


def test_progress_resets_inactivity_but_not_phase_elapsed():
    now = [10]
    progress = PhaseProgress(clock=lambda: now[0])
    progress.observe({"phase": "preparing", "completed": 0, "total": 5})
    now[0] = 610
    assert progress.wait_seconds() == 600
    progress.observe({"phase": "preparing", "completed": 1, "total": 5})
    assert progress.wait_seconds() == 0
    assert progress.started == 10
    now[0] = 710
    progress.observe({"phase": "preparing", "completed": 1, "total": 5})
    assert progress.wait_seconds() == 100


def test_each_render_or_submit_has_own_clock_with_identical_counts():
    now = [1]
    progress = PhaseProgress(clock=lambda: now[0])
    progress.observe({"phase": "batch", "state": "rendering", "operation": "render:1", "total": 5})
    now[0] = 501
    progress.observe({"phase": "batch", "state": "rendering", "operation": "render:2"})
    assert progress.wait_seconds() == 0
    now[0] = 551
    progress.observe({"phase": "batch", "state": "rendering", "operation": "render:2"})
    assert progress.wait_seconds() == 50
    now[0] = 601
    progress.observe({"phase": "batch", "state": "submitting", "operation": "submit:1"})
    assert progress.wait_seconds() == 0
    assert progress.started == 1
    assert progress.eta_seconds() is None


def test_scan_caption_does_not_imply_audit_or_full_file_limited_scan():
    progress = PhaseProgress()
    progress.observe({"phase": "scanning", "state": "orienting", "total": 5, "documents": 12})
    assert "0 of 5 worker-folder scan passes finished" in progress.caption()
    assert "12 documents encountered" in progress.caption()
    progress.observe({"phase": "scanning", "state": "limited", "completed": 1})
    assert "remaining folders not fully scanned" in progress.caption()
    assert progress.percent == 20
    assert progress.wait_seconds() == 0
    assert "audit" not in progress.caption()


def test_prepared_and_accepted_are_distinct_and_terminal_wait_is_zero():
    progress = PhaseProgress()
    progress.observe({"phase": "batch", "state": "submitting", "total": 20,
                      "completed": 10, "accepted": 5})
    assert progress.caption() == "10 of 20 requests prepared · 5 accepted by provider"
    progress.observe({"phase": "batch", "state": "submitted", "accepted": 10})
    assert progress.wait_seconds() == 0
    progress.observe({"phase": "scanning", "state": "scanning", "total": 0})
    assert progress.accepted == 0
    assert "Choose a care-home" not in progress.caption()


def test_preparation_caption_does_not_guarantee_conversion_success():
    progress = PhaseProgress()
    progress.observe({"phase": "preparing", "state": "complete", "total": 5, "completed": 5})
    assert "preparation passes finished" in progress.caption()
    assert "converted" not in progress.caption()
    assert progress.wait_seconds() == 0


def test_zero_renderable_batch_is_terminal_attention_not_provider_wait():
    progress = PhaseProgress(clock=lambda: 100)
    progress.observe({"phase": "batch", "state": "attention", "total": 5,
                      "completed": 0, "accepted": 0, "operation": "no-renderable"})
    assert progress.caption() == "0 of 5 requests prepared · 0 accepted by provider"
    assert progress.wait_seconds() == 0
    assert progress.finished == 100
