"""Offline batch cohort and automatic-review restart binding regressions."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from _load_app import load_app
from test_release_blockers_v131 import FIXTURE, make_engine


app = load_app()


class EndedBatchAPI:
    model_id = "claude-haiku-4-5"
    api_key = "offline"
    in_tokens = 0
    out_tokens = 0

    def __init__(self, pending=False):
        self.pending = pending
        self.submit_batch = Mock()
        self.get_batch = Mock(side_effect=self._get_batch)

    def _get_batch(self, batch_id):
        return {
            "id": batch_id,
            "processing_status": "in_progress" if self.pending else "ended",
            "results_url": None if self.pending else "offline-results",
            "request_counts": {
                "processing": 1 if self.pending else 0,
                "succeeded": 0 if self.pending else 100,
            },
        }

    def batch_results(self, _url):
        yield from self.results

    @staticmethod
    def classify_payload(vocab, _imgs, text, **_kwargs):
        return "system " + vocab, [{"type": "text", "text": text}], 300

    def build_batch_request(self, custom_id, system, blocks, max_tokens):
        return {"custom_id": custom_id,
                "params": {"model": self.model_id, "system": system,
                           "messages": blocks, "max_tokens": max_tokens}}

    @staticmethod
    def _json_from(raw):
        return json.loads(raw)


def workers(root, count):
    result = []
    for index in range(count):
        worker = Path(root) / f"Worker {index:02d}"
        worker.mkdir()
        shutil.copy2(FIXTURE, worker / "doc.pdf")
        result.append(worker)
    return result


def completed_result():
    return {
        "type": "succeeded",
        "message": {
            "usage": {"input_tokens": 10, "output_tokens": 2},
            "content": [{"type": "text", "text": json.dumps({
                "match": True, "name": "Passport",
                "group": "Crucial", "confidence": 99,
            })}],
        },
    }


def saved_apply_state(root, selected, *, include_scope=True):
    state = app.BatchState(root)
    state.init("Synthetic", "claude-haiku-4-5", 1.0,
               {"post_run_audit": False, "move_mode": False})
    if include_scope:
        state.data["submitted_worker_scope"] = [
            {"name": worker.name, "source_path": str(worker.resolve())}
            for worker in selected]
    ids = []
    for index, worker in enumerate(selected):
        path = worker / "doc.pdf"
        digest = app.file_hash(path)
        custom_id = f"request-{index}"
        state.add_request(custom_id, path, worker, digest, 1)
        ids.append(custom_id)
    state.add_batch("primary", len(ids), "in_progress", request_ids=ids)
    state.data["primary_submission_complete"] = True
    state.save()
    return state, ids


def run_apply(root, api, applied):
    statuses = []
    engine = make_engine(
        root, api, lambda _stats, status: statuses.append(status))

    def apply_one(worker, path, _parsed, _digest, vocab, records, **_kwargs):
        applied.append(worker.name)
        records.append({"path": path, "name": "Passport",
                        "group": "Crucial", "original": path.name,
                        "imgs": [], "text": ""})
        return vocab

    with patch.object(engine, "_apply_classification", side_effect=apply_one), \
            patch.object(engine, "_finish_worker"), \
            patch.object(engine, "_maybe_fix_rotation", return_value=None), \
            patch.object(engine, "_record_roster_handover"), \
            patch.object(engine, "_run_post_run_audit"):
        engine.run_batch_apply()
    return engine, statuses


class TestBatchWorkerScope(unittest.TestCase):
    def test_submit_persists_review_id_and_exact_five_before_failed_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            all_workers = workers(root, 20)
            api = EndedBatchAPI()
            api.submit_batch.side_effect = app.APIError(
                0, "synthetic ambiguous submission")
            statuses = []
            engine = make_engine(
                root, api, lambda _stats, status: statuses.append(status))
            engine.max_workers = 5
            engine._review_run_id = "review-run-five"
            with patch.object(
                    engine, "_batch_classification_view",
                    return_value=(["image"], "text", [0], 1, False)):
                engine.run_batch_submit()

            saved = app.BatchState(root).data
            self.assertEqual(saved["auto_review_run_id"], "review-run-five")
            self.assertEqual(
                [item["name"] for item in saved["submitted_worker_scope"]],
                [worker.name for worker in all_workers[:5]])
            self.assertEqual(saved["primary_submission"]["status"], "ambiguous")
            self.assertEqual(api.submit_batch.call_count, 1)
            self.assertTrue(statuses[-1].startswith("batch_submit_failed:"))

            # Reopening the app binds only the marker saved with this batch.
            probe = SimpleNamespace(
                care_home_dir=root, _review_run_id="",
                _get_review_controller=lambda: Mock(),
                _refresh_ai_review_summary=Mock())
            app.App._restore_ai_review(probe, saved)
            self.assertEqual(probe._review_run_id, "review-run-five")

    def test_twenty_folder_root_applies_only_persisted_five(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            all_workers = workers(root, 20)
            state, ids = saved_apply_state(root, all_workers[:5])
            api = EndedBatchAPI()
            api.results = [
                {"custom_id": custom_id, "result": completed_result()}
                for custom_id in ids]
            applied = []
            engine, statuses = run_apply(root, api, applied)

            self.assertEqual(applied, [worker.name for worker in all_workers[:5]])
            self.assertEqual(
                [worker.name for worker in engine._audit_worker_dirs],
                [worker.name for worker in all_workers[:5]])
            self.assertEqual(engine.stats["workers"], 5)
            self.assertTrue(statuses[-1].startswith("batch_applied:"))
            self.assertTrue(all((worker / "doc.pdf").is_file()
                                for worker in all_workers[5:]))
            self.assertFalse(state.path.exists())

    def test_interrupted_apply_skips_completed_scope_members_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selected = workers(root, 5)
            state, ids = saved_apply_state(root, selected)
            for worker in selected[:2]:
                key = str(worker.resolve()).casefold()
                state.data["workers"][key] = {
                    "name": worker.name, "source_path": str(worker),
                    "final_path": str(worker), "completed": True,
                    "classification_status": "complete",
                    "finishing_status": "complete",
                    "movement_status": "disabled",
                }
            state.save()
            api = EndedBatchAPI()
            api.results = [
                {"custom_id": custom_id, "result": completed_result()}
                for custom_id in ids]
            applied = []
            engine, statuses = run_apply(root, api, applied)

            self.assertEqual(applied, [worker.name for worker in selected[2:]])
            self.assertEqual(
                [worker.name for worker in engine._audit_worker_dirs],
                [worker.name for worker in selected])
            self.assertTrue(statuses[-1].startswith("batch_applied:"))

    def test_legacy_scope_is_inferred_only_from_saved_requests_and_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            all_workers = workers(root, 4)
            state, _ids = saved_apply_state(
                root, all_workers[:2], include_scope=False)
            api = EndedBatchAPI(pending=True)
            api.results = []
            statuses = []
            engine = make_engine(
                root, api, lambda _stats, status: statuses.append(status))
            engine.run_batch_apply()

            saved = app.BatchState(root).data
            self.assertEqual(
                [item["name"] for item in saved["submitted_worker_scope"]],
                [worker.name for worker in all_workers[:2]])
            self.assertTrue(statuses[-1].startswith("batch_pending:"))
            self.assertEqual(api.get_batch.call_count, 1)

    def test_legacy_state_without_scope_evidence_fails_before_poll_or_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workers(root, 4)
            state = app.BatchState(root)
            state.init("Legacy", "claude-haiku-4-5", 1.0, {})
            state.add_batch("primary", 1, "in_progress")
            state.data["primary_submission_complete"] = True
            state.save()
            api = EndedBatchAPI()
            api.results = []
            statuses = []
            engine = make_engine(
                root, api, lambda _stats, status: statuses.append(status))
            engine.run_batch_apply()

            self.assertTrue(statuses[-1].startswith("batch_scope_invalid:"))
            api.get_batch.assert_not_called()
            self.assertTrue(state.path.exists())
            self.assertNotIn(
                "submitted_worker_scope", app.BatchState(root).data)


if __name__ == "__main__":
    unittest.main()
