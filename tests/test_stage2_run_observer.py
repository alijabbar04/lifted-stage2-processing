import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import stage2_run_observer as module


class Engine:
    def __init__(self, path):
        self.dir = path
        self.stats = {"workers_done": 2}
        self.move_dest = None
        for name in ("set_status", "set_progress", "set_preview", "on_cost", "on_done"):
            setattr(self, name, Mock())


def test_observation_restores_callbacks_and_never_claims_pipeline_completion(monkeypatch, tmp_path):
    publisher = Mock()
    monkeypatch.setattr(module, "RunPublisher", Mock(return_value=publisher))
    engine = Engine(tmp_path)
    original = engine.set_status
    @module.observe_engine_operation
    def run_batch_apply(self):
        self.set_status("Applying")
        self.set_progress(2, 5)
        self.set_preview(b"NEVER-PERSIST-IMAGE", "example.pdf")
        self.on_cost(1.23, 300)
        self.on_done(self.stats, "batch_wait")
        return "native-result"
    assert run_batch_apply(engine) == "native-result"
    assert engine.set_status is original
    original.assert_called_once_with("Applying")
    publisher.finish.assert_called_once_with(outcome="batch_wait", error="")
    assert "NEVER-PERSIST-IMAGE" not in str(publisher.mock_calls)
    assert engine._run_publisher is None


def test_publisher_failures_do_not_change_native_result_or_exception(monkeypatch, tmp_path):
    publisher = Mock()
    publisher.update.side_effect = OSError("disk full")
    publisher.finish.side_effect = OSError("disk full")
    monkeypatch.setattr(module, "RunPublisher", Mock(return_value=publisher))
    engine = Engine(tmp_path)
    original = engine.set_status
    @module.observe_engine_operation
    def run(self):
        self.set_status("working")
        raise ValueError("native failure")
    with pytest.raises(ValueError, match="native failure"):
        run(engine)
    assert engine.set_status is original
    assert engine._run_publisher is None


def test_readonly_recovery_and_nested_operation_do_not_publish(monkeypatch, tmp_path):
    factory = Mock()
    monkeypatch.setattr(module, "RunPublisher", factory)
    @module.observe_engine_operation
    def recover_primary_submission(self, allow_resubmit=False):
        return allow_resubmit
    engine = Engine(tmp_path)
    assert recover_primary_submission(engine) is False
    factory.assert_not_called()
    engine._run_publisher = Mock()
    assert recover_primary_submission(engine, True) is True
    factory.assert_not_called()


def test_registry_start_failure_is_not_processing_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(module, "RunPublisher", Mock(side_effect=OSError("unavailable")))
    @module.observe_engine_operation
    def run(self):
        return 42
    assert run(Engine(tmp_path)) == 42


def test_core_external_lock_bypass_retains_observer():
    import ast
    import functools
    source = (Path(__file__).resolve().parents[1] / "src" / "Stage2_Processing.pyw").read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_care_home_writer_operation")
    calls = []
    def observe(method):
        @functools.wraps(method)
        def observed(self, *args, **kwargs):
            calls.append("observed")
            return method(self, *args, **kwargs)
        return observed
    namespace = {"functools": functools, "observe_engine_operation": observe}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "core-decorator", "exec"), namespace)
    @namespace["_care_home_writer_operation"]
    def run_batch_apply(self):
        return "existing external locks"
    assert run_batch_apply.__wrapped__(object()) == "existing external locks"
    assert calls == ["observed"]


@pytest.mark.parametrize("choice", ["minimise", "cancel", "stop"])
def test_core_busy_close_distinguishes_minimise_cancel_and_safe_stop(monkeypatch, choice):
    from types import SimpleNamespace
    import ast
    import stage2_session_ui
    source = (Path(__file__).resolve().parents[1] / "src" / "Stage2_Processing.pyw").read_text(encoding="utf-8-sig")
    cls = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == "App")
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_close_app")
    namespace = {}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "core-close", "exec"), namespace)
    monkeypatch.setattr(stage2_session_ui, "ask_close_running", Mock(return_value=choice))
    app = SimpleNamespace(_review_busy=False, _diagnostics=Mock(),
        dashboard=SimpleNamespace(is_busy=lambda: True), iconify=Mock(),
        engine=Mock(), notification_service=Mock(), destroy=Mock(), _closing=False,
        _scan_cancel=Mock(), _unknown_event=Mock(), after=Mock(), _close_when_idle=Mock())
    namespace["_close_app"](app)
    app.destroy.assert_not_called()
    app.notification_service.close.assert_not_called()
    if choice == "minimise":
        app.iconify.assert_called_once()
        app.engine.stop.assert_not_called()
        app.after.assert_not_called()
    elif choice == "cancel":
        app.iconify.assert_not_called()
        app.engine.stop.assert_not_called()
        app.after.assert_not_called()
    else:
        app.engine.stop.assert_called_once()
        app._scan_cancel.set.assert_called_once()
        app.after.assert_called_once_with(500, app._close_when_idle)
