"""Offline recovery: never pay twice, never lose the unrendered primary tail."""
import datetime
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from _load_app import load_app
from test_release_blockers_v131 import SubmitAPI, make_engine, FIXTURE

app = load_app()


class RecoveryFixture:
    def __init__(self, root, inventory=False):
        self.root = Path(root)
        self.worker = self.root / "Worker"
        self.worker.mkdir()
        self.ids = []
        self.meta = {}
        for index in range(4):
            path = self.worker / f"doc{index}.pdf"
            shutil.copy2(FIXTURE, path)
            digest = app.file_hash(path)
            cid = f"{digest[:56]}-{index:03d}"
            self.ids.append(cid)
            self.meta[cid] = {"path": str(path), "worker": "Worker",
                              "worker_dir": str(self.worker), "fhash": digest,
                              "pages": 1}
        self.state = app.BatchState(self.root)
        self.state.init("Synthetic", "claude-haiku-4-5", 1.0,
                        {"bundle_split": False, "post_run_audit": False})
        self.state.data["requests"] = {cid: self.meta[cid] for cid in self.ids[:3]}
        self.state.add_batch("known", 2, "in_progress")
        self.started = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        self.state.data["primary_submission"] = {
            "status": "ambiguous", "started_ts": self.started,
            "request_identities": [self.ids[2]], "planned_chunk_id": "primary-0002"}
        if inventory:
            self.state.data["primary_inventory"] = dict(self.meta)
            self.state.data["primary_submission_complete"] = False
        self.state.save()
        self.api = SubmitAPI()
        self.api.get_batch = Mock(side_effect=self.get_batch)
        self.api.list_batches = Mock(return_value={"data": [], "has_more": False})
        self.api.batch_results = Mock(side_effect=self.results)
        self.api.submit_batch = Mock(return_value={"id": "recovered", "processing_status": "in_progress"})
        self.statuses = []
        self.engine = make_engine(self.root, self.api,
                                  lambda _stats, status: self.statuses.append(status))
        self.engine.PRIMARY_RECOVERY_GRACE_SECONDS = 0
        self.engine.PRIMARY_RECOVERY_RECHECK_SECONDS = 0
        self.engine._batch_classification_view = Mock(return_value=([], "synthetic document", [0], 1, False))

    def get_batch(self, bid):
        ids = self.ids[:2] if bid == "known" else [self.ids[2]]
        return {"id": bid, "processing_status": "ended", "results_url": bid,
                "request_counts": {"succeeded": len(ids)}}

    def results(self, url):
        ids = self.ids[:2] if url == "known" else [self.ids[2]]
        return [{"custom_id": cid, "result": {"type": "succeeded"}} for cid in ids]

    def candidate(self):
        return {"id": "candidate", "created_at": self.started}


