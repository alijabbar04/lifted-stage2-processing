"""Offline tests for descriptive Other and discounted follow-up state."""

import inspect
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from _load_app import load_app


app = load_app()


class StubKB:
    TYPES = {"Passport": "Crucial", "Bank Statement": "Important"}

    def canonical_name(self, name):
        for item in self.TYPES:
            if item.casefold() == str(name or "").casefold():
                return item
        return None

    def group_of(self, name):
        return self.TYPES.get(name, "Other")

    def vocabulary_block(self):
        return "Passport\nBank Statement"


def apply_engine():
    engine = app.Engine.__new__(app.Engine)
    engine.kb = StubKB()
    engine.bundle_split = False
    engine.auto_other = True
    engine.stats = {"unknown": 0, "renamed": 0, "errors": 0}
    engine.api = Mock(model_id="claude-haiku-4-5")
    engine.resolution = 1.0
    engine.manifest = Mock()
    engine.rename_log = Mock()
    engine.care_home = "Test"
    engine.log = Mock()
    engine.redact_logs = False
    return engine


class TestDescriptiveOtherPolicy(unittest.TestCase):

    def _apply(self, label):
        with tempfile.TemporaryDirectory() as tmp:
            worker = Path(tmp) / "Worker"
            worker.mkdir()
            path = worker / "input.pdf"
            path.write_bytes(b"pdf")
            records = []
            engine = apply_engine()
            result = {"match": False, "other_label": label,
                      "guess": label, "confidence": 91}
            engine._apply_classification(
                worker, path, result, "hash", "vocab", records,
                interactive=False, unknown_queue=[])
            return records[0]["path"].stem, engine

    def test_p60_becomes_prefixed_other_without_followup(self):
        self.assertFalse(app.batch_result_needs_followup(
            StubKB(), {"match": False, "other_label": "P60",
                       "confidence": 91}))
        stem, engine = self._apply("P60")
        self.assertEqual(stem, "Other - P60")
        engine.manifest.record.assert_called_once()

    def test_customer_experience_email_keeps_description_and_prefix(self):
        stem, _engine = self._apply("customer experience email")
        self.assertEqual(stem, "Other - customer experience email")

    def test_format_only_other_labels_are_unresolved(self):
        for label in ("email", "letter", "form", "scan", "screenshot", "PDF"):
            with self.subTest(label=label):
                self.assertTrue(app.batch_result_needs_followup(
                    StubKB(), {"match": False, "other_label": label,
                               "confidence": 99}))
        self.assertFalse(app.batch_result_needs_followup(
            StubKB(), {"match": False, "other_label": "P60",
                       "confidence": 99}))
        self.assertFalse(app.batch_result_needs_followup(
            StubKB(), {"match": False,
                       "other_label": "customer experience email",
                       "confidence": 99}))

    def test_noncanonical_match_true_receives_followup(self):
        self.assertTrue(app.batch_result_needs_followup(
            StubKB(), {"match": True, "name": "Invented payroll form",
                       "confidence": 99}))

    def test_snap_to_controlled_vocabulary_avoids_other_duplicate(self):
        result = {"match": False, "other_label": "bank statement",
                  "confidence": 90}
        self.assertFalse(app.batch_result_needs_followup(StubKB(), result))
        matched, name, group, *_rest = app.validate_result(StubKB(), result)
        self.assertTrue(matched)
        self.assertEqual((name, group), ("Bank Statement", "Important"))

    def test_generic_blank_malformed_and_low_confidence_need_followup(self):
        cases = (
            {},
            {"match": False, "other_label": "", "confidence": 90},
            {"match": False, "other_label": "Unknown", "confidence": 90},
            {"match": False, "other_label": "Other", "confidence": 90},
            {"match": False, "other_label": "payroll query letter",
             "confidence": app.AUTO_REVIEW_LABEL_CONF - 1},
            {"match": True, "name": "Passport",
             "confidence": app.AUTO_REVIEW_MATCH_CONF - 1},
        )
        for result in cases:
            with self.subTest(result=result):
                self.assertTrue(app.batch_result_needs_followup(
                    StubKB(), result))


