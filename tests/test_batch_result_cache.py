"""Offline tests for bounded result GETs and durable result caching."""
import json
import io
import tempfile
import types
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from _load_app import load_app

app = load_app()
import batch_result_cache
import batch_transport


RESULTS_URL = "https://api.anthropic.com/v1/messages/batches/b1/results"


def rows(*ids):
    return [{"custom_id": item, "result": {"type": "succeeded"}}
            for item in ids]


class Response:
    def __init__(self, payload, headers=None, timeout=False):
        self.payload = payload
        self.headers = headers or {}
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        if self.timeout:
            raise TimeoutError("synthetic read timeout")
        return self.payload


def test_cache_requires_exact_ids_and_recovers_from_corruption(tmp_path):
    expected = ["a", "b"]
    saved = batch_result_cache.store(tmp_path, "batch-1", expected,
                                     RESULTS_URL, rows("a", "b"))
    assert batch_result_cache.load(tmp_path, "batch-1", expected,
                                   RESULTS_URL) == rows("a", "b")
    assert batch_result_cache.load(tmp_path, "batch-1", ["a"],
                                   RESULTS_URL) is None
    saved.write_text('{"schema":1,"rows":[', encoding="utf-8")
    assert batch_result_cache.load(tmp_path, "batch-1", expected,
                                   RESULTS_URL) is None


def test_cache_rejects_duplicate_or_partial_rows(tmp_path):
    with pytest.raises(batch_result_cache.ResultCacheError):
        batch_result_cache.store(tmp_path, "batch-1", ["a", "b"],
                                 RESULTS_URL, rows("a", "a"))
    with pytest.raises(batch_result_cache.ResultCacheError):
        batch_result_cache.store(tmp_path, "batch-1", ["a", "b"],
                                 RESULTS_URL, rows("a"))


def test_batch_results_retries_read_timeout_and_retry_after(tmp_path):
    api = app.ClaudeAPI("synthetic-key", "synthetic-model")
    payload = ("\n".join(json.dumps(row) for row in rows("a"))
               + "\n").encode("utf-8")
    responses = [Response(b"", timeout=True),
                 Response(payload, headers={"Retry-After": "2"})]
    retries = []
    stops = []
    with patch.object(app.urllib.request, "urlopen",
                      side_effect=lambda *_args, **_kw: responses.pop(0)), \
            patch.object(app.time, "sleep") as sleep:
        assert api.batch_results(
            RESULTS_URL,
            on_retry=lambda *args: retries.append(args),
            stop_check=lambda: stops.append(True)) == rows("a")
    assert sleep.call_count == 1
    assert retries and retries[0][0] == 1
    assert len(stops) == 3


def test_batch_results_honors_bounded_retry_after_for_transient_http():
    api = app.ClaudeAPI("synthetic-key", "synthetic-model")
    payload = ("\n".join(json.dumps(row) for row in rows("a"))
               + "\n").encode("utf-8")
    error = urllib.error.HTTPError(
        RESULTS_URL, 503, "busy", {"Retry-After": "9999"},
        io.BytesIO(b'{"error":{"message":"temporary"}}'))
    response = Response(payload)
    with patch.object(app.urllib.request, "urlopen",
                      side_effect=[error, response]), \
            patch.object(app.time, "sleep") as sleep:
        assert api.batch_results(RESULTS_URL) == rows("a")
    assert sleep.call_args.args[0] == app.ClaudeAPI.RESULT_RETRY_MAX_DELAY


def test_batch_results_fails_closed_on_malformed_jsonl():
    api = app.ClaudeAPI("synthetic-key", "synthetic-model")
    response = Response(b'{"custom_id":"a"}\nnot-json\n')
    with patch.object(app.urllib.request, "urlopen", return_value=response):
        with pytest.raises(app.APIError) as caught:
            api.batch_results(RESULTS_URL)
    assert caught.value.status == 0
    assert "malformed" in caught.value.message


def test_engine_download_uses_cache_and_never_accepts_partial_set(tmp_path):
    engine = app.Engine.__new__(app.Engine)
    engine.dir = Path(tmp_path)
    engine._phase = Mock()
    engine._phase_progress = Mock()
    api = types.SimpleNamespace(batch_results=Mock(return_value=iter(rows("a", "b"))))
    batches = [{"id": "batch-1", "n": 2, "results_url": RESULTS_URL}]
    mapping = {"batch-1": ["a", "b"]}
    assert engine._download_batch_results(api, batches, mapping) == {
        "a": {"type": "succeeded"}, "b": {"type": "succeeded"}}
    assert api.batch_results.call_count == 1
    api.batch_results.reset_mock()
    assert engine._download_batch_results(api, batches, mapping)
    api.batch_results.assert_not_called()
    with pytest.raises(app.APIError):
        engine._download_batch_results(
            api, batches, {"batch-1": ["a", "b", "c"]})


def _legacy_engine(tmp_path):
    engine = app.Engine.__new__(app.Engine)
    engine.dir = Path(tmp_path)
    engine._phase = Mock()
    engine._phase_progress = Mock()
    return engine


