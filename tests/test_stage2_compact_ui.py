"""Invisible, isolated Tk dashboard geometry and App-contract regression tests."""
import base64
import gc
import io
from pathlib import Path
import sys
import tkinter as tk
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from stage2_compact_ui import CompactDashboard


class TestCompactDashboard(unittest.TestCase):
    def setUp(self):
        gc.collect()
        collect_enabled = gc.isenabled()
        gc.disable()
        try:
            self.app = tk.Tk()
            self.app.withdraw()
            self.app.overrideredirect(True)
            self.app.attributes("-alpha", 0.0)
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Tk display unavailable: {exc}")
        finally:
            if collect_enabled:
                gc.enable()
        app = self.app
        app.cfg = {}
        app.worker_thread = None
        app._scanning = app._recovery_busy = False
        app.care_home_dir = None
        app.move_dest = None
        for name in ("_open_jobs", "_open_reports", "_open_api_analytics", "_open_tools", "_open_settings",
                     "_pick_folder", "_start", "_batch_check_status", "_stop", "_open_ai_workflow", "_flatten_only", "_toggle_console"):
            setattr(app, name, Mock())
        from PIL import Image
        png = io.BytesIO()
        Image.new("RGB", (96, 96), "black").save(png, format="PNG")
        self.ns = {"APP_ICON_B64": base64.b64encode(png.getvalue()), "save_config": Mock(),
                   "worker_dirs_in": Mock(return_value=[1, 2, 3, 4, 5]),
                   "open_stage_guide": Mock(), "style_titlebar_black": Mock()}
        self.dashboard = CompactDashboard(app, self.ns)
        self.size(1180, 760)

    def tearDown(self):
        self.app.destroy()
        self.dashboard = self.app = None
        gc.collect()

    def size(self, width, height):
        self.app.geometry(f"{width}x{height}+0+0")
        self.app.deiconify()
        self.app.update()

    def assert_visible_fitted(self, widget):
        app = self.app
        self.assertTrue(widget.winfo_viewable(), str(widget))
        left, top = widget.winfo_rootx() - app.winfo_rootx(), widget.winfo_rooty() - app.winfo_rooty()
        self.assertGreaterEqual(left, 0, str(widget))
        self.assertGreaterEqual(top, 0, str(widget))
        self.assertLessEqual(left + widget.winfo_width(), app.winfo_width(), str(widget))
        self.assertLessEqual(top + widget.winfo_height(), app.winfo_height(), str(widget))
        self.assertGreaterEqual(widget.winfo_width(), widget.winfo_reqwidth(), str(widget) + " horizontal clipping")
        self.assertGreaterEqual(widget.winfo_height(), widget.winfo_reqheight(), str(widget) + " vertical clipping")

    def test_app_widget_contract_and_navigation_actions_retained(self):
        for name in ("banner", "pick_btn", "progress", "status_lbl", "preview_name", "preview_canvas", "log_text", "info_vars",
                     "folder_lbl", "flatten_btn", "start_btn", "batch_btn", "stop_btn", "cost_var", "token_var"):
            self.assertTrue(hasattr(self.app, name), name)
        self.assertEqual(set(self.dashboard.nav_buttons), {"Jobs", "Reports", "API Usage", "Tools", "Guide", "Settings"})
        self.dashboard.nav_buttons["API Usage"].invoke()
        self.app._open_api_analytics.assert_called_once()
        self.dashboard.nav_buttons["Guide"].invoke()
        self.ns["open_stage_guide"].assert_called_once_with(self.app)
        self.assertIsInstance(self.dashboard._brand_icon, tk.PhotoImage)

    def test_essential_buttons_fit_normal_and_minimum_sizes(self):
        for width, height in ((1180, 760), (1000, 640)):
            for preview in (False, True):
                with self.subTest(size=(width, height), preview=preview):
                    self.size(width, height)
                    self.dashboard.set_preview_visible(preview)
                    self.app.update_idletasks()
                    for widget in [*self.dashboard.nav_buttons.values(), self.app.pick_btn,
                                   self.app.start_btn, self.app.batch_btn, self.app.stop_btn,
                                   self.dashboard.review_btn, self.dashboard.learning_btn, self.dashboard.preview_btn]:
                        self.assert_visible_fitted(widget)
                    self.assertGreater(self.dashboard.activity.winfo_height(), 70)

    def test_long_care_home_and_audit_status_do_not_hide_controls(self):
        self.app.care_home_dir = Path("C:/Test") / ("A long care-home name with a regional office " * 3 + " [Files]")
        self.dashboard.refresh_context()
        self.dashboard.activity_event({"phase": "audit", "state": "adjudicating", "completed": 384,
            "total": 600, "needs_review": 7, "path": "C:/Test/" + "A long document filename " * 8 + ".pdf"})
        self.size(1000, 640)
        self.app.update_idletasks()
        self.assert_visible_fitted(self.app.pick_btn)
        self.assert_visible_fitted(self.dashboard.review_btn)
        self.assert_visible_fitted(self.dashboard.learning_btn)
        self.assert_visible_fitted(self.dashboard.preview_btn)

    def test_audit_progress_is_real_work_and_wait_not_fake_completion(self):
        self.dashboard.activity_event({"phase": "audit", "state": "started", "total": 600, "completed": 0})
        self.dashboard.activity_event({"phase": "audit", "state": "adjudicating", "total": 600, "completed": 384, "needs_review": 7})
        self.assertEqual(float(self.app.progress["value"]), 384)
        self.assertEqual(float(self.app.progress["maximum"]), 600)
        self.assertIn("64%", self.dashboard.progress_label["text"])
        self.assertIn("Second opinion", self.dashboard.wait_label["text"])
        self.dashboard.activity_event({"phase": "audit", "state": "stopped", "total": 600, "completed": 384})
        self.assertEqual(float(self.app.progress["value"]), 384)
        self.assertIn("incomplete", self.dashboard.progress_label["text"])

    def test_recent_activity_is_bounded(self):
        for index in range(200):
            self.dashboard.add_activity("Test event " + str(index))
        self.assertEqual(len(self.dashboard.events), 150)
        self.assertEqual(len(self.dashboard.activity.get_children()), 6)

    def test_stats_refresh_does_not_hide_unresolved_or_error_review_rows(self):
        self.dashboard.activity_event({"phase": "audit", "state": "document_done", "total": 600,
            "completed": 384, "needs_review": 11, "errors": 4})
        self.dashboard.update_stats({"workers": 5, "audit_flagged": 7})
        self.assertEqual(self.dashboard.review_var.get(), "11")

    def test_provider_wait_and_stop_are_not_labelled_as_failure_or_completion(self):
        for status in ("batch_submitted:100|2", "batch_followup_submitted:50|1", "batch_pending"):
            self.dashboard.finish({}, status)
            self.assertEqual(self.dashboard.state_label.cget("text"), "Waiting for provider")
        self.dashboard.finish({}, "stopped")
        self.assertEqual(self.dashboard.state_label.cget("text"), "Stopped")
        self.dashboard.finish({}, "batch_applied")
        self.assertEqual(self.dashboard.state_label.cget("text"), "Complete")

    def test_structured_batch_scan_phase_survives_legacy_status_and_fits(self):
        self.dashboard.activity_event({"kind": "run_progress", "phase": "scanning",
            "state": "orienting", "completed": 4, "total": 5, "documents": 1234,
            "operation": "orientation:1234"})
        self.dashboard.status_changed("Scanning documents for the batch…")
        self.assertEqual(self.dashboard.phase_label.cget("text"), "Scanning documents · local orientation preflight")
        self.assertEqual(self.dashboard.state_label.cget("text"), "Local orientation check")
        self.dashboard.activity_event({"kind": "run_progress", "phase": "scanning",
            "state": "limited", "completed": 4, "total": 5, "documents": 1234})
        for width, height in ((1180, 760), (1000, 640)):
            for preview in (False, True):
                with self.subTest(size=(width, height), preview=preview):
                    self.size(width, height)
                    self.dashboard.set_preview_visible(preview)
                    self.app.update_idletasks()
                    for widget in (self.dashboard.progress_label, self.dashboard.review_btn,
                                   self.dashboard.learning_btn, self.app.stop_btn):
                        self.assert_visible_fitted(widget)
        self.assertEqual(float(self.app.progress["value"]), 4)
        self.assertEqual(float(self.app.progress["maximum"]), 5)

    def test_zero_renderable_batch_is_attention_not_provider_wait(self):
        self.dashboard.activity_event({"kind": "run_progress", "phase": "batch",
            "state": "attention", "completed": 0, "total": 5, "accepted": 0,
            "operation": "no-renderable"})
        self.assertEqual(self.dashboard.phase_label.cget("text"), "Batch preparation needs attention")
        self.assertEqual(self.dashboard.state_label.cget("text"), "Needs attention · nothing sent")
        self.assertIn("No provider batch/classification result was submitted or applied",
                      self.dashboard.wait_label.cget("text"))

    def test_terminal_finish_survives_queued_status_after_active_progress(self):
        active = {"kind": "run_progress", "phase": "batch", "state": "rendering",
                  "completed": 2, "total": 5, "operation": "render:2"}
        for status, expected in (
                ("stopped", "Stopped"),
                ("batch_error:synthetic", "Needs attention"),
                ("batch_ambiguous:synthetic", "Needs attention"),
                ("batch_limit:synthetic", "Needs attention")):
            with self.subTest(status=status):
                self.dashboard.activity_event(active)
                self.dashboard.finish({}, status)
                self.dashboard.status_changed("Final status after queued callback")
                self.assertEqual(self.dashboard.state_label.cget("text"), expected)
                self.assertNotIn("since last progress", self.dashboard.wait_label.cget("text"))

        self.dashboard.activity_event({"kind": "run_progress", "phase": "batch",
                                       "state": "attention", "completed": 0, "total": 5,
                                       "operation": "no-renderable"})
        self.dashboard.finish({}, "batch_no_renderable:5")
        self.dashboard.status_changed("Nothing renderable; retry after repair")
        self.assertEqual(self.dashboard.state_label.cget("text"), "Needs attention · nothing sent")
        self.assertIn("No provider batch/classification result was submitted or applied",
                      self.dashboard.wait_label.cget("text"))

        self.app.worker_thread = types.SimpleNamespace(is_alive=lambda: True)
        self.dashboard.activity_event(active)
        self.dashboard.status_changed("Rendering next request")
        self.assertEqual(self.dashboard.state_label.cget("text"), "Rendering")
        self.assertIn("since last progress", self.dashboard.wait_label.cget("text"))
        self.app.worker_thread = None
        self.dashboard.activity_event({"kind": "run_progress", "phase": "batch",
                                       "state": "submitted", "completed": 5, "total": 5,
                                       "accepted": 5})
        self.dashboard.finish({}, "batch_submitted:5|3|synthetic")
        self.dashboard.status_changed("Provider is processing the accepted batch")
        self.assertEqual(self.dashboard.state_label.cget("text"), "Accepted · processing pending")
        self.assertIn("still need provider processing", self.dashboard.wait_label.cget("text"))

        self.dashboard.activity_event(active)
        self.dashboard.finish({}, "batch_applied")
        self.dashboard.status_changed("Accepted results were applied")
        self.assertEqual(self.dashboard.state_label.cget("text"), "Phase complete")
        self.assertNotIn("since last progress", self.dashboard.wait_label.cget("text"))

        for status, expected_wait in (
                ("batch_nothing_to_submit", "Local scanning finished"),
                ("batch_none_pending", "Batch preparation finished")):
            with self.subTest(status=status):
                phase = "scanning" if status == "batch_nothing_to_submit" else "batch"
                self.dashboard.activity_event({"kind": "run_progress", "phase": phase,
                                               "state": "rendering", "completed": 2,
                                               "total": 5, "operation": "render:2"})
                self.dashboard.finish({}, status)
                self.dashboard.status_changed("Benign final status")
                self.assertEqual(self.dashboard.state_label.cget("text"), "Phase complete")
                self.assertIn(expected_wait, self.dashboard.wait_label.cget("text"))
                self.assertNotIn("provider work may still exist", self.dashboard.wait_label.cget("text"))

        for status, expected in (
                ("batch_none_pending", "No pending batch"),
                ("batch_nothing_to_submit", "Nothing to submit"),
                ("batch_pending", "Waiting for provider"),
                ("batch_submitted:5|3|synthetic", "Waiting for provider"),
                ("stopped", "Stopped"),
                ("batch_error:synthetic", "Needs attention"),
                ("batch_applied", "Complete")):
            with self.subTest(nonstructured=status):
                self.dashboard.reset()
                self.dashboard.finish({}, status)
                self.dashboard.status_changed("Benign final status")
                self.assertFalse(self.dashboard.structured_progress)
                self.assertEqual(self.dashboard.state_label.cget("text"), expected)
                self.assertEqual(self.dashboard.wait_label.cget("text"), "Benign final status")
                self.app._scanning = True
                self.dashboard.status_changed("Scanning input folders")
                self.assertEqual(self.dashboard.state_label.cget("text"), "Working")
                self.app._scanning = False

        self.dashboard.activity_event({"kind": "run_progress", "phase": "scanning",
                                       "state": "rendering", "completed": 2, "total": 5,
                                       "operation": "render:2"})
        self.dashboard.finish({}, "batch_nothing_to_submit")
        self.dashboard.status_changed("Nothing to submit")
        self.dashboard.reset()
        self.app._scanning = True
        self.dashboard.status_changed("Scanning folder (no API calls yet)")
        self.assertEqual(self.dashboard.state_label.cget("text"), "Working")
        self.app._scanning = False

    def test_followup_planning_and_upload_are_not_download_or_processing_complete(self):
        self.dashboard.activity_event({"kind": "run_progress", "phase": "followup_plan",
            "state": "rendering", "completed": 74, "total": 180, "prepared": 70, "operation": "size:75"})
        self.dashboard.status_changed("Sizing follow-up requests: 74/180 candidates checked.")
        self.assertEqual(self.dashboard.phase_label.cget("text"), "Planning stronger-model follow-up")
        self.assertIn("70 requests sized, not submitted", self.dashboard.progress_label.cget("text"))
        self.dashboard.activity_event({"kind": "run_progress", "phase": "followup_upload",
            "state": "submitting", "completed": 35, "total": 80, "accepted": 100, "operation": "submit:3"})
        self.dashboard.status_changed("Sending follow-up requests…")
        self.assertEqual(self.dashboard.phase_label.cget("text"), "Submitting stronger-model follow-up")
        self.assertIn("35 of 80 remaining requests prepared", self.dashboard.progress_label.cget("text"))
        self.assertIn("100 accepted overall", self.dashboard.progress_label.cget("text"))
        for width, height in ((1180, 760), (1000, 640)):
            for preview in (False, True):
                with self.subTest(size=(width, height), preview=preview):
                    self.size(width, height)
                    self.dashboard.set_preview_visible(preview)
                    self.app.update_idletasks()
                    for widget in (self.dashboard.progress_label, self.dashboard.phase_label,
                                   self.dashboard.review_btn, self.dashboard.learning_btn, self.app.stop_btn):
                        self.assert_visible_fitted(widget)
        self.dashboard.activity_event({"kind": "run_progress", "phase": "followup_upload",
            "state": "submitted", "completed": 35, "total": 80, "accepted": 135})
        self.assertEqual(self.dashboard.state_label.cget("text"), "Accepted · processing pending")
        self.assertIn("still need provider processing", self.dashboard.wait_label.cget("text"))


if __name__ == "__main__":
    unittest.main()
