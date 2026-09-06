"""Offline Tk dialog smoke tests. Own hidden windows; no live app/AI/network."""
from pathlib import Path
import gc
import sys
import tempfile
import threading
import time
import tkinter as tk
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import stage2_workflow_ui as ui
import stage2_ai_workflows as workflows
import stage2_notifications as notifications


class TestWorkflowSelection(unittest.TestCase):
    def test_busy_guard_covers_scan_recovery_and_thread(self):
        app = types.SimpleNamespace(worker_thread=None, _scanning=False, _recovery_busy=False)
        self.assertFalse(ui.is_processing_busy(app))
        app._scanning = True
        self.assertTrue(ui.is_processing_busy(app))
        app._scanning = False
        app._recovery_busy = True
        self.assertTrue(ui.is_processing_busy(app))
        app._recovery_busy = False
        app.worker_thread = types.SimpleNamespace(is_alive=lambda: True)
        self.assertTrue(ui.is_processing_busy(app))

    def test_exact_audit_preferred_and_registry_matching_care_home(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            exact = root / "Filename_Audit_Report.csv"
            exact.touch()
            other = root / "Filename_Audit_Report-other.xlsx"
            other.touch()
            app = types.SimpleNamespace(_latest_audit_report=exact, care_home_dir=root)
            ns = {"load_processing_reports": lambda: [{"path": str(other), "care_home": "elsewhere"}]}
            self.assertEqual(ui.latest_audit_report(app, ns), exact)
            app._latest_audit_report = None
            ns["load_processing_reports"] = lambda: [{"path": str(other), "care_home": "elsewhere"}, {"path": str(exact), "care_home": root.name}]
            self.assertEqual(ui.latest_audit_report(app, ns), exact)
            self.assertFalse(hasattr(app, "_latest_audit_completed"))

    def test_blocked_open_never_creates_or_launches_review(self):
        app = types.SimpleNamespace(worker_thread=None, _scanning=True)
        with patch.object(ui.messagebox, "showinfo") as warning, patch.object(ui, "AIWorkflowDialog") as dialog:
            self.assertIsNone(ui.open_ai_workflow(app, {}, "audit-review"))
        warning.assert_called_once()
        dialog.assert_not_called()

    def test_repeated_clicks_reuse_the_same_dialog(self):
        app = types.SimpleNamespace()
        dialog = Mock()
        dialog.winfo_exists.return_value = True
        factory = Mock(return_value=dialog)
        self.assertIs(ui._open_single_dialog(app, "review", factory), dialog)
        self.assertIs(ui._open_single_dialog(app, "review", factory), dialog)
        factory.assert_called_once()
        dialog.lift.assert_called_once()

    def test_files_processed_suffix_match_does_not_select_another_home(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            intended = root / "Filename_Audit_Report-intended.csv"
            unrelated = root / "Filename_Audit_Report-other.csv"
            intended.touch()
            unrelated.touch()
            app = types.SimpleNamespace(_latest_audit_report=None, care_home_dir=root / "Example Support & Housing [Files]")
            ns = {"load_processing_reports": lambda: [
                {"path": str(unrelated), "care_home": "Another Care Home [Processed]"},
                {"path": str(intended), "care_home": "Example Support & Housing [Processed]"}]}
            self.assertEqual(ui.latest_audit_report(app, ns), intended)
            app.care_home_dir = root / "Third Care Home [Files]"
            self.assertIsNone(ui.latest_audit_report(app, ns))
            app._latest_audit_report = intended
            self.assertIsNone(ui.latest_audit_report(app, ns))


class TestHiddenTkDialogs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gc.collect()
        collect_enabled = gc.isenabled()
        gc.disable()
        try:
            cls.app = tk.Tk()
            cls.app.withdraw()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Tk display unavailable: {exc}")
        finally:
            if collect_enabled:
                gc.enable()

    @classmethod
    def tearDownClass(cls):
        cls.app.destroy()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.audit = self.root / "Filename_Audit_Report.csv"
        self.audit.write_text("name,result\n", encoding="utf-8")
        self.app.cfg = {}
        self.app.worker_thread = None
        self.app._scanning = False
        self.app._recovery_busy = False
        self.app.care_home_dir = self.root
        self.app.move_dest = self.root
        self.app._latest_audit_report = self.audit
        self.app._latest_audit_completed = False
        self.app.notification_service = Mock()
        self.ns = {"save_config": Mock(return_value=True), "APP_DIR": self.root,
                   "load_processing_reports": lambda: [], "style_titlebar_black": Mock(),
                   "bundled_resource": lambda *parts: self.root.joinpath(*parts), "ReportsDialog": Mock()}
        self.accounts = [workflows.Account("codex-test", "codex", "Offline Codex", self.root, "offline@example.test"),
                         workflows.Account("claude-test", "claude", "Offline Claude", self.root, "offline@example.test")]
        self.discovery = patch.object(workflows, "discover_accounts", return_value=self.accounts)
        self.discovery.start()
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            if dialog.winfo_exists():
                dialog._operation_busy = False
                dialog._close()
        self.app.update()
        self.discovery.stop()
        self.directory.cleanup()

    def pump(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.update()
            if predicate():
                return
            time.sleep(0.01)
        self.fail("Hidden dialog operation did not finish within bounded wait")

    def ai_dialog(self, role="audit-review"):
        dialog = ui.AIWorkflowDialog(self.app, self.ns, role)
        dialog.withdraw()
        self.dialogs.append(dialog)
        self.pump(lambda: not dialog._operation_busy)
        return dialog

    def notification_dialog(self):
        dialog = ui.NotificationSettingsDialog(self.app, self.ns)
        dialog.withdraw()
        self.dialogs.append(dialog)
        return dialog

    def prepared(self, dialog):
        return types.SimpleNamespace(workspace=self.root / dialog.role, request_dir=self.root / "request-1",
            command="Read the offline exact request", preflight={"email": "offline@example.test", "model": "offline-model", "effort": "high"})

    def test_audit_dialog_requires_completion_confirmation_and_correct_allflags_scope(self):
        dialog = self.ai_dialog()
        self.assertFalse(dialog.complete_var.get())
        with self.assertRaisesRegex(workflows.WorkflowError, "Confirm"):
            dialog._collect()
        dialog.complete_var.set(True)
        values = dialog._collect()
        self.assertTrue(values["review_all_flags"])
        self.assertTrue(values["completed_audit"])
        self.assertFalse(values["allow_document_changes"])
        self.assertFalse(values["allow_code_changes"])
        self.assertEqual(values["model_key"], "luna")
        self.assertEqual(values["account"].provider, "codex")
        self.ns["style_titlebar_black"].assert_called()

    def test_only_verified_exact_report_is_prechecked_and_browse_invalidates_it(self):
        self.app._latest_audit_completed = True
        dialog = self.ai_dialog()
        self.assertTrue(dialog.complete_var.get())
        dialog.audit_var.set(str(self.root / "another-audit.csv"))
        self.assertFalse(dialog.complete_var.get())

    def test_model_switch_filters_provider_and_blocks_stale_request(self):
        dialog = self.ai_dialog()
        dialog.prepared = self.prepared(dialog)
        dialog.model_var.set(workflows.MODEL_CHOICES["opus"]["label"])
        dialog._model_changed()
        self.assertEqual(len(dialog.filtered_accounts), 1)
        self.assertEqual(dialog.filtered_accounts[0].provider, "claude")
        self.assertIsNone(dialog.prepared)
        self.assertEqual(str(dialog.launch_button["state"]), "disabled")

    def test_correction_authority_requires_explicit_files_folder(self):
        dialog = self.ai_dialog()
        dialog.complete_var.set(True)
        dialog.allow_var.set(True)
        dialog.processing_var.set("")
        with self.assertRaisesRegex(workflows.WorkflowError, "original Files"):
            dialog._collect()
        dialog.processing_var.set(str(self.root))
        self.assertTrue(dialog._collect()["allow_document_changes"])

    def test_learning_scope_allows_code_only_when_checked(self):
        dialog = self.ai_dialog("code-learning")
        values = dialog._collect()
        self.assertEqual(values["model_key"], "fable")
        self.assertFalse(values["allow_code_changes"])
        self.assertFalse(values["allow_document_changes"])
        self.assertIsNone(values["audit_report"])
        dialog.allow_var.set(True)
        self.assertTrue(dialog._collect()["allow_code_changes"])

    def test_prepare_verifies_in_background_and_shows_email_before_launch(self):
        dialog = self.ai_dialog()
        dialog.complete_var.set(True)
        threads = []
        def prepare(**kwargs):
            threads.append(threading.get_ident())
            self.assertTrue(kwargs["review_all_flags"])
            return self.prepared(dialog)
        with patch.object(workflows, "prepare_workflow", side_effect=prepare) as prepare_call, patch.object(workflows, "launch_workflow") as launch:
            dialog._prepare()
            self.pump(lambda: not dialog._operation_busy)
        self.assertNotEqual(threads[0], threading.get_ident())
        prepare_call.assert_called_once()
        launch.assert_not_called()
        self.assertIn("offline@example.test", dialog.status.get())
        self.assertIn("not launched", dialog.status.get())
        self.assertEqual(str(dialog.launch_button["state"]), "normal")
        self.assertEqual(self.app.cfg["ai_workflows"]["audit-review_expected_email"], "offline@example.test")

    def test_form_changed_during_prepare_cannot_launch_old_scope(self):
        dialog = self.ai_dialog()
        dialog.complete_var.set(True)
        gate = threading.Event()
        def prepare(**_kwargs):
            gate.wait(2)
            return self.prepared(dialog)
        with patch.object(workflows, "prepare_workflow", side_effect=prepare):
            dialog._prepare()
            dialog.allow_var.set(True)
            gate.set()
            self.pump(lambda: not dialog._operation_busy)
        self.assertIsNone(dialog.prepared)
        self.assertEqual(str(dialog.launch_button["state"]), "disabled")
        self.assertIn("form changed", dialog.status.get())

    def test_launch_rechecks_busy_and_does_not_claim_completion(self):
        dialog = self.ai_dialog()
        dialog.prepared = self.prepared(dialog)
        self.app._recovery_busy = True
        with patch.object(workflows, "launch_workflow") as launch:
            dialog._launch()
            launch.assert_not_called()
            self.app._recovery_busy = False
            dialog._launch()
            self.pump(lambda: not dialog._operation_busy)
        launch.assert_called_once()
        self.assertIn("not review completion", dialog.status.get())
        self.app.notification_service.emit.assert_called_once()
        self.assertEqual(self.app.notification_service.emit.call_args.args[0], "review_started")

    def test_notification_settings_validate_and_never_persist_raw_token(self):
        dialog = self.notification_dialog()
        values = dialog.channel_vars["discord"]
        values["enabled"].set(True)
        values["destination"].set("1234567890")
        values["new_token"].set("offline-secret")
        dialog._test()
        self.assertIn("Save the new bot token first", dialog.status.get())
        with patch.object(notifications, "set_bot_token") as store:
            dialog._save()
            self.pump(lambda: not dialog._operation_busy)
        store.assert_called_once_with("discord", "offline-secret", "discord")
        self.assertNotIn("offline-secret", str(self.app.cfg))
        self.assertEqual(values["new_token"].get(), "")
        self.app.notification_service.configure.assert_called_once()

    def test_settings_save_failure_retains_original_configuration(self):
        original = notifications.default_settings()
        self.app.cfg["notifications"] = original
        self.ns["save_config"].return_value = False
        dialog = self.notification_dialog()
        dialog.progress_var.set(False)
        dialog._save()
        self.pump(lambda: not dialog._operation_busy)
        self.assertIs(self.app.cfg["notifications"], original)
        self.app.notification_service.configure.assert_not_called()
        self.assertIn("could not be saved", dialog.status.get())

    def test_notifications_take_and_restore_settings_modal_grab(self):
        settings = tk.Toplevel(self.app)
        settings.withdraw()
        settings.grab_set()
        try:
            dialog = self.notification_dialog()
            self.assertIs(dialog.master, settings)
            self.assertIs(self.app.grab_current(), dialog)
            dialog._close()
            self.assertIs(self.app.grab_current(), settings)
        finally:
            settings.grab_release()
            settings.destroy()

    def test_test_service_has_separate_queue_and_actual_delivery_status(self):
        dialog = self.notification_dialog()
        variables = dialog.channel_vars["discord"]
        variables["enabled"].set(True)
        variables["destination"].set("1234567890")
        original_class = notifications.NotificationService
        sender = Mock()
        with patch.object(notifications, "NotificationService", side_effect=lambda settings: original_class(settings, sender=sender)):
            dialog._test()
            self.assertIn("queued", dialog.status.get())
            self.pump(lambda: "delivered" in dialog.status.get())
        sender.assert_called_once()
        self.app.notification_service.drain_statuses.assert_not_called()
        self.app.notification_service.emit.assert_not_called()

    def test_reports_choice_preserves_audit_browser_and_master_ledger(self):
        ledger = self.root / "Master_Filename_Review_Ledger.xlsx"
        self.app.cfg["ai_workflows"] = {"ledger_path": str(ledger)}
        dialog = ui.ReportsMenuDialog(self.app, self.ns)
        dialog.withdraw()
        self.dialogs.append(dialog)
        self.assertEqual(dialog.ledger, ledger)
        dialog._audits()
        self.ns["ReportsDialog"].assert_called_once_with(self.app)
        self.assertFalse(dialog.winfo_exists())

    def test_mapped_dialog_footer_buttons_fit_default_and_minimum_geometry(self):
        # Map only our own alpha-zero windows: geometry must be tested on mapped
        # native windows, not merely inferred from withdrawn requested sizes.
        self.app.attributes("-alpha", 0.0)
        self.app.deiconify()
        try:
            for kind, sizes in (("reports", ((690, 490), (640, 480))),
                                ("notifications", ((780, 760), (640, 480))),
                                ("audit-review", ((780, 780), (640, 480))),
                                ("code-learning", ((780, 780), (640, 480)))):
                if kind == "reports":
                    dialog = ui.ReportsMenuDialog(self.app, self.ns)
                    self.dialogs.append(dialog)
                elif kind == "notifications":
                    dialog = self.notification_dialog()
                else:
                    dialog = self.ai_dialog(kind)
                dialog.attributes("-alpha", 0.0)
                for width, height in sizes:
                    with self.subTest(dialog=kind, width=width, height=height,
                                      tk_scale=float(self.app.tk.call("tk", "scaling"))):
                        dialog.geometry(f"{width}x{height}+0+0")
                        dialog.deiconify()
                        self.app.update()
                        for button in dialog.footer.winfo_children():
                            if not isinstance(button, tk.Button):
                                continue
                            self.assertTrue(button.winfo_viewable(), button.cget("text"))
                            self.assertGreaterEqual(button.winfo_width(), button.winfo_reqwidth(), button.cget("text"))
                            self.assertGreaterEqual(button.winfo_height(), button.winfo_reqheight(), button.cget("text"))
                            bottom = button.winfo_rooty() - dialog.winfo_rooty() + button.winfo_height()
                            self.assertLessEqual(bottom, dialog.winfo_height(), button.cget("text"))
                        self.assertGreater(dialog._body_canvas.winfo_height(), 80)
                dialog._close()
        finally:
            self.app.withdraw()

    def test_native_caption_style_is_accepted_after_map_and_restore(self):
        import platform
        if platform.system() != "Windows":
            self.skipTest("Native DWM caption test is Windows-only")
        import ctypes
        from ctypes import wintypes
        from _load_app import load_app
        self.ns["style_titlebar_black"] = load_app().style_titlebar_black
        self.app.attributes("-alpha", 0.0)
        self.app.deiconify()
        dialog = ui.ReportsMenuDialog(self.app, self.ns)
        self.dialogs.append(dialog)
        dialog.attributes("-alpha", 0.0)
        get_parent = ctypes.windll.user32.GetParent
        get_parent.argtypes, get_parent.restype = [wintypes.HWND], wintypes.HWND
        native_set = ctypes.windll.dwmapi.DwmSetWindowAttribute
        native_set.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        native_set.restype = ctypes.c_long
        accepted = []
        def record_native_set(hwnd, attribute, pointer, size):
            result = native_set(hwnd, attribute, pointer, size)
            value = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint)).contents.value
            accepted.append((hwnd, attribute, value, result))
            return result
        def mapped_and_styled():
            accepted.clear()
            dialog.deiconify()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                self.app.update()
                hwnd = get_parent(dialog.winfo_id()) or dialog.winfo_id()
                if all(sum(call[:3] == (hwnd, attribute, expected) for call in accepted) >= 2
                       for attribute, expected in ((35, 0x000000), (36, 0xFFFFFF))):
                    break
                time.sleep(0.01)
            hwnd = get_parent(dialog.winfo_id()) or dialog.winfo_id()
            for attribute, expected in ((35, 0x000000), (36, 0xFFFFFF)):
                # Microsoft documents caption/text colors as Set-only; Get
                # returns E_INVALIDARG even on Windows 11. Forward the REAL call
                # and verify Windows accepted the requested value on final HWND.
                matches = [call for call in accepted if call[:3] == (hwnd, attribute, expected)]
                self.assertGreaterEqual(len(matches), 2, f"No delayed Map reapplication of DWM attribute {attribute}")
                self.assertTrue(all(call[3] == 0 for call in matches), matches)
        try:
            with patch.object(ctypes.windll.dwmapi, "DwmSetWindowAttribute", new=record_native_set):
                mapped_and_styled()
                dialog.withdraw()
                self.app.update()
                mapped_and_styled()
        finally:
            self.app.withdraw()

    def test_dialog_footer_reflows_at_larger_tk_scaling(self):
        original = float(self.app.tk.call("tk", "scaling"))
        try:
            self.app.tk.call("tk", "scaling", 2.0)
            self.test_mapped_dialog_footer_buttons_fit_default_and_minimum_geometry()
        finally:
            self.app.tk.call("tk", "scaling", original)


if __name__ == "__main__":
    unittest.main()