class FakeBatchAPI:
    def __init__(self, model_id):
        self.model_id = model_id
        self.api_key = "not-a-live-key"
        self.in_tokens = 0
        self.out_tokens = 0
        self.submit_batch = Mock(return_value={
            "id": "msgbatch_followup", "processing_status": "in_progress"})
        self.classify = Mock()
        self.results_by_url = {}
        self.status_by_id = {}

    def classify_payload(self, vocab, imgs, text, **_kwargs):
        return "system " + vocab, [{"type": "text", "text": text}], 500

    def build_batch_request(self, custom_id, system, blocks, max_tokens):
        return {"custom_id": custom_id,
                "params": {"model": self.model_id, "system": system,
                           "messages": blocks, "max_tokens": max_tokens}}

    def get_batch(self, batch_id):
        return self.status_by_id[batch_id]

    def batch_results(self, url):
        yield from self.results_by_url.get(url, [])

    @staticmethod
    def _json_from(raw):
        return json.loads(raw)


def followup_engine(root, primary, stronger):
    engine = app.Engine.__new__(app.Engine)
    engine.dir = Path(root)
    engine.api = primary
    engine.escalation_api = stronger
    engine.kb = StubKB()
    engine.resolution = 1.0
    engine.max_budget_gbp = 35.0
    engine.stats = {}
    engine.log = Mock()
    engine.on_done = Mock()
    engine.redact_logs = False
    return engine


