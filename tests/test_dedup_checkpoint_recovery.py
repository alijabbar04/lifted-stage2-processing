"""Crash-safe deduplication must remain exact across finishing restarts."""
import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from _load_app import load_app


app = load_app()


def make_fixture(tmp_path, copies=3):
    root = tmp_path / "Files"
    worker = root / "Worker"
    worker.mkdir(parents=True)
    duplicate_paths = []
    for index in range(copies):
        suffix = "" if index == 0 else f" ({index})"
        path = worker / f"Passport{suffix}.pdf"
        path.write_bytes(b"same-passport")
        duplicate_paths.append(path)
    unique = worker / "DBS Document.pdf"
    unique.write_bytes(b"unique-dbs")
    records = [
        {"path": path, "name": "Passport", "group": "Crucial",
         "original": path.name, "imgs": [], "text": ""}
        for path in duplicate_paths]
    records.append({"path": unique, "name": "DBS Document", "group": "Crucial",
                    "original": unique.name, "imgs": [], "text": ""})
    rows = [{"path": str(record["path"]),
             "hash": app.file_hash(record["path"]),
             "name": record["name"], "group": record["group"]}
            for record in records]
    state = app.BatchState(root)
    key = str(worker.resolve()).casefold()
    state.data = {"workers": {key: {
        "name": worker.name, "source_path": str(worker),
        "classification_status": "complete", "finishing_status": "in_progress",
        "movement_status": "not_ready", "completed": False,
        "applied_records": copy.deepcopy(rows)}}}
    assert state.save()
    return root, worker, records, rows, state


def engine_for(state):
    engine = object.__new__(app.Engine)
    engine._batch_state = state
    engine._live_state = None
    engine.log = lambda _message: None
    engine.stats = {"duplicates": 0}
    return engine


def prepare_without_delete(engine, worker, records):
    with patch.object(app, "_apply_dedup_transition",
                      side_effect=RuntimeError("synthetic crash before delete")):
        with pytest.raises(RuntimeError, match="synthetic crash"):
            engine._durable_dedup_worker(worker, records)


def state_worker(state, worker):
    return state.data["workers"][str(worker.resolve()).casefold()]


def test_plan_is_durable_before_any_destructive_delete(tmp_path):
    root, worker, records, rows, state = make_fixture(tmp_path)
    engine = engine_for(state)
    prepare_without_delete(engine, worker, records)

    assert all(Path(row["path"]).exists() for row in rows)
    durable = app.BatchState(root)
    saved = state_worker(durable, worker)
    assert saved["dedup_transition"]["status"] == "prepared"
    assert saved["classification_applied_records"] == rows
    assert saved["applied_records"] == rows


def test_prepared_transition_resumes_after_crash_before_delete(tmp_path):
    root, worker, records, rows, state = make_fixture(tmp_path)
    prepare_without_delete(engine_for(state), worker, records)

    resumed_state = app.BatchState(root)
    rebuilt = engine_for(resumed_state)._resume_prepared_dedup_transition(
        worker, records, resumed_state)
    saved = state_worker(app.BatchState(root), worker)

    assert sorted(path.name for path in worker.glob("*.pdf")) == [
        "DBS Document.pdf", "Passport.pdf"]
    assert len(rebuilt) == 2
    assert saved["dedup_transition"]["status"] == "complete"
    assert saved["dedup_transition"]["removed_count"] == 2
    assert saved["classification_applied_records"] == rows
    assert len(saved["applied_records"]) == 2


def test_prepared_transition_resumes_after_partial_deletion(tmp_path):
    root, worker, records, _rows, state = make_fixture(tmp_path, copies=4)
    prepare_without_delete(engine_for(state), worker, records)
    prepared = state_worker(app.BatchState(root), worker)["dedup_transition"]
    already_removed = Path(prepared["groups"][0]["duplicates"][0]["path"])
    already_removed.unlink()

    resumed_state = app.BatchState(root)
    rebuilt = engine_for(resumed_state)._resume_prepared_dedup_transition(
        worker, records, resumed_state)
    saved = state_worker(app.BatchState(root), worker)
    assert len(rebuilt) == 2
    assert saved["dedup_transition"]["removed_count"] == 3
    assert sorted(path.name for path in worker.glob("*.pdf")) == [
        "DBS Document.pdf", "Passport.pdf"]


def test_failed_prepare_save_removes_nothing(tmp_path):
    _root, worker, records, rows, state = make_fixture(tmp_path)
    state.save = lambda: False
    with pytest.raises(app.DurableStateError, match="no file was removed"):
        engine_for(state)._durable_dedup_worker(worker, records)
    assert all(Path(row["path"]).exists() for row in rows)


