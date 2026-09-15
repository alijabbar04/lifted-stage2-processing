"""Hidden synthetic Tk smoke test for the delayed-recovery operator controls."""
import time
import tkinter as tk
from unittest.mock import Mock

from test_source_recovery import setup, render, app
from stage2_source_recovery_ui import RecoveryWindow
from test_source_recovery_integration import Provider, engine_for


def test_queue_reopens_masked_complete_and_controls_fit(tmp_path):
    home = tmp_path / "Synthetic home"
    c, paths = setup(home, ["synthetic-password", None])
    c.preflight(render)
    root = tk.Tk()
    root.attributes("-alpha", 0.0)
    root._refresh_run_controls = Mock()
    root._refresh_folder_state = Mock()
    root._recovery_busy = False
    api = Provider()
    window = RecoveryWindow(root, engine_for(home, api, []), app.BatchState,
                            "test", "ui", lambda n: n * .01)
    window.attributes("-alpha", 0.0)
    try:
        deadline = time.monotonic() + 20
        while window.busy and time.monotonic() < deadline:
            root.update()
            time.sleep(.01)
        assert not window.busy
        assert window.controller.summary()["locked"] == 1
        assert len(window.tree.get_children()) == 2
        assert window.password_entry.cget("show")
        assert not window.password.get()
        assert window.controller.data["preflight_complete"]
        api.submit_batch.assert_not_called()
        for width, height in [(1100, 760), (1100, 680)]:
            window.geometry(f"{width}x{height}")
            root.update()
            for control in window.buttons + [window.password_entry, window.cancel_button]:
                assert control.winfo_viewable()
                x = control.winfo_rootx() - window.winfo_rootx()
                y = control.winfo_rooty() - window.winfo_rooty()
                assert 0 <= x and x + control.winfo_width() <= window.winfo_width()
                assert 0 <= y and y + control.winfo_height() <= window.winfo_height()
        window.password.set("synthetic-password")
        assert window.close()
        assert not window.password.get()
        assert paths[0].exists()
    finally:
        if window.winfo_exists():
            window.destroy()
        root.destroy()