class TestDiscountedFollowupState(unittest.TestCase):

    def test_batch_creation_post_is_never_retried_after_ambiguous_network_error(self):
        api = app.ClaudeAPI("not-a-live-key", "claude-haiku-4-5")
        api.batch_transport = "urllib"  # Explicitly test the legacy transport offline.
        with patch.object(app.urllib.request, "urlopen",
                          side_effect=urllib.error.URLError("offline")) as send:
            with self.assertRaises(app.APIError):
                api.submit_batch([{"custom_id": "one", "params": {}}])
        self.assertEqual(send.call_count, 1)

    def test_batch_apply_contains_no_live_unknown_retry_loops(self):
        source = inspect.getsource(app.Engine.run_batch_apply)
        self.assertNotIn("_auto_review_unknowns(", source)
        self.assertNotIn("_recheck_leftover_unknowns(", source)
        self.assertNotIn("_rescue_batch_result(", source)

    def test_each_unresolved_document_gets_one_batch_request_no_live_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "Worker"
            worker.mkdir()
            primary = FakeBatchAPI("claude-haiku-4-5")
            stronger = FakeBatchAPI("claude-sonnet-4-6")
            state = app.BatchState(root)
            state.init("Home", primary.model_id, 1.0, {})
            unresolved = []
            for index in range(3):
                path = worker / f"unknown-{index}.pdf"
                path.write_bytes(f"doc-{index}".encode())
                digest = app.file_hash(path)
                cid = f"primary-{index}"
                unresolved.append(cid)
                state.add_request(cid, path, worker, digest, 1)
            state.data["est_finishing_gbp"] = 0
            state.data["est_audit_gbp"] = 0
            state.save()
            engine = followup_engine(root, primary, stronger)

            with patch.object(app.DocRender, "render",
                              return_value=(["one-page"], "text")), \
                 patch.object(app.DocRender, "page_count", return_value=1):
                waiting = engine._submit_followup_batch(
                    state, unresolved, StubKB().vocabulary_block(), 0.01)

            self.assertTrue(waiting)
            stronger.submit_batch.assert_called_once()
            submitted = stronger.submit_batch.call_args.args[0]
            self.assertEqual(len(submitted), 3)
            self.assertEqual(len({item["custom_id"] for item in submitted}), 3)
            self.assertTrue(all(item["params"]["model"]
                                == "claude-sonnet-4-6" for item in submitted))
            primary.classify.assert_not_called()
            stronger.classify.assert_not_called()

    def test_oversized_followup_is_split_into_guarded_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "Worker"
            worker.mkdir()
            primary = FakeBatchAPI("claude-haiku-4-5")
            stronger = FakeBatchAPI("claude-sonnet-4-6")
            state = app.BatchState(root)
            state.init("Home", primary.model_id, 1.0, {})
            unresolved = []
            for index in range(4):
                path = worker / f"unknown-{index}.pdf"
                path.write_bytes(f"doc-{index}".encode())
                cid = f"primary-{index}"
                unresolved.append(cid)
                state.add_request(
                    cid, path, worker, app.file_hash(path), 1)
            state.data["est_finishing_gbp"] = 0
            state.data["est_audit_gbp"] = 0
            state.save()
            engine = followup_engine(root, primary, stronger)

            sample_system, sample_blocks, sample_max = \
                stronger.classify_payload(
                    StubKB().vocabulary_block(), ["page"], "x" * 400)
            sample = stronger.build_batch_request(
                "fu-sample", sample_system, sample_blocks, sample_max)
            one_request = engine._serialized_request_bytes(sample)
            engine.FOLLOWUP_CHUNK_TARGET_BYTES = one_request + 16
            engine.BATCH_SUBMIT_MAX_BYTES = one_request * 2 + 256
            submitted_chunks = []

            def accept(requests):
                submitted_chunks.append(requests)
                return {"id": f"followup-{len(submitted_chunks)}",
                        "processing_status": "in_progress"}

            stronger.submit_batch = Mock(side_effect=accept)
            with patch.object(app.DocRender, "render",
                              return_value=(["one-page"], "x" * 400)), \
                 patch.object(app.DocRender, "page_count", return_value=1):
                waiting = engine._submit_followup_batch(
                    state, unresolved, StubKB().vocabulary_block(), 0.01)

            self.assertTrue(waiting)
            self.assertEqual(len(submitted_chunks), 4)
            submitted_ids = [request["custom_id"]
                             for chunk in submitted_chunks for request in chunk]
            self.assertEqual(len(submitted_ids), 4)
            self.assertEqual(len(set(submitted_ids)), 4)
            reloaded = app.BatchState(root)
            followup = reloaded.data["followup"]
            self.assertEqual(followup["phase"], "pending")
            self.assertEqual(followup["planned_chunks"], 4)
            self.assertEqual(len(followup["batches"]), 4)
            self.assertTrue(all(len(batch["request_ids"]) == 1
                                for batch in followup["batches"]))

    def test_ambiguous_later_chunk_never_resubmits_accepted_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "Worker"
            worker.mkdir()
            primary = FakeBatchAPI("claude-haiku-4-5")
            stronger = FakeBatchAPI("claude-sonnet-4-6")
            state = app.BatchState(root)
            state.init("Home", primary.model_id, 1.0, {})
            unresolved = []
            for index in range(2):
                path = worker / f"unknown-{index}.pdf"
                path.write_bytes(f"doc-{index}".encode())
                cid = f"primary-{index}"
                unresolved.append(cid)
                state.add_request(
                    cid, path, worker, app.file_hash(path), 1)
            state.data["est_finishing_gbp"] = 0
            state.data["est_audit_gbp"] = 0
            state.save()
            engine = followup_engine(root, primary, stronger)

            sample_system, sample_blocks, sample_max = \
                stronger.classify_payload(
                    StubKB().vocabulary_block(), ["page"], "x" * 400)
            sample = stronger.build_batch_request(
                "fu-sample", sample_system, sample_blocks, sample_max)
            one_request = engine._serialized_request_bytes(sample)
            engine.FOLLOWUP_CHUNK_TARGET_BYTES = one_request + 16
            engine.BATCH_SUBMIT_MAX_BYTES = one_request * 2 + 256
            stronger.submit_batch = Mock(side_effect=[
                {"id": "followup-accepted",
                 "processing_status": "in_progress"},
                app.APIError(0, "offline"),
            ])

            with patch.object(app.DocRender, "render",
                              return_value=(["one-page"], "x" * 400)), \
                 patch.object(app.DocRender, "page_count", return_value=1), \
                 self.assertRaises(app.APIError):
                engine._submit_followup_batch(
                    state, unresolved, StubKB().vocabulary_block(), 0.01)

            reloaded = app.BatchState(root)
            followup = reloaded.data["followup"]
            self.assertEqual(followup["phase"], "ambiguous")
            self.assertEqual(len(followup["batches"]), 1)
            self.assertEqual(len(followup["submitted_request_ids"]), 1)
            calls_before_restart = stronger.submit_batch.call_count
            restarted = followup_engine(root, primary, stronger)
            self.assertTrue(restarted._submit_followup_batch(
                reloaded, unresolved, StubKB().vocabulary_block(), 0.01))
            self.assertEqual(stronger.submit_batch.call_count,
                             calls_before_restart)

    def test_clean_restart_resumes_after_last_accepted_chunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "Worker"
            worker.mkdir()
            primary = FakeBatchAPI("claude-haiku-4-5")
            stronger = FakeBatchAPI("claude-sonnet-4-6")
            state = app.BatchState(root)
            state.init("Home", primary.model_id, 1.0, {})
            unresolved = []
            for index in range(3):
                path = worker / f"unknown-{index}.pdf"
                path.write_bytes(f"doc-{index}".encode())
                cid = f"primary-{index}"
                unresolved.append(cid)
                state.add_request(
                    cid, path, worker, app.file_hash(path), 1)
            state.data["est_finishing_gbp"] = 0
            state.data["est_audit_gbp"] = 0
            state.save()
            engine = followup_engine(root, primary, stronger)

            sample_system, sample_blocks, sample_max = \
                stronger.classify_payload(
                    StubKB().vocabulary_block(), ["page"], "x" * 400)
            sample = stronger.build_batch_request(
                "fu-sample", sample_system, sample_blocks, sample_max)
            one_request = engine._serialized_request_bytes(sample)
            engine.FOLLOWUP_CHUNK_TARGET_BYTES = one_request + 16
            engine.BATCH_SUBMIT_MAX_BYTES = one_request * 2 + 256
            accepted = []

            def accept(requests):
                accepted.append(requests)
                return {"id": f"followup-{len(accepted)}",
                        "processing_status": "in_progress"}

            stronger.submit_batch = Mock(side_effect=accept)
            with patch.object(app.DocRender, "render",
                              return_value=(["one-page"], "x" * 400)), \
                 patch.object(app.DocRender, "page_count", return_value=1):
                engine._submit_followup_batch(
                    state, unresolved, StubKB().vocabulary_block(), 0.01)

            completed = app.BatchState(root)
            followup = completed.data["followup"]
            first_batch = followup["batches"][0]
            first_id = first_batch["request_ids"][0]
            # Recreate a clean process stop immediately after chunk 1 was
            # accepted and durably saved, before chunk 2 began.
            followup["batches"] = [first_batch]
            followup["submitted_request_ids"] = [first_id]
            followup["phase"] = "submitting"
            followup["submission"] = {
                "status": "accepted", "batch_id": first_batch["id"],
                "request_ids": [first_id], "planned_chunk": 1,
            }
            completed.data["phase"] = "followup_submitting"
            completed.data["followup"] = followup
            self.assertTrue(completed.save())

            resumed_payloads = []

            def accept_resumed(requests):
                resumed_payloads.append(requests)
                return {"id": f"resumed-{len(resumed_payloads)}",
                        "processing_status": "in_progress"}

            stronger.submit_batch = Mock(side_effect=accept_resumed)
            restarted = followup_engine(root, primary, stronger)
            with patch.object(app.DocRender, "render",
                              return_value=(["one-page"], "x" * 400)), \
                 patch.object(app.DocRender, "page_count", return_value=1):
                self.assertTrue(restarted._submit_followup_batch(
                    completed, unresolved, StubKB().vocabulary_block(), 0.01))

            resumed_ids = [request["custom_id"]
                           for chunk in resumed_payloads for request in chunk]
            self.assertEqual(len(resumed_ids), 2)
            self.assertNotIn(first_id, resumed_ids)
            final = app.BatchState(root).data["followup"]
            self.assertEqual(final["phase"], "pending")
            self.assertEqual(len(final["batches"]), 3)
            self.assertEqual(len(final["submitted_request_ids"]), 3)

    def test_restart_does_not_submit_or_bill_followup_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "Worker"
            worker.mkdir()
            path = worker / "unknown.pdf"
            path.write_bytes(b"doc")
            digest = app.file_hash(path)
            primary = FakeBatchAPI("claude-haiku-4-5")
            stronger = FakeBatchAPI("claude-sonnet-4-6")
            state = app.BatchState(root)
            state.init("Home", primary.model_id, 1.0, {})
            state.add_request("primary-0", path, worker, digest, 1)
            state.save()
            first = followup_engine(root, primary, stronger)
            with patch.object(app.DocRender, "render",
                              return_value=(["one-page"], "text")), \
                 patch.object(app.DocRender, "page_count", return_value=1):
                first._submit_followup_batch(
                    state, ["primary-0"], StubKB().vocabulary_block(), 0.01)
            self.assertEqual(stronger.submit_batch.call_count, 1)

            reloaded = app.BatchState(root)
            self.assertEqual(reloaded.data["followup"]["phase"], "pending")
            restarted = followup_engine(root, primary, stronger)
            restarted._submit_followup_batch(
                reloaded, ["primary-0"], StubKB().vocabulary_block(), 0.01)
            self.assertEqual(stronger.submit_batch.call_count, 1)

    def test_v130_state_remains_pending_and_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / app.BATCH_STATE_NAME
            state_path.write_text(
                '{"version":1,"applied":false,"model_id":"old",'
                '"batches":[{"id":"batch-old","n":1}],"requests":{}}',
                encoding="utf-8")
            state = app.BatchState(root)
            self.assertTrue(state.exists())
            self.assertEqual(state.batch_ids(), ["batch-old"])
            self.assertEqual(state.batch_ids("followup"), [])

    def test_followup_waits_then_applies_once_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "Worker"
            worker.mkdir()
            path = worker / "query.pdf"
            path.write_bytes(b"offline-pdf")
            digest = app.file_hash(path)
            primary = FakeBatchAPI("claude-haiku-4-5")
            stronger = FakeBatchAPI("claude-sonnet-4-6")
            primary.status_by_id["primary-batch"] = {
                "id": "primary-batch", "processing_status": "ended",
                "results_url": "primary-results",
                "request_counts": {"succeeded": 1},
            }
            primary.results_by_url["primary-results"] = [{
                "custom_id": "primary-0",
                "result": {
                    "type": "succeeded",
                    "message": {
                        "usage": {"input_tokens": 100, "output_tokens": 20},
                        "content": [{"type": "text", "text": json.dumps({
                            "match": False, "other_label": "Unknown",
                            "guess": "Other", "confidence": 20,
                        })}],
                    },
                },
            }]
            state = app.BatchState(root)
            state.init("Home", primary.model_id, 1.0, {})
            state.add_request("primary-0", path, worker, digest, 1)
            state.add_batch("primary-batch", 1, "in_progress")
            state.data["est_finishing_gbp"] = 0
            state.data["est_audit_gbp"] = 0
            state.save()

            statuses = []
            first = app.Engine(
                root, StubKB(), primary, "Home", log=lambda _m: None,
                set_status=lambda _m: None, set_progress=lambda *_a: None,
                set_preview=lambda *_a: None, ask_unknown=lambda *_a: None,
                on_cost=lambda *_a: None,
                on_done=lambda _stats, status: statuses.append(status),
                resolution=1.0, escalation_api=stronger,
                bundle_split=False, post_run_audit=False,
                max_budget_gbp=35.0)
            with patch.object(app.DocRender, "render",
                              return_value=(["one-page"], "text")), \
                 patch.object(app.DocRender, "page_count", return_value=1), \
                 patch.object(first, "_finish_worker") as finish, \
                 patch.object(first, "_maybe_fix_rotation", return_value=None), \
                 patch.object(first, "_auto_review_unknowns") as live_retry, \
                 patch.object(first, "_recheck_leftover_unknowns") as live_recheck:
                first.run_batch_apply()
            self.assertTrue(statuses[-1].startswith(
                "batch_followup_submitted:"))
            self.assertTrue(path.exists())
            finish.assert_not_called()
            live_retry.assert_not_called()
            live_recheck.assert_not_called()
            self.assertEqual(stronger.submit_batch.call_count, 1)

            reloaded = app.BatchState(root)
            followup_cid = next(iter(reloaded.data["followup"]["requests"]))
            stronger.status_by_id["msgbatch_followup"] = {
                "id": "msgbatch_followup", "processing_status": "ended",
                "results_url": "followup-results",
                "request_counts": {"succeeded": 1},
            }
            stronger.results_by_url["followup-results"] = [{
                "custom_id": followup_cid,
                "result": {
                    "type": "succeeded",
                    "message": {
                        "usage": {"input_tokens": 120, "output_tokens": 20},
                        "content": [{"type": "text", "text": json.dumps({
                            "match": False,
                            "other_label": "payroll query letter",
                            "confidence": 92,
                        })}],
                    },
                },
            }]

            second_statuses = []
            second = app.Engine(
                root, StubKB(), primary, "Home", log=lambda _m: None,
                set_status=lambda _m: None, set_progress=lambda *_a: None,
                set_preview=lambda *_a: None, ask_unknown=lambda *_a: None,
                on_cost=lambda *_a: None,
                on_done=lambda _stats, status: second_statuses.append(status),
                resolution=1.0, escalation_api=stronger,
                bundle_split=False, post_run_audit=False,
                max_budget_gbp=35.0)
            with patch.object(second, "_finish_worker") as finish, \
                 patch.object(second, "_maybe_fix_rotation", return_value=None), \
                 patch.object(second, "_auto_review_unknowns") as live_retry, \
                 patch.object(second, "_recheck_leftover_unknowns") as live_recheck:
                second.run_batch_apply()
            self.assertTrue(second_statuses[-1].startswith("batch_applied:"))
            self.assertFalse((root / app.BATCH_STATE_NAME).exists())
            self.assertTrue((worker / "Other - payroll query letter.pdf").exists())
            self.assertEqual(stronger.submit_batch.call_count, 1)
            finish.assert_called_once()
            live_retry.assert_not_called()
            live_recheck.assert_not_called()

            # A third resume sees no state and cannot rename/apply it again.
            third_statuses = []
            third = app.Engine(
                root, StubKB(), primary, "Home", log=lambda _m: None,
                set_status=lambda _m: None, set_progress=lambda *_a: None,
                set_preview=lambda *_a: None, ask_unknown=lambda *_a: None,
                on_cost=lambda *_a: None,
                on_done=lambda _stats, status: third_statuses.append(status),
                resolution=1.0, escalation_api=stronger,
                bundle_split=False, post_run_audit=False)
            third.run_batch_apply()
            self.assertEqual(third_statuses[-1], "batch_none_pending")
            self.assertEqual(
                list(worker.glob("Other - payroll query letter*.pdf")),
                [worker / "Other - payroll query letter.pdf"])

    def test_completed_generic_stays_unresolved_but_failed_followup_falls_back(self):
        cases = {
            "errored": ({"type": "errored", "error": {"message": "x"}},
                        "Passport.pdf"),
            "expired": ({"type": "expired"}, "Passport.pdf"),
            "canceled": ({"type": "canceled"}, "Passport.pdf"),
            "no-result": (None, "Passport.pdf"),
            "completed-generic": ({
                "type": "succeeded",
                "message": {
                    "usage": {"input_tokens": 120, "output_tokens": 20},
                    "content": [{"type": "text", "text": json.dumps({
                        "match": False, "other_label": "Unknown",
                        "guess": "Other", "confidence": 20,
                    })}],
                },
            }, "Other - Unknown.pdf"),
        }
        for label, (followup_result, expected_name) in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                worker = root / "Worker"
                worker.mkdir()
                path = worker / "query.pdf"
                path.write_bytes(b"offline-pdf")
                primary = FakeBatchAPI("claude-haiku-4-5")
                stronger = FakeBatchAPI("claude-sonnet-4-6")
                primary.status_by_id["primary-batch"] = {
                    "id": "primary-batch", "processing_status": "ended",
                    "results_url": "primary-results",
                    "request_counts": {"succeeded": 1},
                }
                primary.results_by_url["primary-results"] = [{
                    "custom_id": "primary-0",
                    "result": {
                        "type": "succeeded",
                        "message": {
                            "usage": {"input_tokens": 100,
                                      "output_tokens": 20},
                            "content": [{"type": "text", "text": json.dumps({
                                "match": True, "name": "Passport",
                                "group": "Crucial", "confidence": 50,
                            })}],
                        },
                    },
                }]
                state = app.BatchState(root)
                state.init("Home", primary.model_id, 1.0, {})
                state.add_request("primary-0", path, worker,
                                  app.file_hash(path), 1)
                state.add_batch("primary-batch", 1, "in_progress")
                state.data["est_finishing_gbp"] = 0
                state.data["est_audit_gbp"] = 0
                state.save()

                def engine(statuses):
                    return app.Engine(
                        root, StubKB(), primary, "Home", log=lambda _m: None,
                        set_status=lambda _m: None,
                        set_progress=lambda *_a: None,
                        set_preview=lambda *_a: None,
                        ask_unknown=lambda *_a: None,
                        on_cost=lambda *_a: None,
                        on_done=lambda _stats, status: statuses.append(status),
                        resolution=1.0, escalation_api=stronger,
                        bundle_split=False, post_run_audit=False,
                        max_budget_gbp=35.0)

                first_statuses = []
                first = engine(first_statuses)
                with patch.object(app.DocRender, "render",
                                  return_value=(["one-page"], "text")), \
                     patch.object(app.DocRender, "page_count", return_value=1), \
                     patch.object(first, "_finish_worker"), \
                     patch.object(first, "_maybe_fix_rotation",
                                  return_value=None):
                    first.run_batch_apply()
                self.assertTrue(first_statuses[-1].startswith(
                    "batch_followup_submitted:"))
                self.assertEqual(stronger.submit_batch.call_count, 1)

                reloaded = app.BatchState(root)
                followup_cid = next(iter(
                    reloaded.data["followup"]["requests"]))
                stronger.status_by_id["msgbatch_followup"] = {
                    "id": "msgbatch_followup", "processing_status": "ended",
                    "results_url": "followup-results",
                    "request_counts": {label: 1},
                }
                stronger.results_by_url["followup-results"] = (
                    [] if followup_result is None else [{
                        "custom_id": followup_cid,
                        "result": followup_result,
                    }])

                second_statuses = []
                second = engine(second_statuses)
                with patch.object(second, "_finish_worker") as finish, \
                     patch.object(second, "_maybe_fix_rotation",
                                  return_value=None):
                    second.run_batch_apply()
                self.assertTrue(second_statuses[-1].startswith(
                    "batch_applied:"))
                self.assertTrue((worker / expected_name).exists())
                self.assertFalse((root / app.BATCH_STATE_NAME).exists())
                self.assertEqual(stronger.submit_batch.call_count, 1)
                finish.assert_called_once()

                third_statuses = []
                engine(third_statuses).run_batch_apply()
                self.assertEqual(third_statuses, ["batch_none_pending"])
                self.assertEqual(stronger.submit_batch.call_count, 1)


