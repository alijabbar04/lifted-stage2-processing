"""Direct apply must not finalize lost or corrupted quarantine evidence."""
from pathlib import Path
from unittest.mock import Mock

import pytest

from test_source_recovery_integration import (
    Provider, app, controller, engine_for, pdf, recovery,
)


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_direct_apply_retains_active_state_when_quarantine_archive_unverifiable(tmp_path, damage):
    root = tmp_path / "Synthetic care home"
    pdf(root / "Excluded worker" / "locked.pdf", "later", text="Synthetic excluded evidence")
    api, events = Provider(), []
    engine_for(root, api, events).run_batch_submit()
    c = controller(root)
    ids = list(c.data["records"])
    c.quarantine(ids, c.token(ids))
    archive = Path(c.data["records"][ids[0]]["quarantine_path"])
    if damage == "missing":
        archive.unlink()
    else:
        archive.write_bytes(b"Synthetic archive corruption")

    # Deliberately do not reopen Source recovery or call resume: this is the
    # direct Apply accepted route that previously bypassed archive validation.
    engine = engine_for(root, api, events)
    engine._record_review_processing = Mock()
    engine.run_batch_apply()

    assert app.BatchState(root).exists(), events[-1]
    assert not app.BatchState(root).data.get("processing_complete")
    assert not list(root.glob("*.terminal-*.bak"))
    assert not list(root.glob("_source_exclusions_*.json"))
    engine._record_review_processing.assert_not_called()
    api.submit_batch.assert_not_called()
    api.classify.assert_not_called()


def test_terminal_archive_guard_rejects_out_of_scope_paths_without_reading_them(tmp_path, monkeypatch):
    root = tmp_path / "Synthetic care home"
    pdf(root / "Excluded worker" / "locked.pdf", "later")
    api, events = Provider(), []
    engine_for(root, api, events).run_batch_submit()
    c = controller(root)
    ids = list(c.data["records"])
    c.quarantine(ids, c.token(ids))
    c.data["records"][ids[0]]["quarantine_path"] = str(tmp_path / "outside.bin")
    read_hash = Mock(side_effect=AssertionError("must not read an out-of-scope archive"))
    monkeypatch.setattr(recovery, "digest", read_hash)
    with pytest.raises(recovery.RecoveryError, match="outside"):
        recovery.verify_excluded_archives(c.state)
    read_hash.assert_not_called()
