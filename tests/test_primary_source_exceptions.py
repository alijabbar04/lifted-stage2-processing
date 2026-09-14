"""Synthetic coverage for durable provider-free primary source outcomes."""

import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from _load_app import load_app


app = load_app()


def _fixture(tmp_path):
    worker = tmp_path / "Worker"
    worker.mkdir()
    source = worker / "damaged.pdf"
    source.write_bytes(b"synthetic source")
    digest = app.file_hash(source)
    cid = f"{digest[:56]}-000"
    state = app.BatchState(tmp_path)
    state.init("Synthetic", "claude-haiku-4-5", 1.0,
               {"post_run_audit": False})
    state.data["primary_inventory"] = {
        cid: {"path": str(source), "worker": worker.name,
              "worker_dir": str(worker), "fhash": digest, "pages": 0}}
    state.data["requests"] = {}
    state.save()
    engine = app.Engine.__new__(app.Engine)
    engine.dir = tmp_path
    return engine, state, worker, source, cid, digest


def test_unreadable_exception_is_hash_bound_and_not_a_request(tmp_path):
    engine, state, worker, source, cid, digest = _fixture(tmp_path)
    engine._record_primary_source_exception(
        state, cid, state.data["primary_inventory"][cid],
        "unreadable_source", "no renderable page images or extracted text")
    saved = app.BatchState(tmp_path).data
    record = saved["primary_render_exclusions"][cid]
    assert record["schema"] == "stage2-primary-source-exception/v1"
    assert record["fhash"] == digest
    assert record["provider_request_built"] is False
    assert record["worker_completion_allowed"] is False
    assert app.Engine._primary_excluded_ids(app.BatchState(tmp_path)) == {cid}


def test_exclusion_cannot_be_added_to_requests_or_accepted_batch(tmp_path):
    engine, state, worker, source, cid, digest = _fixture(tmp_path)
    engine._record_primary_source_exception(
        state, cid, state.data["primary_inventory"][cid], "unreadable_source")
    state = app.BatchState(tmp_path)
    state.data["requests"][cid] = dict(state.data["primary_inventory"][cid])
    with pytest.raises(RuntimeError, match="submitted request"):
        app.Engine._primary_excluded_ids(state)
    state.data["requests"].pop(cid)
    state.data["batches"] = [{"id": "b", "n": 1, "request_ids": [cid]}]
    with pytest.raises(RuntimeError, match="accepted batch"):
        app.Engine._primary_excluded_ids(state)


def test_loaded_exclusion_cannot_overlap_ambiguous_submission_marker(tmp_path):
    engine, state, worker, source, cid, digest = _fixture(tmp_path)
    engine._record_primary_source_exception(
        state, cid, state.data["primary_inventory"][cid], "unreadable_source")
    state = app.BatchState(tmp_path)
    state.data["primary_submission"] = {
        "status": "ambiguous", "request_identities": [cid]}
    state.save()
    with pytest.raises(RuntimeError, match="ambiguous submission"):
        app.Engine._primary_excluded_ids(app.BatchState(tmp_path))


def test_changed_source_blocks_recovery_even_with_saved_exception(tmp_path):
    engine, state, worker, source, cid, digest = _fixture(tmp_path)
    engine._record_primary_source_exception(
        state, cid, state.data["primary_inventory"][cid], "unreadable_source")
    source.write_bytes(b"changed after exception")
    state = app.BatchState(tmp_path)
    with pytest.raises(RuntimeError, match="missing, moved or changed"):
        engine._primary_recovery_inventory(state)


def test_empty_worker_with_prior_inventory_is_not_no_documents_supplied(tmp_path):
    engine, state, worker, source, cid, digest = _fixture(tmp_path)
    source.unlink()
    key = str(worker.resolve()).casefold()
    state.data["workers"] = {key: {"source_path": str(worker),
                                   "applied_records": [],
                                   "classification_status": "complete"}}
    state.save()
    with pytest.raises(app.FinishingInputChanged, match="saved inventory"):
        engine._validate_batch_worker_records(worker, [], app.BatchState(tmp_path))


def test_literal_empty_requires_explicit_typed_approval(tmp_path):
    engine, state, worker, source, cid, digest = _fixture(tmp_path)
    source.unlink()
    key = str(worker.resolve()).casefold()
    state.data["primary_inventory"] = {}
    state.data["workers"] = {key: {"source_path": str(worker),
                                   "applied_records": [],
                                   "classification_status": "complete"}}
    state.save()
    with pytest.raises(app.FinishingInputChanged, match="approved"):
        engine._validate_batch_worker_records(worker, [], app.BatchState(tmp_path))
    assert app.BatchState(tmp_path).data["workers"][key]["source_outcome"] == \
        "no_documents_supplied"
    state = app.BatchState(tmp_path)
    state.data["workers"][key]["approved_empty_outcome"] = {
        "reason": "pre-existing empty source folder",
        "user_authorized": True,
        "approval_ts": "2026-09-14T00:00:00+00:00",
    }
    state.save()
    state = app.BatchState(tmp_path)
    engine._validate_batch_worker_records(worker, [], state)
    assert state.data["workers"][key]["completion_outcome"] == "no_documents_supplied"


def test_recovery_records_new_unrenderable_and_does_not_submit(tmp_path):
    engine, state, worker, source, cid, digest = _fixture(tmp_path)
    engine.api = Mock()
    engine.api.classify_payload = Mock()
    engine.api.build_batch_request = Mock()
    engine._check_stop = lambda: None
    engine.set_status = lambda *args: None
    engine._phase_progress = lambda *args, **kwargs: None
    engine.set_progress = lambda *args: None
    engine.log = lambda *args: None
    engine._batch_classification_view = lambda path: ([], "", [0], 1, False)
    engine.kb = Mock()
    engine.kb.vocabulary_block.return_value = "synthetic"
    engine._resume_primary_inventory(state, state.data["primary_inventory"], set())
    saved = app.BatchState(tmp_path).data
    assert cid in saved["primary_render_exclusions"]
    assert saved["primary_submission_complete"] is True
    engine.api.submit_batch.assert_not_called()
