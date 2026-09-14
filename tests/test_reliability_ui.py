"""Offline UI contracts for recovery. No real credentials, files or provider."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from _load_app import load_app
from stage2_progress import PhaseProgress

app = load_app()


def test_final_checks_offer_assessment_before_result_download():
    pending = {"model_id": "frozen-model", "workers": {
        "worker": {"finishing_status": "deferred", "completed": False}}}
    ui = SimpleNamespace(care_home_dir=Path("offline"),
        _batch_busy_guard=Mock(return_value=False), cfg={"model": app.DEFAULT_MODEL},
        _primary_recovery_needed=Mock(return_value=False),
        _make_engine=Mock(), _check_finishing_recovery=Mock())
    with patch.object(app, "has_pending_batch", return_value=pending), \
         patch.object(app, "get_api_key", return_value="offline-fixture"):
        app.App._batch_check_status(ui)
    ui._check_finishing_recovery.assert_called_once_with()
    ui.engine.run_batch_apply.assert_not_called()


def test_source_only_state_opens_without_credentials_or_engine():
    for phase in ("primary_exceptions_only", "primary_nothing_renderable"):
        pending = {"phase": phase, "requests": {}, "batches": [], "workers": {}}
        ui = SimpleNamespace(care_home_dir=Path("offline"),
            _batch_busy_guard=Mock(return_value=False), _done_batch=Mock(),
            _make_engine=Mock())
        with patch.object(app, "has_pending_batch", return_value=pending), \
             patch.object(app, "BatchState", return_value=SimpleNamespace(data=pending)), \
             patch.object(app, "get_api_key") as credentials:
            app.App._batch_check_status(ui)
        credentials.assert_not_called()
        ui._make_engine.assert_not_called()
        assert ui._done_batch.call_args.args[1].startswith("batch_source_attention:")


def test_changed_or_ambiguous_checks_never_offer_paid_retry():
    for field in ("changed", "blocked"):
        ui = SimpleNamespace(set_status=Mock(), after=Mock())
        info = {"operations": 2, "token": "retained-token", field: 1,
                "workers": [{"name": "Example", "families": ["Passport"]}]}
        with patch.object(app.messagebox, "askyesno") as ask, \
             patch.object(app.messagebox, "showwarning") as warning:
            app.App._done_batch(ui, {}, "batch_apply_attention:" + app.json.dumps(info))
        ask.assert_not_called()
        warning.assert_called_once()
        ui.after.assert_not_called()


def test_valid_retry_still_requires_explicit_confirmation():
    ui = SimpleNamespace(set_status=Mock(), after=Mock())
    info = {"operations": 2, "token": "retained-token", "estimated_extra_gbp": 0.12,
            "workers": [{"name": "Example", "families": ["Passport"]}]}
    with patch.object(app.messagebox, "askyesno", return_value=False) as ask:
        app.App._done_batch(ui, {}, "batch_apply_attention:" + app.json.dumps(info))
    assert "£0.12" in ask.call_args.args[1]
    assert "new live request" in ask.call_args.args[1]
    ui.after.assert_not_called()


def test_assessment_failure_unlocks_controls_without_submitting():
    ui = SimpleNamespace(worker_thread=None,
        _refresh_folder_state=Mock(return_value=({}, {})),
        _refresh_run_controls=Mock(), set_status=Mock(), _done_batch=Mock())
    with patch.object(app.messagebox, "showwarning") as warning:
        app.App._finishing_recovery_checked(ui, {"error": "Input could not be read"})
    assert not ui._recovery_busy
    warning.assert_called_once()
    ui._done_batch.assert_not_called()


def test_download_progress_is_not_workers_or_submissions():
    progress = PhaseProgress(clock=lambda: 20)
    progress.observe({"phase": "downloading_results", "state": "downloading",
                      "completed": 3, "total": 12})
    assert "3 of 12 result batches verified" in progress.caption()
    assert "not submitting documents" in progress.caption()
    assert "worker" not in progress.caption()
    progress.observe({"phase": "downloading_results", "state": "retrying"})
    assert progress.completed == 3
    assert progress.eta_seconds() is None


def test_source_attention_is_specific_but_not_success_or_audit():
    from stage2_recovery_ui import source_attention_message
    summary = {"completed_workers": 2, "empty_workers": ["Example Empty"],
        "unreadable_documents": [{"worker": "Example Source", "path": "C:/fixture/bad.pdf",
                                  "reason": "unreadable_source"}],
        "other_incomplete_workers": []}
    message = source_attention_message(summary)
    assert "2 worker folder(s) completed" in message
    assert "NO DOCUMENTS SUPPLIED" in message
    assert "bad.pdf - Unreadable document" in message
    assert "not processed workers or an accuracy pass" in message
    ui = SimpleNamespace(set_status=Mock())
    with patch.object(app.messagebox, "showwarning") as warning, \
         patch.object(app.messagebox, "showerror") as error:
        app.App._done_batch(ui, {}, "batch_source_attention:" + app.json.dumps(summary))
    warning.assert_called_once()
    error.assert_not_called()


def test_retry_facts_reset_between_downloads_and_do_not_inflate_progress():
    clock = [20]
    progress = PhaseProgress(clock=lambda: clock[0])
    progress.observe({"phase": "downloading_results", "state": "downloading",
                      "completed": 3, "total": 12})
    clock[0] = 23
    progress.observe({"phase": "downloading_results", "state": "retrying",
                      "retry": 1, "retry_delay_seconds": 1.5})
    assert (progress.retry, progress.retry_delay_seconds, progress.completed) == (1, 1.5, 3)
    clock[0] = 25
    progress.observe({"phase": "downloading_results", "state": "retrying",
                      "retry": 2, "retry_delay_seconds": 3})
    assert progress.wait_seconds() == 0
    assert progress.eta_seconds() is None
    progress.observe({"phase": "downloading_results", "state": "downloading",
                      "completed": 4, "total": 12})
    assert (progress.retry, progress.retry_delay_seconds) == (0, 0)


def test_source_notification_is_aggregate_only():
    from stage2_notifications import format_event
    message = format_event("source_attention", workers=2, empty=1, unreadable=1, other=0,
                           path="C:/private", worker="Example Private")
    assert "2 worker folder(s) complete" in message
    assert "1 empty folder(s)" in message
    assert "Example Private" not in message and "C:/private" not in message
