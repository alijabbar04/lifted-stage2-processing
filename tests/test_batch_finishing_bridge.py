"""Offline tests for the standalone discounted finishing bridge."""

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _load_app import load_app
import batch_finishing_bridge as bridge


app = load_app()


class Fixture:
    def __init__(self, root: Path, specs):
        self.root = root
        self.worker = root / "Worker A"
        self.worker.mkdir()
        self.records = []
        for filename, family, body in specs:
            path = self.worker / filename
            path.write_bytes(body)
            self.records.append({
                "path": path, "name": family, "group": "Important",
                "original": filename, "imgs": [], "text": "",
            })

        self.api = app.ClaudeAPI("offline-key", "claude-haiku-4-5")
        self.engine = app.Engine.__new__(app.Engine)
        self.engine.api = self.api
        self.engine.resolution = 1.0
        self.engine._batch_state = app.BatchState(root)
        key = str(self.worker.resolve()).casefold()
        self.engine._batch_state.data = {
            "version": 6,
            "workers": {key: {
                "name": self.worker.name,
                "source_path": str(self.worker),
                "finishing_status": "pending",
                "ranking_families": {},
                "finishing_operations": {},
            }},
            "costs": {},
        }
        assert self.engine._batch_state.save()
        self.engine._records_from_manifest = lambda worker: [
            dict(record) for record in self.records]
        self.engine._validate_batch_worker_records = lambda worker, records, state: None
        self.engine._pages_for_review = lambda record, path: (
            ["cGFnZQ=="], "ordinary evidence " + path.name)
        self.engine._signature_pages = lambda record, path: (
            ["c2lnbmF0dXJl"], "signature evidence " + path.name)

    @property
    def state_worker(self):
        return self.engine._batch_state.data["workers"][
            str(self.worker.resolve()).casefold()]

    def ledger(self):
        return json.loads((self.root / bridge.LEDGER_NAME).read_text(
            encoding="utf-8"))

    def prepare(self, **kwargs):
        return bridge.prepare(self.engine, [self.worker], **kwargs)

    def submit(self, prepared):
        return bridge.submit(
            self.engine, preparation_id=prepared["preparation_id"])

    def successful_rows(self):
        rows = []
        for operation in self.ledger()["operations"].values():
            if operation["method"] == "cos_issue_date":
                raw = '{"issue_date":"2026-01-02"}'
            elif operation["method"] == "share_code_check":
                raw = ('{"check_date":"2026-02-03",'
                       '"work_permitted":true}')
            elif operation["method"] == "contract_signed":
                raw = '{"signed":false}'
            else:
                raw = ('{"score":72,"legible":true,"complete":true,'
                       '"date":"","note":"clear copy"}')
            rows.append({
                "custom_id": operation["custom_id"],
                "result": {
                    "type": "succeeded",
                    "message": {
                        "id": "msg_" + operation["custom_id"][-8:],
                        "content": [{"type": "text", "text": raw}],
                        "usage": {"input_tokens": 100,
                                  "output_tokens": 10},
                    },
                },
            })
        return rows

    def ended_api(self, batch_id, rows):
        self.api.get_batch = Mock(return_value={
            "id": batch_id,
            "processing_status": "ended",
            "results_url": "https://api.anthropic.com/results/offline",
            "request_counts": {
                "processing": 0, "succeeded": len(rows), "errored": 0,
                "canceled": 0, "expired": 0,
            },
        })
        self.api.batch_results = Mock(return_value=iter(rows))


class TestBatchFinishingPreparation(unittest.TestCase):
    def test_prepare_mirrors_second_pass_selection_and_never_posts(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), [
                ("Certificate of Sponsorship.pdf", "Certificate of Sponsorship", b"same-cos"),
                ("Certificate of Sponsorship (2).pdf", "Certificate of Sponsorship", b"same-cos"),
                ("Share Code Check Result.pdf", "Share Code Check Result", b"share-one"),
                ("Share Code Check Result (2).pdf", "Share Code Check Result", b"share-two"),
                ("Employment Contract.pdf", "Employment Contract", b"contract-one"),
                ("Employment Contract (2).pdf", "Employment Contract", b"contract-two"),
                ("Passport.pdf", "Passport", b"passport"),
                ("Other - Note.pdf", "Other - Note", b"other"),
            ])
            before = {path.name: path.read_bytes()
                      for path in fixture.worker.iterdir()}
            with patch.object(app.urllib.request, "urlopen") as network:
                summary = fixture.prepare(chunk_target_bytes=1024 * 1024)

            self.assertEqual(summary["operations"], 9)
            network.assert_not_called()
            after = {path.name: path.read_bytes()
                     for path in fixture.worker.iterdir()}
            self.assertEqual(before, after)
            operations = list(fixture.ledger()["operations"].values())
            by_method = {}
            for operation in operations:
                by_method[operation["method"]] = by_method.get(
                    operation["method"], 0) + 1
                self.assertEqual(
                    operation["payload_hash"],
                    bridge._digest(operation["request"]["params"]))
                self.assertEqual(operation["request"]["params"]["model"],
                                 fixture.api.model_id)
            self.assertEqual(by_method, {
                "cos_issue_date": 1,
                "share_code_check": 2,
                "doc_quality": 4,
                "contract_signed": 2,
            })

    def test_single_nondated_family_and_other_need_no_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), [
                ("Employment Contract.pdf", "Employment Contract", b"contract"),
                ("Passport.pdf", "Passport", b"passport"),
                ("Other.pdf", "Other", b"other"),
            ])
            summary = fixture.prepare()
            self.assertEqual(summary["status"], "nothing_to_submit")
            self.assertEqual(summary["operations"], 0)

    def test_prepare_refuses_to_overwrite_its_only_durable_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), [
                ("Certificate of Sponsorship.pdf", "Certificate of Sponsorship", b"cos")])
            fixture.prepare()
            with self.assertRaisesRegex(bridge.BatchFinishingError,
                                        "refusing to overwrite"):
                fixture.prepare()