class TestPrimaryRecovery(unittest.TestCase):
    def test_assess_legacy_tail_is_read_only_and_preserves_duplicate_ids(self):
        with tempfile.TemporaryDirectory() as root:
            f = RecoveryFixture(root)
            before = f.state.path.read_bytes()
            with patch.object(app, "cleanup_orientation_temp_files", side_effect=AssertionError("cleanup called")):
                report = f.engine.recover_primary_submission()
            self.assertEqual(report["status"], "needs_authorization", report)
            self.assertEqual((report["accepted"], report["remaining"], report["legacy_tail"]), (2, 2, 1))
            self.assertEqual(before, f.state.path.read_bytes())
            self.assertFalse(list(f.root.glob("*.bak")))
            f.api.submit_batch.assert_not_called()
            self.assertFalse(f.statuses)

    def test_authorized_no_match_snapshots_rechecks_and_submits_only_tail(self):
        with tempfile.TemporaryDirectory() as root:
            f = RecoveryFixture(root)
            before = f.state.path.read_bytes()
            report = f.engine.recover_primary_submission(True)
            self.assertEqual(report["status"], "submitted", report)
            self.assertEqual(f.api.list_batches.call_count, 2)
            sent = f.api.submit_batch.call_args.args[0]
            self.assertEqual({row["custom_id"] for row in sent}, set(f.ids[2:]))
            state = app.BatchState(f.root).data
            self.assertTrue(state["primary_submission_complete"])
            self.assertEqual(len(state["primary_inventory"]), 4)
            self.assertEqual(state["batches"][0]["id"], "known")
            self.assertEqual(Path(report["snapshot"]).read_bytes(), before)
            self.assertTrue(f.statuses[-1].startswith("batch_submitted:"))

    def test_exact_provider_match_reuses_results_and_only_sends_unrendered_tail(self):
        with tempfile.TemporaryDirectory() as root:
            f = RecoveryFixture(root)
            f.api.list_batches.return_value = {"data": [f.candidate()], "has_more": False}
            report = f.engine.recover_primary_submission(True)
            self.assertEqual(report["status"], "submitted", report)
            self.assertEqual({row["custom_id"] for row in f.api.submit_batch.call_args.args[0]}, {f.ids[3]})
            self.assertEqual(app.BatchState(f.root).batch_ids(), ["known", "candidate", "recovered"])

    def test_pending_candidate_blocks_without_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            f = RecoveryFixture(root)
            f.api.list_batches.return_value = {"data": [f.candidate()], "has_more": False}
            f.api.get_batch.side_effect = lambda bid: (f.get_batch(bid) if bid == "known"
                                                      else {"id": bid, "processing_status": "in_progress"})
            before = f.state.path.read_bytes()
            report = f.engine.recover_primary_submission(True)
            self.assertEqual(report["status"], "blocked", report)
            self.assertEqual(before, f.state.path.read_bytes())
            f.api.submit_batch.assert_not_called()

    def test_changed_saved_file_blocks(self):
        with tempfile.TemporaryDirectory() as root:
            f = RecoveryFixture(root)
            with Path(f.meta[f.ids[0]]["path"]).open("ab") as stream:
                stream.write(b"changed")
            report = f.engine.recover_primary_submission(True)
            self.assertEqual(report["status"], "blocked", report)
            f.api.submit_batch.assert_not_called()

    def test_second_check_finds_batch_and_does_not_resubmit_uncertain_ids(self):
        with tempfile.TemporaryDirectory() as root:
            f = RecoveryFixture(root)
            f.api.list_batches.side_effect = [{"data": [], "has_more": False},
                {"data": [f.candidate()], "has_more": False}]
            report = f.engine.recover_primary_submission(True)
            self.assertEqual(report["status"], "submitted", report)
            self.assertEqual({row["custom_id"] for row in f.api.submit_batch.call_args.args[0]}, {f.ids[3]})

    def test_recovery_post_failure_stays_ambiguous_with_full_inventory(self):
        with tempfile.TemporaryDirectory() as root:
            f = RecoveryFixture(root)
            f.api.submit_batch.side_effect = app.APIError(0, "network outcome unknown")
            report = f.engine.recover_primary_submission(True)
            self.assertEqual(report["status"], "blocked", report)
            state = app.BatchState(f.root).data
            self.assertEqual(state["primary_submission"]["status"], "ambiguous")
            self.assertFalse(state["primary_submission_complete"])
            self.assertEqual(len(state["primary_inventory"]), 4)
            self.assertEqual(f.api.submit_batch.call_count, 1)

    def test_incomplete_inventory_blocks_apply_even_after_last_batch_accepted(self):
        with tempfile.TemporaryDirectory() as root:
            f = RecoveryFixture(root, inventory=True)
            f.state.data["primary_submission"] = {"status": "accepted"}
            f.state.save()
            f.engine.run_batch_apply()
            self.assertEqual(f.statuses, ["batch_primary_incomplete"])
            f.api.get_batch.assert_not_called()
            f.api.submit_batch.assert_not_called()

    def test_invalid_known_id_ledger_blocks(self):
        with tempfile.TemporaryDirectory() as root:
            f = RecoveryFixture(root)
            f.state.data["batches"][0]["request_ids"] = [f.ids[0]]
            f.state.save()
            report = f.engine.recover_primary_submission(True)
            self.assertEqual(report["status"], "blocked", report)
            f.api.submit_batch.assert_not_called()

    def test_initial_failure_persists_all_eligible_files_before_post(self):
        with tempfile.TemporaryDirectory() as root:
            f = RecoveryFixture(root)
            f.state.path.unlink()
            f.api.submit_batch.side_effect = app.APIError(0, "network outcome unknown")
            f.engine.BATCH_SUBMIT_MAX_REQUESTS = 1
            f.engine.run_batch_submit()
            state = app.BatchState(f.root).data
            self.assertEqual(len(state["primary_inventory"]), 4)
            self.assertEqual(len(state["requests"]), 1)
            self.assertFalse(state["primary_submission_complete"])
            self.assertEqual(f.api.submit_batch.call_count, 1)

    def test_clean_restart_skips_accepted_recovery_chunk_and_keeps_tail(self):
        with tempfile.TemporaryDirectory() as root:
            f = RecoveryFixture(root)
            f.engine.BATCH_SUBMIT_MAX_REQUESTS = 1
            f.api.submit_batch.side_effect = [
                {"id": "first-recovery", "processing_status": "in_progress"},
                app.APIError(0, "connection interrupted")]
            report = f.engine.recover_primary_submission(True)
            self.assertEqual(report["status"], "blocked", report)
            interrupted = app.BatchState(f.root).data
            self.assertEqual(interrupted["batches"][-1]["request_ids"], [f.ids[2]])
            self.assertEqual(interrupted["primary_submission"]["request_identities"], [f.ids[3]])
            self.assertFalse(interrupted["primary_submission_complete"])

            restarted = make_engine(f.root, f.api)
            restarted.PRIMARY_RECOVERY_GRACE_SECONDS = 0
            restarted.PRIMARY_RECOVERY_RECHECK_SECONDS = 0
            restarted._batch_classification_view = Mock(
                return_value=([], "synthetic document", [0], 1, False))
            f.api.submit_batch.reset_mock(side_effect=True)
            f.api.submit_batch.return_value = {"id": "last-recovery", "processing_status": "in_progress"}
            assessment = restarted.recover_primary_submission()
            self.assertEqual((assessment["accepted"], assessment["remaining"]), (3, 1), assessment)
            f.api.submit_batch.assert_not_called()
            report = restarted.recover_primary_submission(True)
            self.assertEqual(report["status"], "submitted", report)
            self.assertEqual({row["custom_id"] for row in f.api.submit_batch.call_args.args[0]}, {f.ids[3]})
            self.assertEqual(app.BatchState(f.root).batch_ids(),
                             ["known", "first-recovery", "last-recovery"])
            self.assertTrue(app.BatchState(f.root).data["primary_submission_complete"])

    def test_recovery_target_splits_chunks_below_the_hard_cap(self):
        with tempfile.TemporaryDirectory() as root:
            f = RecoveryFixture(root)
            system, blocks, mt = f.api.classify_payload("Passport", [], "synthetic document")
            one_request = f.api.build_batch_request(f.ids[2], system, blocks, mt)
            one_wire_size = len(json.dumps({"requests": [one_request]}).encode("utf-8"))
            f.engine.PRIMARY_RECOVERY_TARGET_BYTES = one_wire_size + 1
            f.api.submit_batch.side_effect = [
                {"id": "small-1", "processing_status": "in_progress"},
                {"id": "small-2", "processing_status": "in_progress"}]
            statuses = []
            f.engine.set_status = statuses.append
            report = f.engine.recover_primary_submission(True)
            self.assertEqual(report["status"], "submitted", report)
            self.assertEqual(f.api.submit_batch.call_count, 2)
            sent_ids = []
            for call in f.api.submit_batch.call_args_list:
                payload = call.args[0]
                wire_size = len(json.dumps({"requests": payload}).encode("utf-8"))
                self.assertLessEqual(wire_size, f.engine.PRIMARY_RECOVERY_TARGET_BYTES)
                self.assertLessEqual(wire_size, f.engine.PRIMARY_RECOVERY_CHUNK_BYTES)
                sent_ids.extend(row["custom_id"] for row in payload)
            self.assertEqual(sent_ids, f.ids[2:])
            self.assertTrue(any(message.startswith("Uploading recovery chunk")
                                and "MiB" in message for message in statuses))

    def test_single_request_above_target_is_allowed_without_downsampling(self):
        with tempfile.TemporaryDirectory() as root:
            f = RecoveryFixture(root)
            f.api.list_batches.return_value = {"data": [f.candidate()], "has_more": False}
            f.engine.PRIMARY_RECOVERY_TARGET_BYTES = 1
            original_resolution = f.state.data["resolution"]
            report = f.engine.recover_primary_submission(True)
            self.assertEqual(report["status"], "submitted", report)
            f.api.submit_batch.assert_called_once()
            payload = f.api.submit_batch.call_args.args[0]
            wire_size = len(json.dumps({"requests": payload}).encode("utf-8"))
            self.assertEqual([row["custom_id"] for row in payload], [f.ids[3]])
            self.assertGreater(wire_size, f.engine.PRIMARY_RECOVERY_TARGET_BYTES)
            self.assertLessEqual(wire_size, f.engine.PRIMARY_RECOVERY_CHUNK_BYTES)
            self.assertEqual(f.engine.resolution, original_resolution)

    def test_writer_lock_blocks_all_mutating_entry_points(self):
        with tempfile.TemporaryDirectory() as root:
            f = RecoveryFixture(root)
            before = f.state.path.read_bytes()
            with app.CareHomeWriterLock(f.root):
                f.engine.run_batch_submit()
                f.engine.run_batch_apply()
                f.engine.run()
                report = f.engine.recover_primary_submission(True)
            self.assertTrue(report["busy"], report)
            self.assertEqual(len(f.statuses), 4)
            self.assertTrue(all(status.startswith("batch_busy:") for status in f.statuses))
            self.assertEqual(f.state.path.read_bytes(), before)
            f.api.submit_batch.assert_not_called()
            f.api.get_batch.assert_not_called()
            f.api.list_batches.assert_not_called()

    def test_read_only_assessment_never_creates_a_writer_lock(self):
        with tempfile.TemporaryDirectory() as root:
            f = RecoveryFixture(root)
            report = f.engine.recover_primary_submission(False)
            self.assertEqual(report["status"], "needs_authorization", report)
            self.assertFalse((f.root / app.CareHomeWriterLock.NAME).exists())

    def test_writer_lock_releases_on_exception_and_keeps_same_file(self):
        with tempfile.TemporaryDirectory() as root:
            lock_path = Path(root) / app.CareHomeWriterLock.NAME
            with self.assertRaisesRegex(RuntimeError, "synthetic"):
                with app.CareHomeWriterLock(root):
                    raise RuntimeError("synthetic failure")
            self.assertTrue(lock_path.exists())
            original_identity = lock_path.stat().st_ino
            with app.CareHomeWriterLock(root):
                self.assertEqual(lock_path.stat().st_ino, original_identity)
            self.assertTrue(lock_path.exists())

    def test_writer_lock_is_cross_process_and_os_releases_it_on_exit(self):
        with tempfile.TemporaryDirectory() as root:
            lock_path = Path(root) / app.CareHomeWriterLock.NAME
            code = """import os, sys
stream = open(sys.argv[1], 'r+b')
try:
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(3)
os._exit(0)
"""
            with app.CareHomeWriterLock(root):
                child = subprocess.run([sys.executable, "-c", code, str(lock_path)],
                                       capture_output=True, timeout=10)
                self.assertEqual(child.returncode, 3, child.stderr)
            child = subprocess.run([sys.executable, "-c", code, str(lock_path)],
                                   capture_output=True, timeout=10)
            self.assertEqual(child.returncode, 0, child.stderr)
            # os._exit above deliberately bypassed application cleanup.
            with app.CareHomeWriterLock(root):
                self.assertTrue(lock_path.exists())


if __name__ == "__main__":
    unittest.main()