def test_legacy_multiple_idless_batches_resolve_only_after_complete_union(tmp_path):
    engine = _legacy_engine(tmp_path)
    batches = [
        {"id": "legacy-1", "n": 2, "results_url": RESULTS_URL + "/1"},
        {"id": "legacy-2", "n": 2, "results_url": RESULTS_URL + "/2"},
    ]
    api = types.SimpleNamespace(batch_results=Mock(side_effect=[
        iter(rows("a", "b")), iter(rows("c", "d"))]))
    result = engine._download_batch_results(
        api, batches, {"legacy-1": None, "legacy-2": None},
        ["a", "b", "c", "d"])
    assert set(result) == {"a", "b", "c", "d"}
    assert api.batch_results.call_count == 2

    reopened = _legacy_engine(tmp_path)
    second_api = types.SimpleNamespace(batch_results=Mock())
    assert set(reopened._download_batch_results(
        second_api, batches, {"legacy-1": None, "legacy-2": None},
        ["a", "b", "c", "d"])) == {"a", "b", "c", "d"}
    second_api.batch_results.assert_not_called()


def test_legacy_cache_corruption_refetches_only_one_batch(tmp_path):
    engine = _legacy_engine(tmp_path)
    batches = [
        {"id": "legacy-1", "n": 2, "results_url": RESULTS_URL + "/1"},
        {"id": "legacy-2", "n": 2, "results_url": RESULTS_URL + "/2"},
    ]
    first_api = types.SimpleNamespace(batch_results=Mock(side_effect=[
        iter(rows("a", "b")), iter(rows("c", "d"))]))
    engine._download_batch_results(
        first_api, batches, {"legacy-1": None, "legacy-2": None},
        ["a", "b", "c", "d"])
    batch_result_cache.cache_path(tmp_path, "legacy-1").write_text(
        '{"schema":1,"rows":', encoding="utf-8")
    reopened = _legacy_engine(tmp_path)
    second_api = types.SimpleNamespace(batch_results=Mock(return_value=iter(rows("a", "b"))))
    reopened._download_batch_results(
        second_api, batches, {"legacy-1": None, "legacy-2": None},
        ["a", "b", "c", "d"])
    second_api.batch_results.assert_called_once()


@pytest.mark.parametrize("bad_rows", [
    rows("a"),                         # dropped ID
    rows("a", "b", "b"),              # duplicate ID
    rows("a", "b", "foreign"),       # foreign plus wrong count
])
def test_legacy_idless_batches_reject_incomplete_or_foreign_union(tmp_path, bad_rows):
    engine = _legacy_engine(tmp_path)
    batches = [
        {"id": "legacy-1", "n": 2, "results_url": RESULTS_URL + "/1"},
        {"id": "legacy-2", "n": 2, "results_url": RESULTS_URL + "/2"},
    ]
    api = types.SimpleNamespace(batch_results=Mock(side_effect=[
        iter(bad_rows), iter(rows("c", "d"))]))
    with pytest.raises(app.APIError):
        engine._download_batch_results(
            api, batches, {"legacy-1": None, "legacy-2": None},
            ["a", "b", "c", "d"])


def test_known_batch_rejects_swapped_foreign_rows(tmp_path):
    engine = _legacy_engine(tmp_path)
    batches = [
        {"id": "known-1", "n": 2, "results_url": RESULTS_URL + "/1"},
        {"id": "known-2", "n": 2, "results_url": RESULTS_URL + "/2"},
    ]
    api = types.SimpleNamespace(batch_results=Mock(side_effect=[
        iter(rows("c", "d")), iter(rows("a", "b"))]))
    with pytest.raises(app.APIError):
        engine._download_batch_results(
            api, batches,
            {"known-1": ["a", "b"], "known-2": ["c", "d"]},
            ["a", "b", "c", "d"])


def test_cache_rejects_id_only_result_row(tmp_path):
    with pytest.raises(batch_result_cache.ResultCacheError):
        batch_result_cache.store(tmp_path, "batch-id-only", ["a"], RESULTS_URL,
                                 [{"custom_id": "a"}])


def test_download_checks_stop_between_batches(tmp_path):
    engine = _legacy_engine(tmp_path)
    engine._stop = object()
    engine._check_stop = Mock(side_effect=[None, app.StopRequested()])
    batches = [
        {"id": "stop-1", "n": 1, "results_url": RESULTS_URL + "/1"},
        {"id": "stop-2", "n": 1, "results_url": RESULTS_URL + "/2"},
    ]
    api = types.SimpleNamespace(batch_results=Mock(return_value=iter(rows("a"))))
    with pytest.raises(app.StopRequested):
        engine._download_batch_results(api, batches,
                                       {"stop-1": ["a"], "stop-2": ["b"]},
                                       ["a", "b"])
    api.batch_results.assert_called_once()


def test_winhttp_nested_excepinfo_is_numeric_and_redacted():
    com = types.SimpleNamespace(
        COINIT_APARTMENTTHREADED=2, DISPATCH_PROPERTYPUT=4,
        VT_VOID=24, VT_I4=3, VT_VARIANT=12, VT_ARRAY=8192, VT_UI1=17,
        CoInitializeEx=Mock(), CoUninitialize=Mock())
    request = Mock()
    request._oleobj_.GetIDsOfNames.return_value = 6
    error = RuntimeError("secret body")
    error.args = (-2147352567, "Exception occurred.",
                  (0, "WinHTTP", "secret response", None, 0, -2147012894), None)
    request.Send.side_effect = error
    client = types.SimpleNamespace(Dispatch=Mock(return_value=request),
                                   VARIANT=Mock())
    with patch.object(batch_transport, "_load_com", return_value=(com, client)):
        with pytest.raises(batch_transport.BatchTransportError) as caught:
            batch_transport.winhttp_post_batch(
                "https://api.anthropic.com/v1/messages/batches", {}, b"{}")
    text = str(caught.value)
    assert "EXCEPINFO_HRESULT" in text
    assert "secret" not in text
