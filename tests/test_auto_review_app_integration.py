"""Main-app contracts for the automatic review hand-off.

These tests intentionally mock the still-separate automatic-review controller.
They exercise the desktop controller's call boundaries, not provider execution.
"""
import ast
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from _load_app import load_app


SOURCE = Path(__file__).resolve().parents[1] / "src" / "Stage2_Processing.pyw"


def method_node(class_name, method_name):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8-sig"))
    cls = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == class_name)
    return next(node for node in cls.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == method_name)


def method_calls(node):
    return [child.func.attr for child in ast.walk(node)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)]


def test_processing_receipt_is_recorded_before_the_post_run_audit():
    run = method_node("Engine", "run")
    calls = method_calls(run)
    assert calls.index("_record_review_processing") < calls.index("_run_post_run_audit")


def test_start_after_scan_uses_only_the_immutable_start_config():
    method = method_node("App", "_start_after_scan")
    mutable_option_reads = []
    for node in ast.walk(method):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and node.args):
            continue
        owner = node.func.value
        if (isinstance(owner, ast.Attribute) and owner.attr == "cfg"
                and isinstance(owner.value, ast.Name) and owner.value.id == "self"):
            key = node.args[0].value if isinstance(node.args[0], ast.Constant) else None
            if key != "run_mode":  # saved only as the next-run UI preference
                mutable_option_reads.append((node.lineno, key))
    assert not mutable_option_reads, (
        "Settings edited during preflight must not change estimates, scope, or "
        f"the engine configuration: {mutable_option_reads}")
    calls = [node for node in ast.walk(method) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr == "_make_engine"]
    assert any(any(keyword.arg == "run_config" and isinstance(keyword.value, ast.Name)
                       and keyword.value.id == "cfg" for keyword in call.keywords)
               for call in calls)


def test_legacy_batch_without_a_saved_auto_review_run_never_launches_review(monkeypatch):
    module = load_app()
    controller = Mock()
    app = SimpleNamespace(_review_run_id="", _review_check_thread=None,
                          _review_events=deque(), _review_busy=False,
                          _get_review_controller=Mock(return_value=controller),
                          _refresh_run_controls=Mock(), set_status=Mock(), _notify=Mock())
    assert module.App._begin_automatic_review(
        app, {"audit_status": "complete"}, "batch_applied") is False
    controller.maybe_launch.assert_not_called()
    app._get_review_controller.assert_not_called()


def test_complete_audit_handoff_launches_once_and_has_no_completion_modal(monkeypatch):
    module = load_app()

    class InlineThread:
        def __init__(self, *, target, daemon):
            self.target = target
            self.daemon = daemon
        def start(self):
            self.target()
        def is_alive(self):
            return False

    monkeypatch.setattr(module.threading, "Thread", InlineThread)
    controller = Mock()
    controller.maybe_launch.return_value = {"state": "launched"}
    app = SimpleNamespace(_review_run_id="run-123", _review_check_thread=None,
                          _review_events=deque(), _review_busy=False,
                          _get_review_controller=Mock(return_value=controller),
                          _refresh_run_controls=Mock(), set_status=Mock(), _notify=Mock(),
                          _show_ai_review_state=Mock())
    assert module.App._begin_automatic_review(app, {"audit_status": "complete"}, None) is True
    controller.maybe_launch.assert_called_once_with("run-123")
    assert app._review_events.pop() == ("run-123", {"state": "launched"})

    # _done_main must return at the automatic hand-off instead of placing an
    # unattended review behind the historical completion information modal.
    complete = SimpleNamespace(worker_thread=None, _recovery_busy=True,
                               _update_stats=Mock(), _refresh_folder_state=Mock(return_value=({}, None)),
                               _refresh_run_controls=Mock(), dashboard=Mock(), _notify_done=Mock(),
                               _closing=False, _begin_automatic_review=Mock(return_value=True),
                               set_progress=Mock(), set_status=Mock())
    modal = Mock()
    monkeypatch.setattr(module.messagebox, "showinfo", modal)
    module.App._done_main(complete, {"audit_status": "complete"}, None)
    complete._begin_automatic_review.assert_called_once_with({"audit_status": "complete"}, None)
    modal.assert_not_called()


def test_review_notifications_use_audit_review_once_per_meaningful_transition():
    module = load_app()
    emitted = []
    app = SimpleNamespace(_review_notification_run_id="", _review_notification_category="",
                          _notify=lambda event, **data: emitted.append((event, data)))
    for state in ("launched", "running", "running", "outputs-awaiting-verification",
                  "needs-attention", "completed"):
        module.App._notify_review_transition(app, "run-1", state)
    assert emitted == [
        ("review_started", {"phase": "audit_review"}),
        ("blocked", {"phase": "audit_review", "reason": "review"}),
        ("review_complete", {"phase": "audit_review"}),
    ]


def test_output_verification_attention_is_not_busy_and_processing_stop_is_disabled():
    module = load_app()
    app = SimpleNamespace(_review_run_id="run-1", _review_events=deque([(
        "run-1", {"state": "outputs-awaiting-verification", "reason": "missing receipt"})]),
        _review_busy=True, _review_check_thread=None, _refresh_run_controls=Mock(),
        set_status=Mock(), _show_ai_review_state=Mock(), _refresh_ai_review_summary=Mock(),
        _notify_review_transition=Mock())
    module.App._poll_ai_review(app)
    assert app._review_busy is False
    app._show_ai_review_state.assert_called_once_with("outputs-awaiting-verification", "missing receipt")

    controls = {name: Mock() for name in ("pick_btn", "start_btn", "flatten_btn", "batch_btn", "stop_btn")}
    review_only = SimpleNamespace(care_home_dir=Path("C:/Care"), _review_busy=True,
                                  _recovery_busy=False, _scanning=False, worker_thread=None,
                                  **controls)
    module.App._refresh_run_controls(review_only, pending={})
    review_only.stop_btn.configure.assert_called_once_with(state="disabled")


