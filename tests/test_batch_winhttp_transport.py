"""No-network native transport tests. Every COM/HTTP send boundary is mocked."""
import json
import threading
import types
import unittest
from unittest.mock import Mock, patch

from _load_app import load_app

app = load_app()
import batch_transport


URL = "https://api.anthropic.com/v1/messages/batches"


def fake_com():
    pythoncom = types.SimpleNamespace(
        COINIT_APARTMENTTHREADED=2, DISPATCH_PROPERTYPUT=4,
        VT_VOID=24, VT_I4=3, VT_VARIANT=12, VT_ARRAY=8192, VT_UI1=17,
        CoInitializeEx=Mock(), CoUninitialize=Mock())
    request = Mock()
    request.Status = 200
    request.ResponseText = '{"id":"offline-batch","processing_status":"in_progress"}'
    request._oleobj_.GetIDsOfNames.return_value = 6
    client = types.SimpleNamespace(
        Dispatch=Mock(return_value=request),
        VARIANT=Mock(side_effect=lambda kind, value: types.SimpleNamespace(
            varianttype=kind, value=value)))
    return pythoncom, client, request


class TestWinHttpBatchTransport(unittest.TestCase):
    def test_exact_utf8_bytearray_one_send_and_protected_request_setup(self):
        com, client, request = fake_com()
        payload = json.dumps({"requests": [{"custom_id": "é-文件"}]},
                             ensure_ascii=False).encode("utf-8")
        headers = {"content-type": "application/json", "x-api-key": "offline-only"}
        with patch.object(batch_transport, "_load_com", return_value=(com, client)):
            status, raw = batch_transport.winhttp_post_batch(URL, headers, payload)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["id"], "offline-batch")
        request.Open.assert_called_once_with("POST", URL, False)
        request.SetTimeouts.assert_called_once_with(30000, 30000, 120000, 120000)
        request.SetAutoLogonPolicy.assert_called_once_with(2)
        request._oleobj_.InvokeTypes.assert_called_once_with(
            6, 0, com.DISPATCH_PROPERTYPUT, (com.VT_VOID, 0),
            ((com.VT_I4, 1), (com.VT_VARIANT, 1)), 6, False)
        request.Send.assert_called_once()
        sent = request.Send.call_args.args[0]
        self.assertEqual(sent.varianttype, com.VT_ARRAY | com.VT_UI1)
        self.assertEqual(sent.value, payload)
        self.assertTrue(all(timeout > 0 for timeout in request.SetTimeouts.call_args.args))
        com.CoInitializeEx.assert_called_once_with(com.COINIT_APARTMENTTHREADED)
        com.CoUninitialize.assert_called_once()

    def test_send_exception_has_no_retry_fallback_or_secret_in_error(self):
        com, client, request = fake_com()
        error = RuntimeError("secret-key-and-document-body")
        error.hresult = -2147012866
        request.Send.side_effect = error
        with patch.object(batch_transport, "_load_com", return_value=(com, client)), \
                patch.object(app.urllib.request, "urlopen") as other_transport:
            with self.assertRaises(batch_transport.BatchTransportError) as caught:
                batch_transport.winhttp_post_batch(URL, {"x-api-key": "secret-key"}, b"{}")
        self.assertTrue(caught.exception.send_started)
        self.assertIn("HRESULT", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))
        request.Send.assert_called_once()
        other_transport.assert_not_called()
        com.CoUninitialize.assert_called_once()

    def test_option_setup_failure_never_sends_and_releases_com(self):
        com, client, request = fake_com()
        request._oleobj_.InvokeTypes.side_effect = RuntimeError("bad property")
        with patch.object(batch_transport, "_load_com", return_value=(com, client)):
            with self.assertRaises(batch_transport.BatchTransportError) as caught:
                batch_transport.winhttp_post_batch(URL, {}, b"{}")
        self.assertFalse(caught.exception.send_started)
        request.Send.assert_not_called()
        com.CoUninitialize.assert_called_once()

    def test_com_initialization_failure_does_not_uninitialize_unowned_apartment(self):
        com, client, request = fake_com()
        com.CoInitializeEx.side_effect = RuntimeError("apartment unavailable")
        with patch.object(batch_transport, "_load_com", return_value=(com, client)):
            with self.assertRaises(batch_transport.BatchTransportError):
                batch_transport.winhttp_post_batch(URL, {}, b"{}")
        client.Dispatch.assert_not_called()
        request.Send.assert_not_called()
        com.CoUninitialize.assert_not_called()

    def test_com_initializes_and_uninitializes_on_worker_thread(self):
        com, client, request = fake_com()
        seen = []
        com.CoInitializeEx.side_effect = lambda _flag: seen.append(("initialize", threading.get_ident()))
        request.Send.side_effect = lambda _body: seen.append(("send", threading.get_ident()))
        com.CoUninitialize.side_effect = lambda: seen.append(("uninitialize", threading.get_ident()))
        failures = []
        def work():
            try:
                batch_transport.winhttp_post_batch(URL, {}, b"{}")
            except Exception as exc:
                failures.append(exc)
        with patch.object(batch_transport, "_load_com", return_value=(com, client)):
            worker = threading.Thread(target=work)
            worker.start()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(failures)
        self.assertEqual([name for name, _tid in seen], ["initialize", "send", "uninitialize"])
        self.assertEqual(len({tid for _name, tid in seen}), 1)
        self.assertNotEqual(seen[0][1], threading.get_ident())

    def test_optional_dependency_missing_is_actionable_without_fallback(self):
        with patch.dict("sys.modules", {"pythoncom": None}), \
                patch.object(app.urllib.request, "urlopen") as other_transport:
            with self.assertRaises(batch_transport.BatchTransportError) as caught:
                batch_transport.winhttp_post_batch(URL, {}, b"{}")
        self.assertIn("pywin32", str(caught.exception))
        self.assertFalse(caught.exception.send_started)
        other_transport.assert_not_called()

    def test_wrong_destination_or_encoding_fails_before_com(self):
        with patch.object(batch_transport, "_load_com") as load:
            for url, payload in [("https://example.com/v1/messages/batches", b"{}"),
                                 (URL, b"\xff"), (URL, "not-bytes")]:
                with self.subTest(url=url, payload=payload):
                    with self.assertRaises(batch_transport.BatchTransportError):
                        batch_transport.winhttp_post_batch(url, {}, payload)
            load.assert_not_called()