class TestBatchFinishingSubmission(unittest.TestCase):
    def test_marker_precedes_post_and_ambiguous_post_is_never_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), [
                ("Certificate of Sponsorship.pdf", "Certificate of Sponsorship", b"cos")])
            prepared = fixture.prepare()

            def fail(_requests):
                self.assertEqual(fixture.ledger()["chunks"][0]["status"],
                                 "submission_started")
                raise OSError("outcome unknown")

            fixture.api.submit_batch = Mock(side_effect=fail)
            with self.assertRaises(OSError):
                fixture.submit(prepared)
            self.assertEqual(fixture.ledger()["chunks"][0]["status"],
                             "ambiguous")
            with self.assertRaises(bridge.SubmissionAmbiguous):
                fixture.submit(prepared)
            self.assertEqual(fixture.api.submit_batch.call_count, 1)

    def test_submit_only_posts_prepared_chunks_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), [
                ("Certificate of Sponsorship.pdf", "Certificate of Sponsorship", b"cos")])
            prepared = fixture.prepare()
            fixture.api.submit_batch = Mock(return_value={
                "id": "msgbatch_one", "processing_status": "in_progress"})
            first = fixture.submit(prepared)
            second = fixture.submit(prepared)
            self.assertEqual(first["submitted_chunks"], 1)
            self.assertEqual(second["submitted_chunks"], 0)
            fixture.api.submit_batch.assert_called_once()


