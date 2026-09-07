"""A read-phase TimeoutError must retry like URLError, bounded, never for batch POSTs.

`urlopen(timeout=...)` wraps a CONNECT timeout in URLError, but a timeout during
`resp.read()` is a bare TimeoutError: it used to bypass the retry entirely, and a
readable document became a real audit's only error row that way.
"""
import io
import json
import urllib.error
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from _load_app import load_app

app = load_app()


class FakeResponse:
    def __init__(self, payload, read_timeouts):
        self._payload = payload
        self._timeouts = read_timeouts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        if self._timeouts["remaining"] > 0:
            self._timeouts["remaining"] -= 1
            raise TimeoutError("The read operation timed out")
        return json.dumps(self._payload).encode("utf-8")


@contextmanager
def fake_transport(payload, *, read_timeouts=0, connect_urlerrors=0):
    state = {"remaining": read_timeouts, "urlerrors": connect_urlerrors,
             "attempts": 0}

    def urlopen(req, timeout=None):
        state["attempts"] += 1
        if state["urlerrors"] > 0:
            state["urlerrors"] -= 1
            raise urllib.error.URLError(TimeoutError("timed out"))
        return FakeResponse(payload, state)

    with patch.object(app.urllib.request, "urlopen", side_effect=urlopen), \
         patch("time.sleep"):
        yield state


def make_api():
    return app.ClaudeAPI("synthetic-key", "synthetic-model")


MESSAGE = {"content": [{"type": "text", "text": "synthetic answer"}],
           "usage": {"input_tokens": 1, "output_tokens": 1}}


def test_post_retries_read_timeout_and_then_succeeds():
    api = make_api()
    with fake_transport(MESSAGE, read_timeouts=1) as state:
        assert api._post("system", [{"type": "text", "text": "x"}]) == "synthetic answer"
    assert state["attempts"] == 2


def test_post_read_timeout_is_bounded_and_fails_as_network_error():
    api = make_api()
    with fake_transport(MESSAGE, read_timeouts=app.ClaudeAPI.MAX_RETRIES + 1) as state:
        with pytest.raises(app.APIError) as err:
            api._post("system", [{"type": "text", "text": "x"}])
    assert state["attempts"] == app.ClaudeAPI.MAX_RETRIES + 1
    assert err.value.status == 0
    assert "timed out" in err.value.message


def test_post_still_retries_connect_urlerror():
    api = make_api()
    with fake_transport(MESSAGE, connect_urlerrors=1) as state:
        assert api._post("system", [{"type": "text", "text": "x"}]) == "synthetic answer"
    assert state["attempts"] == 2


def test_post_never_retries_client_errors():
    api = make_api()
    calls = {"attempts": 0}

    def urlopen(req, timeout=None):
        calls["attempts"] += 1
        raise urllib.error.HTTPError(app.ClaudeAPI.URL, 400, "bad request", {},
                                     io.BytesIO(b'{"error":{"message":"synthetic"}}'))

    with patch.object(app.urllib.request, "urlopen", side_effect=urlopen), \
         patch("time.sleep"):
        with pytest.raises(app.APIError) as err:
            api._post("system", [{"type": "text", "text": "x"}])
    assert calls["attempts"] == 1
    assert err.value.status == 400


def test_http_get_retries_read_timeout_but_batch_post_never_does():
    api = make_api()
    batch = {"id": "batch_synthetic"}
    with fake_transport(batch, read_timeouts=1) as state:
        assert api._http("GET", app.ClaudeAPI.BATCH_URL + "/batch_synthetic") == batch
    assert state["attempts"] == 2
    # a lost batch-creation response is ambiguous: resending could create and
    # bill a duplicate batch, so the read timeout must surface immediately
    api.batch_transport = "urllib"
    with fake_transport(batch, read_timeouts=1) as state:
        with pytest.raises(app.APIError):
            api._http("POST", app.ClaudeAPI.BATCH_URL, {"requests": []})
    assert state["attempts"] == 1