class TestCumulativeCostEstimate(unittest.TestCase):

    def test_every_enabled_phase_is_separate_and_in_total(self):
        est = app.estimate_pipeline_costs_gbp(
            100, "claude-haiku-4-5", "claude-sonnet-4-6", 1.5,
            StubKB().vocabulary_block(), batch=True, include_audit=True)
        for key in ("primary_gbp", "finishing_gbp",
                    "followup_reserve_gbp", "audit_gbp"):
            self.assertGreater(est[key], 0, key)
        self.assertAlmostEqual(
            est["gbp"], est["primary_gbp"] + est["finishing_gbp"]
            + est["followup_reserve_gbp"] + est["audit_gbp"])

    def test_audit_is_skipped_before_calls_when_remaining_budget_is_too_low(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "Worker"
            worker.mkdir()
            (worker / "Passport.pdf").write_bytes(b"offline")
            primary = FakeBatchAPI("claude-haiku-4-5")
            stronger = FakeBatchAPI("claude-sonnet-4-6")
            engine = app.Engine(
                root, StubKB(), primary, "Home", log=lambda _m: None,
                set_status=lambda _m: None, set_progress=lambda *_a: None,
                set_preview=lambda *_a: None, ask_unknown=lambda *_a: None,
                on_cost=lambda *_a: None, on_done=lambda *_a: None,
                resolution=1.0, escalation_api=stronger,
                post_run_audit=True, max_budget_gbp=0.000001)
            engine._audit_worker_dirs = [worker]
            with patch.object(app, "run_accuracy_audit") as audit:
                engine._run_post_run_audit()
            audit.assert_not_called()
            self.assertEqual(engine.stats["audit_skipped_budget"], 1)
            self.assertGreater(engine.stats["audit_expected_gbp"], 0)


if __name__ == "__main__":
    unittest.main()
