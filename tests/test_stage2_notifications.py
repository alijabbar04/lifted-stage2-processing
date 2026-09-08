"""Offline notification tests: no real messages, tokens or processing runs."""
import decimal
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import stage2_notifications as notifications


def configured(telegram=False, slack=False):
    result = notifications.default_settings()
    result["discord"].update(enabled=True, destination="1234567890")
    result["telegram"].update(enabled=telegram, destination="-12345")
    result["slack"].update(enabled=slack, destination="Example Workspace / #stage-2")
    return result


class TestNotificationSettings(unittest.TestCase):
    def test_defaults_are_opt_in_and_not_personal(self):
        result = notifications.default_settings()
        self.assertFalse(result["discord"]["enabled"])
        self.assertFalse(result["telegram"]["enabled"])
        self.assertFalse(result["slack"]["enabled"])
        self.assertFalse(result["discord"]["destination"])
        self.assertFalse(result["discord"]["credential_file"])
        self.assertEqual(result["slack"]["token_env"], "SLACK_WEBHOOK_URL")
        self.assertEqual(notifications.CHANNELS, ("discord", "telegram", "slack"))

    def test_normalization_discards_secrets_and_rejects_malformed_settings(self):
        result = notifications.normalize_settings({
            "discord": {"enabled": "false", "token": "secret", "credential_ref": "../bad", "token_env": "anything\n"},
            "telegram": None, "wait_minutes": "invalid", "token": "secret"})
        self.assertFalse(result["discord"]["enabled"])
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(result["discord"]["credential_ref"], "discord")
        self.assertEqual(result["discord"]["token_env"], "DISCORD_BOT_TOKEN")
        self.assertEqual(result["wait_minutes"], 10)
        # Existing two-provider settings migrate safely without enabling Slack.
        self.assertEqual(result["slack"], notifications.default_settings()["slack"])

    def test_slack_normalization_discards_pasted_webhook_and_sanitizes_references(self):
        result = notifications.normalize_settings({"slack": {
            "enabled": True, "destination": " Example Workspace / #ops ",
            "webhook_url": "https://hooks.slack.com/services/T/B/SECRET",
            "token": "also-secret", "credential_ref": "../bad",
            "token_env": "not valid"}})
        self.assertTrue(result["slack"]["enabled"])
        self.assertEqual(result["slack"]["destination"], "Example Workspace / #ops")
        self.assertEqual(result["slack"]["credential_ref"], "slack")
        self.assertEqual(result["slack"]["token_env"], "SLACK_WEBHOOK_URL")
        self.assertNotIn("SECRET", json.dumps(result))
        pasted = notifications.normalize_settings({"slack": {
            "destination": "https://hooks.slack.com/services/T/B/SECRET"}})
        self.assertEqual(pasted["slack"]["destination"], "")

    def test_existing_dotenv_reference_reads_only_named_token(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / ".env"
            file.write_text('UNRELATED_KEY=private\n DISCORD_BOT_TOKEN = "offline-token"\n', encoding="utf-8")
            setting = configured()
            setting["discord"]["credential_file"] = str(file)
            with patch.dict("sys.modules", {"keyring": None}), \
                    patch.dict("os.environ", {"DISCORD_BOT_TOKEN": ""}):
                self.assertEqual(notifications._resolve_token("discord", setting["discord"]), "offline-token")
                label = notifications.credentials_status("discord", setting)
            self.assertIn("Credential available", label)
            self.assertNotIn("offline-token", label)
            self.assertNotIn("private", label)

    def test_os_credential_storage_never_falls_back_to_settings(self):
        keyring = types.SimpleNamespace(set_password=Mock())
        with patch.dict("sys.modules", {"keyring": keyring}):
            notifications.set_bot_token("discord", "offline-only")
        keyring.set_password.assert_called_once_with(notifications.KEYRING_SERVICE, "discord", "offline-only")
        with patch.dict("sys.modules", {"keyring": None}):
            with self.assertRaisesRegex(RuntimeError, "OS credential store"):
                notifications.set_bot_token("discord", "offline-only")

    def test_slack_webhook_uses_same_os_store_and_requires_real_slack_url(self):
        webhook = "https://hooks.slack.com/services/T000/B000/offline_secret"
        keyring = types.SimpleNamespace(set_password=Mock())
        with patch.dict("sys.modules", {"keyring": keyring}):
            notifications.set_bot_token("slack", webhook, "slack-test")
        keyring.set_password.assert_called_once_with(
            notifications.KEYRING_SERVICE, "slack-test", webhook)
        for invalid in (
                "http://hooks.slack.com/services/T/B/secret",
                "https://hooks.slack.com.evil.test/services/T/B/secret",
                "https://hooks.slack.com/services/T/B/secret?redirect=evil",
                "https://example.test/services/T/B/secret",
                "https://hooks.slack.com/not-services/T/B/secret",
                "https://hooks.slack.com/services/T/B/secret\n"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "Slack incoming webhook"):
                notifications.set_bot_token("slack", invalid)

    def test_slack_env_and_local_file_fallback_use_only_named_webhook(self):
        webhook = "https://hooks.slack.com/services/T000/B000/offline_secret"
        settings = notifications.default_settings()
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / ".env"
            file.write_text(f"UNRELATED=private\nSLACK_WEBHOOK_URL={webhook}\n", encoding="utf-8")
            settings["slack"]["credential_file"] = str(file)
            with patch.dict("sys.modules", {"keyring": None}), \
                    patch.dict("os.environ", {"SLACK_WEBHOOK_URL": ""}):
                self.assertEqual(notifications._resolve_token("slack", settings["slack"]), webhook)
            env_webhook = "https://hooks.slack-gov.com/services/T111/B111/env_secret"
            with patch.dict("sys.modules", {"keyring": None}), \
                    patch.dict("os.environ", {"SLACK_WEBHOOK_URL": env_webhook}):
                self.assertEqual(notifications._resolve_token("slack", settings["slack"]), env_webhook)

    def test_invalid_destination_and_missing_token_have_safe_status(self):
        settings = configured()
        settings["discord"]["destination"] = "https://external.test"
        self.assertEqual(notifications.credentials_status("discord", settings), "Destination format is not valid")
        with patch.object(notifications, "_resolve_token", return_value=""):
            self.assertEqual(notifications.credentials_status("discord", configured()), "Bot token not found")

    def test_slack_status_distinguishes_local_credential_from_verified_delivery(self):
        settings = notifications.default_settings()
        settings["slack"].update(enabled=True, destination="")
        with patch.object(notifications, "_resolve_token", return_value=""):
            self.assertEqual(notifications.credentials_status("slack", settings), "Slack webhook URL not found")
        with patch.object(notifications, "_resolve_token", return_value="https://example.test/hook"):
            self.assertEqual(notifications.credentials_status("slack", settings), "Slack webhook URL format is not valid")
        webhook = "https://hooks.slack.com/services/T000/B000/offline_secret"
        with patch.object(notifications, "_resolve_token", return_value=webhook):
            label = notifications.credentials_status("slack", settings)
        self.assertIn("Credential available", label)
        self.assertIn("verify delivery", label)
        self.assertNotIn("delivered", label.lower())


class TestNotificationMessages(unittest.TestCase):
    def test_templates_do_not_emit_names_paths_contents_or_arbitrary_error(self):
        for event in notifications.EVENTS:
            with self.subTest(event=event):
                text = notifications.format_event(event, worker="Private Worker", filename="SECRET.pdf",
                    path="C:\\Private\\SECRET.pdf", reason="Private Worker API_TOKEN=secret", exception="secret",
                    phase="Private Worker", completed=3, total=4, workers=5, cost_gbp=1.2)
                self.assertNotIn("Private", text)
                self.assertNotIn("SECRET", text)
                self.assertNotIn("secret", text)
                self.assertLessEqual(len(text), 1900)

    def test_audit_and_submission_wording_does_not_overclaim(self):
        self.assertIn("not a completed run", notifications.format_event("batch_submitted"))
        self.assertIn("does not rename", notifications.format_event("audit_started"))
        self.assertIn("not a guarantee", notifications.format_event("audit_complete"))
        self.assertIn("not confirmation", notifications.format_event("review_started"))
        self.assertIn("not a billing statement", notifications.format_event("run_complete", cost_gbp=10))

    def test_safe_numerics_and_unknown_event(self):
        self.assertIn("100%", notifications.format_event("progress", completed=999, total=10))
        self.assertNotIn("nan", notifications.format_event("run_complete", cost_gbp=float("nan")))
        with self.assertRaises(ValueError):
            notifications.format_event("arbitrary-message")

    def test_run_complete_never_implies_an_unverified_audit_passed(self):
        unknown = notifications.format_event("run_complete", workers=5)
        self.assertNotIn("0 document(s) flagged", unknown)
        self.assertIn("does not confirm", unknown)
        pending = notifications.format_event("run_complete", audit_status="pending")
        self.assertIn("processing completion only", pending)
        finished = notifications.format_event("run_complete", audit_status="complete", needs_review=7)
        self.assertIn("7 document(s) flagged", finished)


class TestNotificationQueue(unittest.TestCase):
    def test_emit_is_nonblocking_and_delivery_not_on_caller_thread(self):
        release = threading.Event()
        invoked = threading.Event()
        calls = []
        def sender(*args):
            calls.append(threading.get_ident())
            invoked.set()
            release.wait(3)
        service = notifications.NotificationService(configured(), sender=sender)
        started = time.monotonic()
        self.assertTrue(service.emit("run_started", workers=5))
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertTrue(invoked.wait(1))
        self.assertNotEqual(calls[0], threading.get_ident())
        release.set()
        self.assertTrue(service.flush(2))
        self.assertIn("delivered", service.drain_statuses()[0])
        service.close()

    def test_defaults_off_unknown_event_and_closed_are_no_ops(self):
        service = notifications.NotificationService(start=False)
        self.assertFalse(service.emit("run_started"))
        service.configure(configured())
        self.assertFalse(service.emit("random"))
        service.close()
        self.assertFalse(service.emit("run_started"))

    def test_duplicate_milestones_suppressed_across_same_run_not_new_run(self):
        service = notifications.NotificationService(configured(), start=False)
        self.assertTrue(service.emit("phase_started", run_id="run-1", phase="audit"))
        self.assertFalse(service.emit("phase_started", run_id="run-1", phase="audit"))
        self.assertTrue(service.emit("phase_started", run_id="run-2", phase="audit"))
        self.assertEqual(len(service._pending), 2)
        service.close()

    def test_progress_is_25_percent_buckets_and_two_minute_throttle(self):
        now = [0.0]
        service = notifications.NotificationService(configured(), clock=lambda: now[0], start=False)
        self.assertFalse(service.emit("progress", completed=1, total=100))
        self.assertTrue(service.emit("progress", completed=25, total=100))
        self.assertFalse(service.emit("progress", completed=30, total=100))
        now[0] = 60
        self.assertFalse(service.emit("progress", completed=50, total=100))
        now[0] = 120
        self.assertTrue(service.emit("progress", completed=60, total=100))
        self.assertTrue(service.emit("progress", completed=100, total=100))
        self.assertFalse(service.emit("progress", completed=100, total=100))
        service.close()

    def test_long_wait_initial_delay_then_half_hour(self):
        now = [0.0]
        service = notifications.NotificationService(configured(), clock=lambda: now[0], start=False)
        self.assertFalse(service.emit("long_wait", wait_seconds=599))
        now[0] = 600
        self.assertTrue(service.emit("long_wait", wait_seconds=600))
        now[0] = 1800
        self.assertFalse(service.emit("long_wait", wait_seconds=1800))
        now[0] = 2400
        self.assertTrue(service.emit("long_wait", wait_seconds=2400))
        service.close()

    def test_queue_bound_and_important_messages_preserve_chronology(self):
        service = notifications.NotificationService(configured(), start=False)
        for number in range(notifications.MAX_PENDING):
            self.assertTrue(service.emit("worker_complete", completed=number, total=100))
        self.assertFalse(service.emit("worker_complete", completed=65, total=100))
        self.assertTrue(service.emit("run_complete", workers=100))
        self.assertEqual(len(service._pending), notifications.MAX_PENDING)
        self.assertEqual(service._pending[-1][0], "run_complete")
        service.close()

    def test_delivery_failure_does_not_block_other_channel_or_processing(self):
        calls = []
        def sender(channel, *_):
            calls.append(channel)
            if channel == "discord":
                raise RuntimeError("SUPER_SECRET_TOKEN in unexpected error")
        service = notifications.NotificationService(configured(telegram=True), sender=sender)
        self.assertTrue(service.emit("run_started"))
        self.assertTrue(service.flush(2))
        status = "\n".join(service.drain_statuses())
        self.assertEqual(calls, ["discord", "telegram"])
        self.assertIn("Processing is unaffected", status)
        self.assertIn("Telegram notification delivered", status)
        self.assertNotIn("SUPER_SECRET", status)
        service.close()

    def test_slack_joins_aggregate_queue_without_becoming_required(self):
        calls = []
        service = notifications.NotificationService(
            configured(telegram=True, slack=True),
            sender=lambda channel, *_: calls.append(channel))
        self.assertTrue(service.emit("phase_complete", phase="audit"))
        self.assertTrue(service.flush(2))
        self.assertEqual(calls, ["discord", "telegram", "slack"])
        self.assertEqual(len(service.drain_statuses()), 3)
        service.close()

    def test_retries_bounded_and_success_after_retry_reported_once(self):
        sender = Mock(side_effect=[notifications.DeliveryError("Rate limited", retry=True, delay=0), None])
        service = notifications.NotificationService(configured(), sender=sender, start=False)
        with patch.object(service._stopping, "wait", return_value=False):
            service._thread.start()
            service.emit("run_started")
            self.assertTrue(service.flush(2))
        self.assertEqual(sender.call_count, 2)
        status = service.drain_statuses()
        self.assertEqual(len(status), 1)
        self.assertIn("delivered", status[0])
        service.close()

    def test_retry_after_is_honoured_on_worker_without_blocking_emit(self):
        sender = Mock(side_effect=[notifications.DeliveryError("Rate limited", retry=True, delay=12), None])
        service = notifications.NotificationService(configured(), sender=sender, start=False)
        with patch.object(service._stopping, "wait", return_value=False) as wait:
            service._thread.start()
            self.assertTrue(service.emit("phase_complete"))
            self.assertTrue(service.flush(2))
        wait.assert_called_once_with(12)
        self.assertEqual(sender.call_count, 2)
        self.assertEqual(len(service.drain_statuses()), 1)
        service.close()

    def test_transport_marked_long_retry_is_not_retried(self):
        sender = Mock(side_effect=notifications.DeliveryError(
            "Rate limited", retry=False, delay=notifications.MAX_RETRY_DELAY + 1))
        service = notifications.NotificationService(configured(), sender=sender, start=False)
        with patch.object(service._stopping, "wait", return_value=False) as wait:
            service._thread.start()
            self.assertTrue(service.emit("phase_complete"))
            self.assertTrue(service.flush(2))
        sender.assert_called_once()
        wait.assert_not_called()
        self.assertIn("not delivered", service.drain_statuses()[0])
        service.close()

    def test_three_failures_exhaust_retry_budget(self):
        sender = Mock(side_effect=notifications.DeliveryError("Unavailable", retry=True))
        service = notifications.NotificationService(configured(), sender=sender, start=False)
        with patch.object(service._stopping, "wait", return_value=False):
            service._thread.start()
            service.emit("run_started")
            self.assertTrue(service.flush(2))
        self.assertEqual(sender.call_count, 3)
        self.assertIn("not delivered", service.drain_statuses()[0])
        service.close()

    def test_disable_prevents_queued_delivery_and_close_is_immediate(self):
        sender = Mock()
        service = notifications.NotificationService(configured(), sender=sender, start=False)
        service.emit("run_started")
        service.configure(notifications.default_settings())
        service._thread.start()
        self.assertTrue(service.flush(2))
        sender.assert_not_called()
        started = time.monotonic()
        service.close()
        self.assertLess(time.monotonic() - started, 0.1)


class TestNotificationTransport(unittest.TestCase):
    def test_discord_disables_mentions_and_uses_replay_nonce(self):
        with patch.object(notifications, "_resolve_token", return_value="offline-token"), \
                patch.object(notifications, "_post_json") as post:
            notifications._send("discord", configured()["discord"], "hello @everyone", "stable-event-id")
        url, headers, payload = post.call_args.args
        self.assertEqual(url, "https://discord.com/api/v10/channels/1234567890/messages")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertTrue(payload["enforce_nonce"])
        self.assertEqual(payload["nonce"], "stable-event-id")

    def test_telegram_send_plain_text_without_preview(self):
        with patch.object(notifications, "_resolve_token", return_value="123456:offline_token"), \
                patch.object(notifications, "_post_json") as post:
            notifications._send("telegram", configured(telegram=True)["telegram"], "hello", "event")
        payload = post.call_args.args[2]
        self.assertEqual(payload["chat_id"], "-12345")
        self.assertNotIn("parse_mode", payload)
        self.assertTrue(payload["link_preview_options"]["is_disabled"])

    def test_slack_uses_only_webhook_bound_routing_and_plain_text(self):
        webhook = "https://hooks.slack.com/services/T000/B000/offline_secret"
        settings = configured(slack=True)["slack"]
        settings["destination"] = "Display label that must not affect routing"
        with patch.object(notifications, "_resolve_token", return_value=webhook), \
                patch.object(notifications, "_post_slack") as post:
            notifications._send("slack", settings, "hello", "event-id-not-supported-by-slack")
        post.assert_called_once_with(webhook, {"text": "hello"})
        self.assertNotIn(settings["destination"], repr(post.call_args))

    def test_slack_requires_exact_plain_ok_success(self):
        class Response:
            def __init__(self, status, body):
                self.status, self.body = status, body
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def read(self, _):
                return self.body
            def getcode(self):
                return self.status

        opener = Mock()
        opener.open.return_value = Response(200, b"ok")
        with patch.object(notifications.urllib.request, "build_opener", return_value=opener):
            self.assertIsNone(notifications._post_slack(
                "https://hooks.slack.com/services/T000/B000/offline_secret", {"text": "hello"}))
        request = opener.open.call_args.args[0]
        self.assertEqual(json.loads(request.data), {"text": "hello"})
        self.assertEqual(opener.open.call_args.kwargs["timeout"], notifications.REQUEST_TIMEOUT)

        for body in (b'{"ok": true}', b"OK", b"ok\n", b""):
            opener.open.return_value = Response(200, body)
            with self.subTest(body=body), patch.object(
                    notifications.urllib.request, "build_opener", return_value=opener):
                with self.assertRaises(notifications.DeliveryError) as caught:
                    notifications._post_slack(
                        "https://hooks.slack.com/services/T000/B000/offline_secret", {"text": "hello"})
                self.assertFalse(caught.exception.retry)
                self.assertIn("not verified", str(caught.exception))

    def test_slack_rate_limit_header_permanent_errors_and_network_are_safe(self):
        webhook = "https://hooks.slack.com/services/T000/B000/SUPER_SECRET"
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            webhook, 429, "SUPER_SECRET", {"Retry-After": "120"}, io.BytesIO(b"rate_limited"))
        with patch.object(notifications.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(notifications.DeliveryError) as caught:
                notifications._post_slack(webhook, {"text": "hello"})
        self.assertFalse(caught.exception.retry)
        self.assertEqual(caught.exception.delay, 120)
        self.assertNotIn("SUPER_SECRET", str(caught.exception))

        for header, expected_retry, expected_delay in (
                ("29.1", True, 30),
                ("1e1000000", False, decimal.Decimal("1e1000000")),
                ("nan", True, 2),
                ("invalid", True, 2),
                (None, True, 2)):
            headers = {} if header is None else {"Retry-After": header}
            opener.open.side_effect = urllib.error.HTTPError(
                webhook, 429, "SUPER_SECRET", headers, io.BytesIO(b"rate_limited"))
            with self.subTest(retry_after=header), patch.object(
                    notifications.urllib.request, "build_opener", return_value=opener):
                with self.assertRaises(notifications.DeliveryError) as parsed:
                    notifications._post_slack(webhook, {"text": "hello"})
            self.assertEqual(parsed.exception.retry, expected_retry)
            self.assertEqual(parsed.exception.delay, expected_delay)

        opener.open.side_effect = urllib.error.HTTPError(
            webhook, 410, "SUPER_SECRET", {}, io.BytesIO(b"channel_is_archived"))
        with patch.object(notifications.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(notifications.DeliveryError) as caught:
                notifications._post_slack(webhook, {"text": "hello"})
        self.assertFalse(caught.exception.retry)
        self.assertIn("no longer available", str(caught.exception))

        opener.open.side_effect = OSError(webhook)
        with patch.object(notifications.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(notifications.DeliveryError) as caught:
                notifications._post_slack(webhook, {"text": "hello"})
        self.assertTrue(caught.exception.retry)
        self.assertNotIn("SUPER_SECRET", str(caught.exception))

    def test_slack_post_rejects_non_slack_url_before_opening(self):
        opener = Mock()
        with patch.object(notifications.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(notifications.DeliveryError, "format is invalid"):
                notifications._post_slack("https://example.test/hook", {"text": "hello"})
        opener.open.assert_not_called()

    def test_rate_limit_delay_and_network_errors_are_sanitized(self):
        opener = Mock()
        error = urllib.error.HTTPError("https://private-token", 429, "raw private-token", {}, io.BytesIO(b'{"retry_after":99}'))
        opener.open.side_effect = error
        with patch.object(notifications.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(notifications.DeliveryError) as caught:
                notifications._post_json("https://example.test", {}, {})
        self.assertTrue(caught.exception.retry)
        self.assertEqual(caught.exception.delay, 30)
        self.assertNotIn("private-token", str(caught.exception))
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 15)
        opener.open.side_effect = OSError("https://botSECRET/sendMessage")
        with patch.object(notifications.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(notifications.DeliveryError) as caught:
                notifications._post_json("https://example.test", {}, {})
        self.assertNotIn("SECRET", str(caught.exception))

    def test_permission_errors_not_retried_and_redirects_disabled(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError("https://private-token", 403, "secret", {}, io.BytesIO(b"{}"))
        with patch.object(notifications.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(notifications.DeliveryError) as caught:
                notifications._post_json("https://example.test", {}, {})
        self.assertFalse(caught.exception.retry)
        self.assertIn("permission", str(caught.exception))
        self.assertIsNone(notifications._NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.test"))


if __name__ == "__main__":
    unittest.main()