class TestBatchFinishingPolling(unittest.TestCase):
    def test_valid_results_install_receipted_native_answers_without_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), [
                ("Certificate of Sponsorship.pdf", "Certificate of Sponsorship", b"cos")])
            prepared = fixture.prepare()
            fixture.api.submit_batch = Mock(return_value={
                "id": "msgbatch_valid", "processing_status": "in_progress"})
            fixture.submit(prepared)
            rows = fixture.successful_rows()
            fixture.ended_api("msgbatch_valid", rows)

            result = bridge.poll(fixture.engine)

            self.assertEqual(result["status"], "installed")
            self.assertEqual(result["installed_operations"], 1)
            native = next(iter(fixture.state_worker["finishing_operations"].values()))
            self.assertEqual(native["status"], "complete")
            self.assertEqual(native["source"], "batch")
            self.assertEqual(native["result"], "2026-01-02")
            self.assertEqual(native["receipt"]["batch_id"], "msgbatch_valid")
            self.assertTrue(native["payload_binding_hash"])
            self.assertGreater(native["cost_gbp"], 0)
            self.assertGreater(result["finishing_batch_actual_gbp"], 0)
            # The bridge supplies evidence only. Native _finish_worker owns
            # ranking, renaming, organisation and the completion marker.
            self.assertEqual(fixture.state_worker["finishing_status"], "pending")
            self.assertEqual(fixture.state_worker["ranking_families"], {})
            self.assertTrue((fixture.worker / "Certificate of Sponsorship.pdf").exists())

    def test_malformed_signed_result_cannot_be_fabricated_as_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), [
                ("Employment Contract.pdf", "Employment Contract", b"one"),
                ("Employment Contract (2).pdf", "Employment Contract", b"two"),
            ])
            prepared = fixture.prepare()
            fixture.api.submit_batch = Mock(return_value={
                "id": "msgbatch_bad", "processing_status": "in_progress"})
            fixture.submit(prepared)
            rows = fixture.successful_rows()
            signed_id = next(key for key, operation in
                             fixture.ledger()["operations"].items()
                             if operation["method"] == "contract_signed")
            for row in rows:
                if row["custom_id"] == signed_id:
                    row["result"]["message"]["content"][0]["text"] = "{}"
            fixture.ended_api("msgbatch_bad", rows)

            with self.assertRaisesRegex(bridge.ResultValidationError,
                                        "malformed contract_signed"):
                bridge.poll(fixture.engine)
            self.assertEqual(fixture.state_worker["finishing_operations"], {})

    def test_duplicate_or_missing_results_install_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), [
                ("Share Code Check Result.pdf", "Share Code Check Result", b"share")])
            prepared = fixture.prepare()
            fixture.api.submit_batch = Mock(return_value={
                "id": "msgbatch_dup", "processing_status": "in_progress"})
            fixture.submit(prepared)
            rows = fixture.successful_rows()
            rows.append(dict(rows[0]))
            fixture.ended_api("msgbatch_dup", rows)
            # Claim one expected success despite returning a duplicated row.
            fixture.api.get_batch.return_value["request_counts"]["succeeded"] = 1

            with self.assertRaisesRegex(bridge.ResultValidationError,
                                        "missing, duplicated"):
                bridge.poll(fixture.engine)
            self.assertEqual(fixture.state_worker["finishing_operations"], {})

    def test_changed_source_blocks_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), [
                ("Certificate of Sponsorship.pdf", "Certificate of Sponsorship", b"cos")])
            prepared = fixture.prepare()
            fixture.api.submit_batch = Mock(return_value={
                "id": "msgbatch_changed", "processing_status": "in_progress"})
            fixture.submit(prepared)
            rows = fixture.successful_rows()
            fixture.ended_api("msgbatch_changed", rows)
            (fixture.worker / "Certificate of Sponsorship.pdf").write_bytes(b"changed")

            with self.assertRaisesRegex(bridge.BatchFinishingError,
                                        "evidence changed"):
                bridge.poll(fixture.engine)
            self.assertEqual(fixture.state_worker["finishing_operations"], {})

    def test_installed_batch_answers_drive_native_second_pass_with_live_post_blocked(self):
        """Exercise the real Engine._second_pass after a synthetic batch.

        This is the recovery boundary: every callback must replay a stored
        operation, including after native exact-dedup removes one record.
        """
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), [
                ("Certificate of Sponsorship.pdf", "Certificate of Sponsorship", b"cos-one"),
                ("Certificate of Sponsorship (2).pdf", "Certificate of Sponsorship", b"cos-two"),
                ("Certificate of Sponsorship (3).pdf", "Certificate of Sponsorship", b"cos-two"),
                ("Share Code Check Result.pdf", "Share Code Check Result", b"share"),
                ("Employment Contract.pdf", "Employment Contract", b"contract-one"),
                ("Employment Contract (2).pdf", "Employment Contract", b"contract-two"),
                ("Passport.pdf", "Passport", b"passport-one"),
                ("Passport (2).pdf", "Passport", b"passport-two"),
            ])
            # Supply the ordinary Engine attributes touched by _second_pass.
            fixture.engine._stop = threading.Event()
            fixture.engine.stats = {
                "errors": 0, "rank_deferred": 0, "ranked": 0,
                "cos": 0, "sharecode": 0, "dbs": 0, "brp": 0,
                "evisa": 0, "ni": 0,
            }
            fixture.engine.log = Mock()
            fixture.engine.redact_logs = False
            fixture.engine.failed_log = Mock()
            fixture.engine.rename_log = Mock()
            fixture.engine.orientation_state = None
            fixture.engine.care_home = "Synthetic"
            fixture.engine._authorized_finishing_retries = {}
            fixture.engine._finishing_retry_active = False

            prepared = fixture.prepare()
            fixture.api.submit_batch = Mock(return_value={
                "id": "msgbatch_native", "processing_status": "in_progress"})
            fixture.submit(prepared)
            rows = fixture.successful_rows()
            fixture.ended_api("msgbatch_native", rows)
            bridge.poll(fixture.engine)

            # Match _finish_worker's first step and then execute the real
            # native second pass. Every live call is a test failure.
            self.assertEqual(app.dedup_worker(fixture.worker), 1)
            records = fixture.engine._rebuild_records(
                fixture.worker, [dict(record) for record in fixture.records])
            with patch.object(app.ClaudeAPI, "_post",
                              side_effect=AssertionError("live POST reached")) as live:
                outcome = fixture.engine._second_pass(fixture.worker, records)

            live.assert_not_called()
            self.assertEqual(outcome["deferred"], [])
            self.assertEqual(
                {row["name"] for row in outcome["completed"]},
                {"Certificate of Sponsorship", "Share Code Check Result",
                 "Employment Contract", "Passport"})
            families = fixture.state_worker["ranking_families"]
            self.assertTrue(all(row["status"] == "complete"
                                for row in families.values()))
            expected_cost = sum(
                value["cost_gbp"] for value in
                fixture.state_worker["finishing_operations"].values())
            self.assertAlmostEqual(
                fixture.engine._batch_state.data["costs"][
                    "finishing_batch_actual_gbp"], expected_cost, places=7)


if __name__ == "__main__":
    unittest.main()