def test_settings_save_applies_palette_to_the_live_dashboard():
    module = load_app()
    cfg = {"ui_palette": "A", "fx": 0.79, "notifications": {}}
    dashboard = SimpleNamespace(apply_palette=Mock())
    app = SimpleNamespace(cfg={}, notification_service=SimpleNamespace(configure=Mock()),
                          dashboard=dashboard, _set_palette_globals=Mock(),
                          _refresh_ai_review_summary=Mock(), _refresh_env_banner=Mock())
    module.App._on_settings_saved(app, cfg)
    dashboard.apply_palette.assert_called_once_with("A")
    app._set_palette_globals.assert_called_once_with("A")


def test_settings_refuses_auto_review_without_the_required_accuracy_audit():
    save = method_node("SettingsDialog", "_save")
    calls = [node for node in ast.walk(save) if isinstance(node, ast.Call)]
    assert any(isinstance(node.func, ast.Attribute) and node.func.attr == "auto_review_defaults"
               for node in calls)
    messages = [node.value for node in ast.walk(save)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    assert any("Automatic AI Document Review can run only after a complete Accuracy Audit" in text
               for text in messages)


def test_settings_palette_selector_updates_live_before_save():
    module = load_app()
    master = SimpleNamespace(_set_palette_globals=Mock(),
                             dashboard=SimpleNamespace(apply_palette=Mock()))
    dialog = SimpleNamespace(_palette_labels={"B · Graphite": "B"},
                             palette_var=SimpleNamespace(get=lambda: "B · Graphite"),
                             master=master)
    module.SettingsDialog._preview_palette(dialog)
    master._set_palette_globals.assert_called_once_with("B")
    master.dashboard.apply_palette.assert_called_once_with("B")
    init = method_node("SettingsDialog", "__init__")
    assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and node.func.attr == "bind" and node.args
               and isinstance(node.args[0], ast.Constant)
               and node.args[0].value == "<<ComboboxSelected>>"
               for node in ast.walk(init))


def test_closing_during_review_requires_the_same_safe_close_path_as_processing():
    method = method_node("App", "_close_app")
    mentions_review_busy = any(isinstance(node, ast.Attribute) and node.attr == "_review_busy"
                               for node in ast.walk(method))
    assert mentions_review_busy, (
        "An active AI Document Review must not bypass the safe close/reopen path "
        "just because processing is idle.")


def test_close_review_requires_confirmation_without_cancelling_the_external_session(monkeypatch):
    module = load_app()
    ask = Mock(return_value=True)
    monkeypatch.setattr(module.messagebox, "askyesno", ask)
    engine = SimpleNamespace(stop=Mock())
    app = SimpleNamespace(_review_busy=True, dashboard=SimpleNamespace(is_busy=lambda: False),
                          notification_service=SimpleNamespace(close=Mock()), destroy=Mock(),
                          engine=engine, _closing=False, _diagnostics=Mock())
    module.App._close_app(app)
    ask.assert_called_once()
    engine.stop.assert_not_called()
    app.notification_service.close.assert_called_once()
    app.destroy.assert_called_once()


def test_cancel_option_only_cancels_an_unclaimed_queued_review(monkeypatch):
    module = load_app()
    monkeypatch.setattr(module.messagebox, "askyesno", Mock(return_value=True))
    info, error = Mock(), Mock()
    monkeypatch.setattr(module.messagebox, "showinfo", info)
    monkeypatch.setattr(module.messagebox, "showerror", error)
    controller = SimpleNamespace(
        get_status=Mock(return_value={"state": "ready", "launch_claim": None}),
        cancel_queued=Mock(return_value={"state": "cancelled"}),
    )
    app = SimpleNamespace(_review_run_id="queued-1", care_home_dir=None,
                          _get_review_controller=Mock(return_value=controller), _review_busy=True,
                          _review_events=deque(), _poll_ai_review=Mock())
    module.App._cancel_queued_automatic_review(app)
    controller.cancel_queued.assert_called_once_with("queued-1")
    assert app._review_busy is False
    app._poll_ai_review.assert_called_once_with()

    controller.get_status.return_value = {"state": "running", "launch_claim": {"id": "already"}}
    app._review_busy = True
    module.App._cancel_queued_automatic_review(app)
    assert controller.cancel_queued.call_count == 1
    info.assert_called_once()
    error.assert_not_called()


def test_reopening_a_persisted_review_uses_existing_session_without_launching():
    module = load_app()
    controller = SimpleNamespace(find_runs=Mock(return_value=[{"run_id": "saved-1"}]),
                                 view_existing=Mock())
    app = SimpleNamespace(_review_run_id="", care_home_dir=Path("C:/Care Home"),
                          _get_review_controller=Mock(return_value=controller))
    module.App._view_ai_session(app)
    controller.find_runs.assert_called_once_with(app.care_home_dir)
    controller.view_existing.assert_called_once_with("saved-1")
