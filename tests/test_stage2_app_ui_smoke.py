"""Instantiate the real App with isolated persistence and invisible Tk windows.

The actual UI callbacks, guide viewer and usage page run; credential lookup,
production logs, migrations and notifications cannot touch real user state.
"""
from contextlib import ExitStack
import gc
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from _load_app import load_app


class TestRealAppUISmoke(unittest.TestCase):
    def setUp(self):
        # Finalize old PhotoImages/interpreters before initializing a new Tcl
        # interpreter; collection during Tk's library load is flaky on Windows.
        gc.collect()
        self.module = load_app()
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.patches = ExitStack()
        module = self.module
        for name in ("install_console_capture", "ensure_app_dir", "migrate_legacy_processing_archive", "KnowledgeBase"):
            self.patches.enter_context(patch.object(module, name))
        self.patches.enter_context(patch.object(module, "load_config", return_value={
            "model": module.DEFAULT_MODEL, "fx": 0.79, "notifications": {}, "post_run_audit": True}))
        self.patches.enter_context(patch.object(module, "save_config", return_value=True))
        fake_notifications = Mock()
        fake_notifications.drain_statuses.return_value = []
        self.patches.enter_context(patch.object(module, "NotificationService", return_value=fake_notifications))
        self.patches.enter_context(patch.object(module.App, "_refresh_env_banner"))
        self.patches.enter_context(patch.object(module, "style_titlebar_black"))
        self.patches.enter_context(patch.object(module, "load_processing_reports", return_value=[]))
        for name in ("APP_DIR", "PROCESSING_REPORTS_ROOT", "ARCHIVED_PROCESSING_REPORTS"):
            self.patches.enter_context(patch.object(module, name, self.root / name))
        self.patches.enter_context(patch.dict("os.environ", {"LIFTED_API_USAGE_PATH": str(self.root / "usage.jsonl")}))
        if module._api_usage is not None:
            self.patches.enter_context(patch.object(module._api_usage, "tk_scale", return_value=1.0))
        self.external = self.patches.enter_context(patch.object(module.os, "startfile"))
        self.messages = self.patches.enter_context(patch.object(module.messagebox, "showerror"))
        collect_enabled = gc.isenabled()
        gc.disable()
        try:
            self.app = module.App()
        except tk.TclError as exc:
            self.patches.close()
            self.folder.cleanup()
            raise unittest.SkipTest(f"Tk display unavailable: {exc}")
        finally:
            if collect_enabled:
                gc.enable()
        self.app.withdraw()
        self.app.overrideredirect(True)
        self.app.attributes("-alpha", 0.0)
        self.callback_errors = []
        self.app.report_callback_exception = lambda exc, value, traceback: self.callback_errors.append((exc, str(value)))

    def tearDown(self):
        # Cancel callbacks on this isolated interpreter before destroying it.
        for identifier in self.app.tk.call("after", "info"):
            try:
                self.app.after_cancel(identifier)
            except tk.TclError:
                pass
        self.app.destroy()
        self.app = None
        self.patches.close()
        self.folder.cleanup()
        gc.collect()

    def pump(self, seconds=0.15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            for child in self.app.winfo_children():
                if isinstance(child, tk.Toplevel):
                    child.withdraw()
            self.app.update()
            time.sleep(0.01)

    def test_actual_app_constructor_geometry_and_normal_callbacks(self):
        for width, height in ((1180, 760), (1000, 640)):
            self.app.geometry(f"{width}x{height}+0+0")
            self.app.deiconify()
            self.pump()
            for widget in (self.app.start_btn, self.app.batch_btn, self.app.stop_btn,
                           self.app.dashboard.review_btn, self.app.dashboard.learning_btn):
                self.assertTrue(widget.winfo_viewable())
                self.assertGreaterEqual(widget.winfo_height(), widget.winfo_reqheight())
        self.app.set_status("Isolated smoke check")
        self.app.set_progress(2, 5)
        self.app.on_cost(1.25, 100)
        self.app._update_stats({"workers": 2, "errors": 0})
        self.app.set_preview(None, "No document data used")
        self.pump()
        self.assertEqual(self.app.cost_var.get(), "£1.25")
        self.assertEqual(self.app.dashboard.workers_var.get(), "2")
        self.assertFalse(self.callback_errors, self.callback_errors)
        self.external.assert_not_called()

    def test_reports_ribbon_opens_chooser_then_actual_audit_browser(self):
        self.app.dashboard.nav_buttons["Reports"].invoke()
        self.pump()
        chooser = self.app._stage2_workflow_dialogs["reports"]
        self.assertEqual(chooser.title(), "Which report would you like?")
        chooser._audits()
        self.pump()
        browsers = [window for window in self.app.winfo_children() if isinstance(window, self.module.ReportsDialog)]
        self.assertEqual(len(browsers), 1)
        self.assertEqual(browsers[0]._rows, [])
        self.assertFalse(self.callback_errors, self.callback_errors)
        self.external.assert_not_called()

    def test_guide_ribbon_constructs_and_renders_actual_bundled_viewer(self):
        pdf = self.module.bundled_resource("docs", "USER_GUIDE.pdf")
        if not pdf.is_file():
            self.skipTest("Bundled guide is not present yet")
        with patch.object(self.module, "find_guide_pdf", return_value=pdf):
            self.app.dashboard.nav_buttons["Guide"].invoke()
            self.pump(0.4)
        guides = [window for window in self.app.winfo_children() if isinstance(window, tk.Toplevel)
                  and window.title() == "Stage 2 — User guide"]
        self.assertEqual(len(guides), 1)
        labels = []
        pending = [guides[0]]
        while pending:
            widget = pending.pop()
            if isinstance(widget, tk.Label):
                labels.append(str(widget.cget("text")))
            pending.extend(widget.winfo_children())
        self.assertTrue(any(text.startswith("Page 1 /") for text in labels), labels)
        self.assertFalse(self.callback_errors, self.callback_errors)
        self.messages.assert_not_called()
        self.external.assert_not_called()

    def test_api_usage_ribbon_constructs_actual_analytics_with_isolated_ledger(self):
        if self.module._api_usage is None:
            self.skipTest("Shared API usage module not installed")
        self.app.dashboard.nav_buttons["API Usage"].invoke()
        self.pump(0.3)
        analytics = [window for window in self.app.winfo_children() if isinstance(window, tk.Toplevel)
                     and window.title().startswith("API Usage")]
        self.assertEqual(len(analytics), 1)
        self.assertFalse(self.callback_errors, self.callback_errors)
        self.messages.assert_not_called()
        self.external.assert_not_called()


if __name__ == "__main__":
    unittest.main()