def test_crash_after_deletes_before_completion_save_resumes(tmp_path):
    root, worker, records, rows, state = make_fixture(tmp_path)
    real_save = state.save
    calls = 0

    def fail_second_save():
        nonlocal calls
        calls += 1
        return real_save() if calls == 1 else False

    state.save = fail_second_save
    with pytest.raises(app.DurableStateError, match="completed on disk"):
        engine_for(state)._durable_dedup_worker(worker, records)
    assert sorted(path.name for path in worker.glob("*.pdf")) == [
        "DBS Document.pdf", "Passport.pdf"]
    durable = app.BatchState(root)
    assert state_worker(durable, worker)["dedup_transition"]["status"] == "prepared"

    current = [record for record in records if Path(record["path"]).exists()]
    engine_for(durable)._resume_prepared_dedup_transition(worker, current, durable)
    final = state_worker(app.BatchState(root), worker)
    assert final["dedup_transition"]["status"] == "complete"
    assert final["classification_applied_records"] == rows


@pytest.mark.parametrize("drift", ["lost-survivor", "new", "changed-duplicate"])
def test_prepared_resume_rejects_loss_new_or_changed_content(tmp_path, drift):
    root, worker, records, _rows, state = make_fixture(tmp_path)
    prepare_without_delete(engine_for(state), worker, records)
    transition = state_worker(app.BatchState(root), worker)["dedup_transition"]
    if drift == "lost-survivor":
        Path(transition["groups"][0]["survivor"]["path"]).unlink()
    elif drift == "new":
        (worker / "Unexpected.pdf").write_bytes(b"new")
    else:
        Path(transition["groups"][0]["duplicates"][0]["path"]).write_bytes(
            b"changed")
    durable = app.BatchState(root)
    with pytest.raises(app.FinishingInputChanged):
        engine_for(durable)._resume_prepared_dedup_transition(
            worker, records, durable)
    assert state_worker(app.BatchState(root), worker)[
        "dedup_transition"]["status"] == "prepared"


def test_full_duplicate_class_retains_exactly_one(tmp_path):
    root, worker, records, _rows, state = make_fixture(tmp_path, copies=5)
    rebuilt, removed = engine_for(state)._durable_dedup_worker(worker, records)
    saved = state_worker(app.BatchState(root), worker)
    assert removed == 4
    assert len([path for path in worker.glob("Passport*.pdf")]) == 1
    assert len(rebuilt) == 2
    assert saved["dedup_transition"]["removed_count"] == 4


def test_ranking_rename_refreshes_survivors_not_classification_provenance(tmp_path):
    root, worker, records, rows, state = make_fixture(tmp_path)
    engine = engine_for(state)
    rebuilt, _removed = engine._durable_dedup_worker(worker, records)
    passport = next(Path(record["path"]) for record in rebuilt
                    if record["name"] == "Passport")
    dated = passport.with_name("Passport - (01-01-2026).pdf")
    passport.rename(dated)
    for record in rebuilt:
        if Path(record["path"]) == passport:
            record["path"] = dated
    engine._checkpoint_dedup_survivors(worker, rebuilt)

    saved = state_worker(app.BatchState(root), worker)
    assert saved["classification_applied_records"] == rows
    assert str(dated.resolve()) in [row["path"] for row in saved["applied_records"]]
    assert str(passport.resolve()) not in [row["path"] for row in saved["applied_records"]]
    restarted = app.BatchState(root)
    again, removed = engine_for(restarted)._durable_dedup_worker(worker, rebuilt)
    assert removed == 0
    assert len(again) == 2


def test_classification_provenance_change_blocks_completed_resume(tmp_path):
    root, worker, records, _rows, state = make_fixture(tmp_path)
    engine_for(state)._durable_dedup_worker(worker, records)
    durable = app.BatchState(root)
    state_worker(durable, worker)["classification_applied_records"][0][
        "name"] = "Changed Classification"
    assert durable.save()
    current = [record for record in records if Path(record["path"]).exists()]
    with pytest.raises(app.FinishingInputChanged,
                       match="classification provenance"):
        engine_for(app.BatchState(root))._durable_dedup_worker(worker, current)


def test_tampered_plan_and_linked_tree_fail_closed(tmp_path):
    root, worker, records, _rows, state = make_fixture(tmp_path)
    prepare_without_delete(engine_for(state), worker, records)
    durable = app.BatchState(root)
    transition = state_worker(durable, worker)["dedup_transition"]
    transition["groups"][0]["duplicates"].pop()
    with pytest.raises(app.FinishingInputChanged, match="digest"):
        engine_for(durable)._resume_prepared_dedup_transition(
            worker, records, durable)


def test_batch_caller_resumes_transition_before_strict_hash_validator():
    source = (Path(__file__).resolve().parents[1] / "src" /
              "Stage2_Processing.pyw").read_text(encoding="utf-8")
    resume = source.index("records = self._resume_prepared_dedup_transition(")
    validate = source.index("self._validate_batch_worker_records(w, records, state)")
    finish = source.index("self._finish_worker(w, records)", validate)
    assert resume < validate < finish


def test_batch_state_save_remains_atomic_writer(tmp_path):
    root, _worker, _records, _rows, state = make_fixture(tmp_path)
    before = json.loads(state.path.read_text(encoding="utf-8"))
    state.data["marker"] = "after"
    assert state.save()
    after = json.loads(state.path.read_text(encoding="utf-8"))
    assert before.get("marker") is None
    assert after["marker"] == "after"
