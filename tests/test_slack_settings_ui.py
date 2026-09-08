"""Offline Slack settings UI coverage; never contacts Slack or real keyring state."""
from pathlib import Path
import sys
import time
import tkinter as tk
import types
import unittest
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import stage2_notifications as notifications
import stage2_workflow_ui as ui


class TestSlackSettingsUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
            cls.root.withdraw()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Tk display unavailable: {exc}")

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        self.root.cfg = {}
        self.root.notification_service = Mock()
        self.root.worker_thread = None
        self.root._scanning = self.root._recovery_busy = False
        self.namespace = {"save_config": Mock(return_value=True), "style_titlebar_black": Mock()}
        # Construction and Tk variable edits must not touch a real keyring or
        # local .env path. The dialog only checks this explicit mock in a worker.
        self.credential_status = patch.object(
            notifications, "credentials_status",
            return_value="Credential available (use Test to verify delivery)")
        self.credential_status_mock = self.credential_status.start()
        self.dialog = ui.NotificationSettingsDialog(self.root, self.namespace)
        self.dialog.withdraw()

    def tearDown(self):
        if self.dialog.winfo_exists():
            self.dialog._operation_busy = False
            self.dialog._close()
        self.credential_status.stop()

    def _pump(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            if predicate():
                return
            time.sleep(.01)
        self.fail("UI operation did not complete")

    def test_slack_is_optional_display_label_and_webhook_is_stored_via_keyring_api(self):
        self.assertIn("slack", self.dialog.channel_vars)
        slack = self.dialog.channel_vars["slack"]
        entries = []
        pending = list(self.dialog.winfo_children())
        while pending:
            widget = pending.pop()
            if isinstance(widget, tk.Entry):
                entries.append(widget)
            pending.extend(widget.winfo_children())
        webhook_entry = next(entry for entry in entries
                             if entry.cget("textvariable") == str(slack["new_token"]))
        self.assertEqual(webhook_entry.cget("show"), "•")
        labels = []
        buttons = []
        pending = list(self.dialog.winfo_children())
        while pending:
            widget = pending.pop()
            if isinstance(widget, tk.Label):
                labels.append(widget.cget("text"))
            if isinstance(widget, tk.Button):
                buttons.append(widget.cget("text"))
            pending.extend(widget.winfo_children())
        self.assertIn("New webhook URL · optional, stored only in the OS credential store", labels)
        self.assertTrue({"Test Discord", "Test Telegram", "Test Slack", "Test enabled channels"}.issubset(buttons))
        slack["enabled"].set(True)
        slack["destination"].set("Care team updates")  # Display-only: not routing input.
        slack["new_token"].set("https://hooks.slack.com/services/T000/B000/secret")
        self.assertIn("Save this new credential first", self.dialog.credential_labels["slack"].cget("text"))
        self.dialog._check_credential_status("slack")
        self.assertIn("Save the new credential first", self.dialog.status.get())
        self.credential_status_mock.assert_not_called()
        values, tokens = self.dialog._values()
        self.assertTrue(values["slack"]["enabled"])
        self.assertEqual(values["slack"]["destination"], "Care team updates")
        self.assertEqual(tokens["slack"], "https://hooks.slack.com/services/T000/B000/secret")
        with patch.object(notifications, "set_bot_token") as store:
            self.dialog._save()
            self._pump(lambda: not self.dialog._operation_busy)
        store.assert_called_once_with("slack", "https://hooks.slack.com/services/T000/B000/secret", "slack")
        self.assertNotIn("hooks.slack.com", str(self.root.cfg))

    def test_slack_only_test_is_queued_then_reports_delivery_confirmation(self):
        slack = self.dialog.channel_vars["slack"]
        slack["enabled"].set(True)
        # A saved Discord choice remains untouched but is excluded from Slack's
        # one-channel test service configuration.
        discord = self.dialog.channel_vars["discord"]
        discord["enabled"].set(True)
        discord["destination"].set("unfinished edit")
        sender = Mock()
        original = notifications.NotificationService
        captured = []
        with patch.object(notifications, "NotificationService",
                          side_effect=lambda values: (captured.append(values), original(values, sender=sender))[1]):
            self.dialog._test_channel("slack")
            self.assertIn("Slack test queued for delivery confirmation", self.dialog.status.get())
            self._pump(lambda: "delivered" in self.dialog.status.get())
        sender.assert_called_once()
        self.assertTrue(captured[0]["slack"]["enabled"])
        self.assertFalse(captured[0]["discord"]["enabled"])
        self.assertTrue(discord["enabled"].get())

    def test_tests_do_not_start_during_credential_save_or_check(self):
        self.dialog._operation_busy = True
        with patch.object(notifications, "NotificationService") as service:
            self.dialog._test_channel("slack")
            self.dialog._test()
            service.assert_not_called()

    def test_explicit_credential_check_runs_after_construction(self):
        self.credential_status_mock.assert_not_called()
        self.dialog._check_credential_status("slack")
        self._pump(lambda: not self.dialog._operation_busy)
        self.credential_status_mock.assert_called_once()
        self.assertIn("Credential status checked locally", self.dialog.status.get())

    def test_slack_setup_opens_only_the_public_setup_guide(self):
        with patch.object(ui.webbrowser, "open", return_value=True) as open_browser:
            self.dialog._open_slack_setup()
        open_browser.assert_called_once_with(
            "https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/", new=2)
        self.assertIn("setup guide", self.dialog.status.get())
        with patch.object(ui.webbrowser, "open", return_value=False):
            self.dialog._open_slack_setup()
        self.assertIn("Could not open", self.dialog.status.get())
