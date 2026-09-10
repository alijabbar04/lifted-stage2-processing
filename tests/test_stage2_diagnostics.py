import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import stage2_diagnostics
from stage2_diagnostics import Diagnostics, _safe, install


def test_safe_redacts_credentials_and_document_fields():
    assert "secret-value" not in _safe({"token": "secret-value", "document": "private"})
    text = _safe("api_key=abc123 C:\\private\\file.pdf")
    assert "abc123" not in text
    assert "private" not in text


def test_record_is_durable_json_and_bounded(tmp_path):
    diag = Diagnostics(tmp_path, max_bytes=300, backups=1)
    diag.record("window_close", reason="user", prompt="private text")
    log_path = next(tmp_path.glob("stage2-diagnostics-*.log"))
    initial = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
    assert initial["event"] == "window_close"
    assert initial["prompt"] == "<redacted>"
    for index in range(20):
        diag.record("window_configure", index=index, detail="x" * 80)
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert lines
    all_text = "\n".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("stage2-diagnostics.log*"))
    assert any(p.name.endswith(".log.1") for p in tmp_path.iterdir())


def test_install_is_idempotent_and_tk_callback_is_captured(tmp_path):
    first = install(tmp_path)
    second = install(tmp_path)
    assert first is second

    class Root:
        def __init__(self):
            self.callbacks = {}
            self.report_callback_exception = None
        def bind(self, sequence, callback, add=None):
            self.callbacks[sequence] = callback
        def after(self, _delay, callback):
            callback()
            return "timer"
        def after_cancel(self, _timer):
            pass

    root = Root()
    first.attach_tk(root)
    assert callable(root.report_callback_exception)
    first.close()


def test_install_falls_back_when_log_directory_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(stage2_diagnostics, "_DEFAULT", None)
    monkeypatch.setattr(stage2_diagnostics, "Diagnostics", lambda _root: (_ for _ in ()).throw(OSError("disk full")))
    fallback = install(tmp_path / "unavailable")
    assert fallback.__class__.__name__ == "NullDiagnostics"
    fallback.record("ignored")
    fallback.attach_tk(None)
