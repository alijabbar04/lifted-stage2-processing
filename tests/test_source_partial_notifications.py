"""Do not announce partial completion until the actual terminal event."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_source_recovery import app


@pytest.mark.parametrize("status", ["batch_processing_complete_audit_pending",
    "batch_error:archive unavailable", "stopped", "batch_applied:1.0|2.0", "batch_audit_complete"])
def test_partial_notice_requires_terminal_status(status):
    ui = SimpleNamespace(_notify=Mock())
    stats = {"terminal_outcome": "completed_with_exclusions",
             "source_exclusions": {"count": 1, "workers": ["Synthetic worker"]}}
    app.App._notify_done(ui, stats, status)
    partial = [call for call in ui._notify.call_args_list if call.args[0] == "completed_with_exclusions"]
    assert bool(partial) == (status.partition(":")[0] in {"batch_applied", "batch_audit_complete"})
