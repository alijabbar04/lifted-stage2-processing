import sys
import types
import tkinter as tk
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stage2_session_ui import _format_details, _invoke, _observer, _status
from stage2_run_state import RunObserver, RunPublisher


def test_session_status_distinguishes_operation_end_and_failure():
    assert _status({"display_state": "running"}) == "running"
    assert _status({"display_state": "operation-ended"}) == "operation-ended"
    assert _status({"display_state": "owner-ended"}) == "owner-ended"
    assert _status({"display_state": "failed"}) == "failed"
    assert _status({}) == "stale-unverified"


def test_unknown_state_is_not_pipeline_completion():
    assert _status({"display_state": "complete"}) == "stale-unverified"


def test_run_observer_registry_contract_is_used(monkeypatch, tmp_path):
    class FakeObserver:
        def __init__(self, care_home_dir=None):
            self.root = care_home_dir

        def list_runs(self, care_home_dir=None):
            return [{"run_id": "run-1", "display_state": "running"}]

        def get(self, run_id):
            return {"run_id": run_id, "display_state": "operation-ended", "phase": "audit"}

        def read_events(self, run_id, limit=100):
            return [{"event": "heartbeat", "run_id": run_id}]

        def log_folder(self, run_id):
            return str(tmp_path)

    monkeypatch.setitem(sys.modules, "stage2_run_state", types.SimpleNamespace(RunObserver=FakeObserver))
    observer = _observer(None, tmp_path)
    assert _invoke(observer, "list_runs", tmp_path)[0]["run_id"] == "run-1"
    assert _invoke(observer, "get", "run-1")["phase"] == "audit"
    assert _invoke(observer, "read_events", "run-1", limit=10)[0]["event"] == "heartbeat"


def test_real_publisher_observer_registry_round_trip(tmp_path):
    care_home = tmp_path / "Files"
    care_home.mkdir()
    publisher = RunPublisher(care_home, mode="batch", registry_root=tmp_path / "registry", heartbeat_seconds=0.05)
    publisher.update(phase="audit", total=4, completed=2, current_document="")
    publisher.event({"kind": "audit-progress", "phase": "audit", "completed": 2, "total": 4})
    run_id = publisher.run_id
    class Context:
        registry_root = tmp_path / "registry"
    observer = _observer(Context(), care_home)
    assert isinstance(observer, RunObserver)
    listed = observer.list_runs(care_home)
    assert listed and listed[0]["run_id"] == run_id
    assert observer.get(run_id)["phase"] == "audit"
    assert observer.read_events(run_id, limit=10)[-1]["kind"] == "audit-progress"
    displayed = _format_details(observer.get(run_id), observer.read_events(run_id, limit=10))
    assert f"PID: {observer.get(run_id)['owner']['pid']}" in displayed
    assert "Phase: audit" in displayed and "Counts: 2 / 4" in displayed
    assert "Estimated cost: GBP 0" in displayed
    publisher.finish()


def test_refresh_preserves_selected_run(monkeypatch):
    class ListBox:
        def __init__(self): self.selected = (1,); self.selected_after = None
        def curselection(self): return self.selected
        def delete(self, *_args): pass
        def insert(self, *_args): pass
        def selection_set(self, index): self.selected_after = index
        def see(self, _index): pass
    class Observer:
        def list_runs(self, _care_home): return [{"run_id": "a"}, {"run_id": "b"}]
    viewer = object.__new__(__import__("stage2_session_ui").SessionViewer)
    viewer.listbox = ListBox(); viewer.sessions = [{"run_id": "a"}, {"run_id": "b"}]
    viewer.observer = Observer(); viewer.care_home_dir = None
    viewer._show_selected = lambda **_kwargs: None
    viewer._write = lambda _text: None
    viewer.refresh()
    assert viewer.listbox.selected_after == 1


def test_write_preserves_scroll_only_for_same_run_refresh():
    from stage2_session_ui import SessionViewer

    class Text:
        def __init__(self): self.moved = None
        def yview(self): return (0.42, 0.58)
        def configure(self, **_kwargs): pass
        def delete(self, *_args): pass
        def insert(self, *_args): pass
        def yview_moveto(self, value): self.moved = value

    viewer = object.__new__(SessionViewer)
    viewer.details = Text()
    viewer._write("timeline", preserve_scroll=True)
    assert viewer.details.moved == 0.42
    viewer.details.moved = None
    viewer._write("new selection", preserve_scroll=False)
    assert viewer.details.moved is None


def test_real_tk_layout_keeps_footer_inside_window():
    from stage2_session_ui import SessionViewer
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Tk display unavailable")
    root.withdraw()
    class Observer:
        registry_root = None
        def list_runs(self, _care_home): return []
    try:
        for geometry in ("900x600", "780x540"):
            viewer = SessionViewer(root, {"run_observer": Observer()}, None)
            viewer.win.geometry(geometry)
            viewer.win.update_idletasks()
            bottom = viewer.footer.winfo_y() + viewer.footer.winfo_height()
            assert bottom <= viewer.win.winfo_height()
            viewer.win.destroy()
    finally:
        root.destroy()
