"""Supplemental source recovery must replay already-paid duplicate aliases."""
import json
import shutil

from test_source_recovery_integration import (
    Provider, app, controller, engine_for, pdf, recovery, render, submit_selected,
)


def test_supplemental_reprocess_replays_renamed_duplicate_aliases(tmp_path):
    root = tmp_path / "Synthetic care home"
    first = pdf(root / "Mixed worker" / "one.pdf", text="Identical ready evidence")
    shutil.copyfile(first, first.with_name("two.pdf"))
    pdf(root / "Mixed worker" / "later.pdf", "later-password", text="Later evidence")
    api, events = Provider(), []

    def engine():
        instance = engine_for(root, api, events)
        instance.reprocess = True
        return instance

    engine().run_batch_submit()
    c = controller(root)
    ready = [cid for cid, r in c.data["records"].items() if r["state"] == "ready"]
    assert len(ready) == 2
    submit_selected(c, api, ready, partial=True)
    assert api.submit_batch.call_count == 1
    assert len(next(iter(api.chunks.values()))) == 1
    engine().run_batch_apply()

    c = controller(root)
    assert len(recovery.request_aliases(c.state)) == 1
    worker = next(iter(c.state.data["workers"].values()))
    assert not worker["completed"]
    assert len(worker["applied_records"]) == 2
    assert all(row["path"].endswith(".pdf") for row in worker["applied_records"])
    assert not first.exists()  # Apply renamed the original alias paths.

    locked = [cid for cid, r in c.data["records"].items() if r["state"] in recovery.LOCKED]
    c.unlock(locked, "later-password", c.token(locked), render)
    c = controller(root)
    submit_selected(c, api, locked)
    final_engine = engine()
    final_engine.run_batch_apply()

    assert not app.BatchState(root).exists(), events[-1]
    receipt = json.loads(next(root.glob("*.terminal-*.bak")).read_text(encoding="utf-8"))
    assert receipt["terminal_outcome"] == "completed"
    assert all(w["completed"] for w in receipt["workers"].values())
    assert all(r["state"] == "accepted_applied" for r in receipt["source_recovery"]["records"].values())
    assert api.submit_batch.call_count == 2
    purchased = [cid for chunk in api.chunks.values() for cid in chunk]
    assert len(purchased) == len(set(purchased)) == 2
