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
