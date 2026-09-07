"""Offline UI state tests: no Tk window, worker documents or API calls."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from _load_app import load_app


app = load_app()


def controls():
    return SimpleNamespace(
        care_home_dir=Path("C:/offline-fixture"),
        pick_btn=Mock(), start_btn=Mock(), flatten_btn=Mock(),
        batch_btn=Mock(), stop_btn=Mock(), _recovery_busy=False,
    )


class TestPrimaryRecoveryUI(unittest.TestCase):
    def test_uncertain_and_incomplete_primary_route_to_recovery(self):
        for marker in ("submission_started", "ambiguous"):
            with self.subTest(marker=marker):
                self.assertTrue(app.App._primary_recovery_needed({
                    "primary_submission": {"status": marker}}))
        self.assertTrue(app.App._primary_recovery_needed({
            "requests": {"first": {}, "second": {}},
            "batches": [{"n": 1}]}))
        self.assertFalse(app.App._primary_recovery_needed({
            "requests": {"first": {}}, "batches": [{"n": 1}]}))
        self.assertTrue(app.App._primary_recovery_needed({
            "primary_submission_complete": False,
            "requests": {"first": {}}, "batches": [{"n": 1}]}))
        self.assertTrue(app.App._primary_recovery_needed({
            "primary_submission_complete": False, "primary_inventory": [{"path": "planned"}]}))
        self.assertFalse(app.App._primary_recovery_needed({
            "requests": {"first": {}},
            "followup": {"phase": "pending"}}))

    def test_pending_batch_blocks_start_and_flatten_but_allows_check(self):
        ui = controls()
        app.App._refresh_run_controls(ui, pending={"batches": [{"id": "saved"}]})
        ui.start_btn.configure.assert_called_once_with(state="disabled")
        ui.flatten_btn.configure.assert_called_once_with(state="disabled")
        ui.batch_btn.configure.assert_called_once_with(state="normal")
        ui.pick_btn.configure.assert_called_once_with(state="normal")

    def test_cleared_pending_state_reenables_normal_actions(self):
        ui = controls()
        app.App._refresh_run_controls(ui, pending={})
        ui.start_btn.configure.assert_called_once_with(state="normal")
        ui.flatten_btn.configure.assert_called_once_with(state="normal")
        ui.batch_btn.configure.assert_called_once_with(state="disabled")

    def test_recovery_check_disables_conflicting_actions(self):
        ui = controls()
        ui._recovery_busy = True
        app.App._refresh_run_controls(ui, pending={"batches": [{"id": "saved"}]})
        for button in (ui.start_btn, ui.flatten_btn, ui.pick_btn, ui.batch_btn):
            button.configure.assert_called_once_with(state="disabled")

    def test_header_reflects_followup_and_cleared_state_after_operation(self):
        with tempfile.TemporaryDirectory() as folder:
            ui = SimpleNamespace(
                care_home_dir=Path(folder), folder_lbl=Mock(), cfg={}, move_dest=None,
                _primary_recovery_needed=app.App._primary_recovery_needed)
            pending = {"requests": {"a": {}, "b": {}}, "batches": [{"n": 2}],
                       "followup": {"phase": "pending", "requests": {"b": {}},
                                    "batches": [{"id": "followup"}]}}
            with patch.object(app, "has_pending_batch", side_effect=[pending, {}]), \
                    patch.object(app, "read_live_checkpoint", return_value={}):
                app.App._refresh_folder_state(ui)
                self.assertIn("PENDING FOLLOW-UP BATCH: 1 document(s) in 1 batch(es)",
                              ui.folder_lbl.configure.call_args.kwargs["text"])
                app.App._refresh_folder_state(ui)
                self.assertNotIn("PENDING", ui.folder_lbl.configure.call_args.kwargs["text"])

    def test_primary_recovery_header_does_not_claim_followup(self):
        with tempfile.TemporaryDirectory() as folder:
            ui = SimpleNamespace(
                care_home_dir=Path(folder), folder_lbl=Mock(), cfg={}, move_dest=None,
                _primary_recovery_needed=app.App._primary_recovery_needed)
            with patch.object(app, "has_pending_batch", return_value={
                    "primary_submission": {"status": "ambiguous"}}), \
                    patch.object(app, "read_live_checkpoint", return_value={}):
                app.App._refresh_folder_state(ui)
            self.assertIn("PRIMARY SUBMISSION NEEDS RECOVERY",
                          ui.folder_lbl.configure.call_args.kwargs["text"])

    def test_failed_stopped_and_pending_outcomes_do_not_force_100_percent(self):
        for status in ("stopped", "failure", "batch_primary_ambiguous",
                       "batch_recovery_blocked:offline failure",
                       "batch_busy:Another writer is active",
                       "batch_pending:{}", "batch_submitted:1|1|1|1"):
            with self.subTest(status=status):
                ui = SimpleNamespace(
                    worker_thread=None, _update_stats=Mock(),
                    _begin_automatic_review=Mock(return_value=False),
                    _refresh_folder_state=Mock(return_value=({}, {})),
                    _refresh_run_controls=Mock(), set_progress=Mock(),
                    _done_batch=Mock(), set_status=Mock(),
                    cost_var=Mock(), _est_at_start=None)
                with patch.object(app.messagebox, "showinfo"), \
                        patch.object(app.messagebox, "showwarning"):
                    app.App._done_main(ui, {}, status)
                ui.set_progress.assert_not_called()

    def test_confirmed_completion_can_fill_progress(self):
        ui = SimpleNamespace(
            worker_thread=None, _update_stats=Mock(),
            _begin_automatic_review=Mock(return_value=False),
            _refresh_folder_state=Mock(return_value=({}, {})),
            _refresh_run_controls=Mock(), set_progress=Mock(), _done_batch=Mock())
        app.App._done_main(ui, {}, "batch_applied:1|1")
        ui.set_progress.assert_called_once_with(1, 1)

    def test_completion_waits_for_worker_exit_before_unlocking_actions(self):
        ui = SimpleNamespace(worker_thread=Mock(), after=Mock(),
                             _done_main=Mock(), _update_stats=Mock())
        ui.worker_thread.is_alive.return_value = True
        app.App._done_main(ui, {}, "batch_primary_ambiguous")
        ui._update_stats.assert_not_called()
        ui.after.assert_called_once_with(25, ui._done_main, {}, "batch_primary_ambiguous")

    def test_declining_recovery_plan_never_submits(self):
        ui = SimpleNamespace(
            worker_thread=None, _refresh_folder_state=Mock(return_value=({}, {})),
            _refresh_run_controls=Mock(), set_status=Mock())
        engine = Mock()
        with patch.object(app.messagebox, "askyesno", return_value=False) as confirm:
            app.App._primary_recovery_checked(ui, engine, {
                "status": "needs_authorization", "remaining": 3,
                "estimated_remaining_gbp": 0.25, "message": "Three missing requests."})
        engine.recover_primary_submission.assert_not_called()
        self.assertIn("£0.25", confirm.call_args.args[1])
        self.assertIn("3 requests", confirm.call_args.args[1])

    def test_unverified_recovery_has_no_submit_confirmation(self):
        ui = SimpleNamespace(
            worker_thread=None, _refresh_folder_state=Mock(return_value=({}, {})),
            _refresh_run_controls=Mock(), set_status=Mock())
        with patch.object(app.messagebox, "askyesno") as confirm, \
                patch.object(app.messagebox, "showwarning") as warning:
            app.App._primary_recovery_checked(ui, Mock(), {
                "status": "blocked", "message": "Provider records unavailable."})
        confirm.assert_not_called()
        self.assertIn("Provider records unavailable", warning.call_args.args[1])

    def test_approved_recovery_runs_verified_resume_on_background_worker(self):
        ui = SimpleNamespace(
            worker_thread=None, _refresh_folder_state=Mock(return_value=({}, {})),
            _refresh_run_controls=Mock(), set_status=Mock(),
            _batch_busy_guard=Mock(return_value=False), _poll_stats=Mock())
        engine = Mock()
        started = []

        class OfflineThread:
            def __init__(self, *, target, daemon):
                self.target = target

            def start(self):
                started.append(True)
                self.target()

        with patch.object(app.messagebox, "askyesno", return_value=True), \
                patch.object(app.threading, "Thread", OfflineThread):
            app.App._primary_recovery_checked(ui, engine, {
                "status": "needs_authorization", "remaining": 3,
                "message": "Three missing requests."})
        self.assertEqual(started, [True])
        engine.recover_primary_submission.assert_called_once_with(allow_resubmit=True)
        self.assertTrue(ui._recovery_busy)

    def test_recovery_has_busy_guard(self):
        ui = SimpleNamespace(worker_thread=None, _scanning=False, _recovery_busy=True)
        with patch.object(app.messagebox, "showwarning"):
            self.assertTrue(app.App._batch_busy_guard(ui))

    def test_primary_ambiguity_has_specific_actionable_copy(self):
        ui = SimpleNamespace(set_status=Mock())
        with patch.object(app.messagebox, "showwarning") as warning, \
                patch.object(app.messagebox, "showerror") as error:
            app.App._done_batch(ui, {}, "batch_primary_ambiguous")
        error.assert_not_called()
        self.assertEqual(warning.call_args.args[0], "Primary batch submission needs recovery")
        self.assertIn("Check batch status", warning.call_args.args[1])

    def test_writer_collision_has_specific_retry_guidance(self):
        ui = SimpleNamespace(set_status=Mock())
        with patch.object(app.messagebox, "showwarning") as warning, \
                patch.object(app.messagebox, "showerror") as error, \
                patch.object(app.messagebox, "askyesno") as confirm:
            app.App._done_batch(ui, {}, "batch_busy:Another writer is active")
        error.assert_not_called()
        confirm.assert_not_called()
        self.assertEqual(warning.call_args.args[0], "Batch folder is already in use")
        self.assertIn("Another writer is active", warning.call_args.args[1])
        self.assertIn("Check batch status", warning.call_args.args[1])
        self.assertIn("Leave the state and lock files in place", warning.call_args.args[1])

    def test_start_cannot_bypass_pending_guard(self):
        ui = SimpleNamespace(
            care_home_dir=Path("C:/offline-fixture"), _batch_busy_guard=Mock(return_value=False),
            _refresh_folder_state=Mock(), _refresh_run_controls=Mock(), _make_engine=Mock())
        with patch.object(app, "has_pending_batch", return_value={"batches": ["saved"]}), \
                patch.object(app.messagebox, "showwarning"):
            app.App._start(ui)
        ui._make_engine.assert_not_called()

    def test_flatten_cannot_bypass_pending_guard(self):
        ui = SimpleNamespace(
            care_home_dir=Path("C:/offline-fixture"), _batch_busy_guard=Mock(return_value=False))
        with patch.object(app, "has_pending_batch", return_value={"batches": ["saved"]}), \
                patch.object(app.messagebox, "showwarning"), \
                patch.object(app.filedialog, "askdirectory") as choose:
            app.App._flatten_only(ui)
        choose.assert_not_called()


if __name__ == "__main__":
    unittest.main()
