"""Crash-safety and fail-closed regression tests for v1.3.1 blockers."""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from _load_app import load_app


app = load_app()
FIXTURE = (Path(__file__).parent / "fixtures" / "orientation"
           / "known_text.pdf")


class StubKB:
    def vocabulary_block(self):
        return "Passport"

    def canonical_name(self, name):
        return "Passport" if str(name).casefold() == "passport" else None

    def group_of(self, name):
        return "Crucial" if name == "Passport" else "Other"


class SubmitAPI:
    model_id = "claude-haiku-4-5"
    api_key = "offline"
    in_tokens = 0
    out_tokens = 0

    def __init__(self, error=None):
        self.submit_batch = Mock(side_effect=error)
        self.classify = Mock()

    def classify_payload(self, vocab, imgs, text, **_kwargs):
        return "system " + vocab, [{"type": "text", "text": text}], 300

    def build_batch_request(self, custom_id, system, blocks, max_tokens):
        return {"custom_id": custom_id,
                "params": {"model": self.model_id, "system": system,
                           "messages": blocks, "max_tokens": max_tokens}}


def make_engine(root, api, on_done=None):
    return app.Engine(
        Path(root), StubKB(), api, "Synthetic Home",
        log=lambda _message: None, set_status=lambda _message: None,
        set_progress=lambda *_args: None, set_preview=lambda *_args: None,
        ask_unknown=lambda *_args: None, on_cost=lambda *_args: None,
        on_done=on_done or (lambda *_args: None),
        convert_pdf=False, bundle_split=False, orientation_mode="off",
        post_run_audit=False, max_budget_gbp=35.0)


class TestPrimarySubmissionSafety(unittest.TestCase):

    def test_ambiguous_primary_state_exists_and_blocks_live_processing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "Worker"
            worker.mkdir()
            shutil.copy2(FIXTURE, worker / "doc.pdf")
            state = app.BatchState(root)
            state.init("Home", "claude-haiku-4-5", 1.0, {})
            state.data["primary_submission"] = {
                "status": "ambiguous", "planned_chunk_id": "primary-0001",
                "request_identities": ["request-1"],
                "submission_started": True, "attempt_id": "attempt-1"}
            state.save()
            self.assertTrue(app.BatchState(root).exists())

            api = SubmitAPI()
            statuses = []
            engine = make_engine(root, api,
                                 lambda _stats, status: statuses.append(status))
            engine.run()
            self.assertEqual(statuses, ["live_blocked_by_batch"])
            api.classify.assert_not_called()
            api.submit_batch.assert_not_called()

    def test_primary_post_plan_is_persisted_before_ambiguous_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "Worker"
            worker.mkdir()
            shutil.copy2(FIXTURE, worker / "doc.pdf")
            api = SubmitAPI(app.APIError(0, "network outcome unknown"))
            statuses = []
            engine = make_engine(root, api,
                                 lambda _stats, status: statuses.append(status))
            with patch.object(engine, "_batch_classification_view",
                              return_value=(["image"], "text", [0], 1, False)):
                engine.run_batch_submit()
            self.assertEqual(api.submit_batch.call_count, 1)
            state = app.BatchState(root)
            marker = state.data["primary_submission"]
            self.assertEqual(marker["status"], "ambiguous")
            self.assertEqual(marker["planned_chunk_id"], "primary-0001")
            self.assertEqual(len(marker["request_identities"]), 1)
            self.assertTrue(marker["submission_started"])
            self.assertTrue(marker["attempt_id"])
            self.assertTrue(state.exists())
            self.assertTrue(statuses[-1].startswith("batch_submit_failed:"))


class TestFinishingPersistence(unittest.TestCase):

    @staticmethod
    def _ledger_engine(root, api, state):
        engine = app.Engine.__new__(app.Engine)
        engine.dir = Path(root)
        engine.api = api
        engine.escalation_api = None
        engine._batch_state = state
        engine._committed_batch_cost_gbp = 0.0
        engine._committed_batch_tokens = 0
        costs = state.data.get("costs") or {}
        engine._persisted_live_cost_gbp = float(
            costs.get("live_actual_gbp", 0) or 0)
        engine._persisted_live_tokens = int(costs.get("live_tokens", 0) or 0)
        engine.max_budget_gbp = 35.0
        engine.on_cost = Mock()
        return engine

    def test_restart_skips_completed_finishing_operation_and_reloads_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "Worker"
            worker.mkdir()
            state = app.BatchState(root)
            state.init("Home", "claude-haiku-4-5", 1.0, {})
            api = SubmitAPI()
            first = self._ledger_engine(root, api, state)

            def charged_call():
                api.in_tokens = 1000
                api.out_tokens = 100
                return {"score": 88}

            result = first._finishing_operation(
                worker, "quality:hash", charged_call)
            self.assertEqual(result, {"score": 88})
            saved = app.BatchState(root)
            self.assertGreater(saved.data["costs"]["live_actual_gbp"], 0)
            operation = next(iter(saved.data["workers"].values()))[
                "finishing_operations"]["quality:hash"]
            self.assertEqual(operation["status"], "complete")

            fresh_api = SubmitAPI()
            restarted = self._ledger_engine(root, fresh_api, saved)
            duplicate = Mock(side_effect=AssertionError("must not be called"))
            again = restarted._finishing_operation(
                worker, "quality:hash", duplicate)
            self.assertEqual(again, {"score": 88})
            duplicate.assert_not_called()
            self.assertAlmostEqual(
                restarted._persisted_live_cost_gbp,
                saved.data["costs"]["live_actual_gbp"])

    def test_ambiguous_finishing_operation_is_never_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "Worker"
            worker.mkdir()
            state = app.BatchState(root)
            state.init("Home", "claude-haiku-4-5", 1.0, {})
            key = str(worker.resolve()).casefold()
            state.data["workers"][key] = {
                "name": worker.name, "source_path": str(worker),
                "finishing_operations": {
                    "date:hash": {"status": "submission_started",
                                  "attempt_id": "ambiguous"}}}
            state.save()
            engine = self._ledger_engine(root, SubmitAPI(), state)
            callback = Mock()
            with self.assertRaises(app.FinishingAmbiguous):
                engine._finishing_operation(worker, "date:hash", callback)
            callback.assert_not_called()


class TestReleaseChecksumContract(unittest.TestCase):

    def test_public_installer_verifies_download_before_copy(self):
        repo = Path(__file__).parents[1]
        script = (repo / "install.ps1").read_text(encoding="utf-8")
        verify_at = script.index("Get-FileHash -Algorithm SHA256")
        copy_at = script.index("Copy-Item -LiteralPath $DownloadedApp")
        self.assertLess(verify_at, copy_at)
        self.assertIn('"SHA256SUMS.txt"', script)
        self.assertIn("exit 1", script[verify_at:copy_at])

    def test_manifest_generator_has_public_final_inputs_and_no_self_hash(self):
        repo = Path(__file__).parents[1]
        script = (repo / "build" / "generate_release_checksums.ps1").read_text(
            encoding="utf-8")
        for artifact in ("Stage2_Processing.exe", "Stage2_Guide_AI_Processing.pdf",
                         "Stage2_Processing_Setup.exe", "inference.onnx"):
            self.assertIn(f'"{artifact}"', script)
        artifacts_block = script.split("$Lines =", 1)[0]
        self.assertNotIn('"SHA256SUMS.txt" =', artifacts_block)


if __name__ == "__main__":
    unittest.main()