class TestClaudeNativeBatchIntegration(unittest.TestCase):
    def api(self):
        api = app.ClaudeAPI("offline-secret", "claude-haiku-4-5")
        api.batch_transport = "winhttp"
        return api

    def test_primary_and_followup_models_use_same_selected_native_boundary(self):
        for model in ("claude-haiku-4-5", "claude-sonnet-4-6"):
            with self.subTest(model=model):
                api = self.api()
                api.model_id = model
                requests = [{"custom_id": "synthetic", "params": {"model": model}}]
                with patch.object(batch_transport, "winhttp_post_batch",
                                  return_value=(200, '{"id":"offline"}')) as native, \
                        patch.object(app.urllib.request, "urlopen") as legacy:
                    self.assertEqual(api.submit_batch(requests), {"id": "offline"})
                native.assert_called_once_with(URL, {
                    "content-type": "application/json", "x-api-key": "offline-secret",
                    "anthropic-version": "2023-06-01"},
                    json.dumps({"requests": requests}).encode("utf-8"))
                legacy.assert_not_called()

    def test_native_exception_cannot_fall_back_to_urllib(self):
        api = self.api()
        with patch.object(batch_transport, "winhttp_post_batch",
                          side_effect=batch_transport.BatchTransportError("ambiguous", True)) as native, \
                patch.object(app.urllib.request, "urlopen") as legacy:
            with self.assertRaises(app.APIError) as caught:
                api.submit_batch([])
        self.assertEqual(caught.exception.status, 0)
        native.assert_called_once()
        legacy.assert_not_called()

    def test_http_error_statuses_preserve_semantics_and_are_never_retried(self):
        for status in (302, 400, 401, 403, 413, 429, 500, 529):
            with self.subTest(status=status):
                api = self.api()
                raw = json.dumps({"error": {"type": "synthetic_error",
                                           "message": "offline-secret redaction test"}})
                with patch.object(batch_transport, "winhttp_post_batch", return_value=(status, raw)) as native:
                    with self.assertRaises(app.APIError) as caught:
                        api.submit_batch([])
                self.assertEqual(caught.exception.status, status)
                self.assertNotIn("offline-secret", caught.exception.detail)
                native.assert_called_once()

    def test_credit_error_is_reported_as_credit_exhausted(self):
        api = self.api()
        with patch.object(batch_transport, "winhttp_post_batch", return_value=(
                400, '{"error":{"type":"invalid_request_error","message":"credit balance too low"}}')):
            with self.assertRaises(app.CreditExhausted):
                api.submit_batch([])

    def test_invalid_success_body_is_ambiguous_and_not_retried(self):
        for raw in ("", "not json", "[]"):
            with self.subTest(raw=raw):
                with patch.object(batch_transport, "winhttp_post_batch", return_value=(200, raw)) as native:
                    with self.assertRaises(app.APIError) as caught:
                        self.api().submit_batch([])
                self.assertEqual(caught.exception.status, 0)
                native.assert_called_once()

    def test_read_only_get_retains_urllib_even_when_native_post_is_selected(self):
        api = self.api()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"data":[],"has_more":false}'
        with patch.object(app.urllib.request, "urlopen", return_value=response) as legacy, \
                patch.object(batch_transport, "winhttp_post_batch") as native:
            self.assertEqual(api.list_batches(), {"data": [], "has_more": False})
        legacy.assert_called_once()
        native.assert_not_called()


if __name__ == "__main__":
    unittest.main()
