"""Offline validation tests for live finishing-response helpers."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock


sys.path.insert(0, str(Path(__file__).resolve().parent))

from _load_app import load_app


app = load_app()


class FinishingResponseValidationTests(unittest.TestCase):
    def api_with_response(self, raw):
        api = app.ClaudeAPI.__new__(app.ClaudeAPI)
        api._post = lambda *args, **kwargs: raw
        return api

    def assert_rejected(self, method, raw):
        api = self.api_with_response(raw)
        with self.assertRaises(ValueError):
            getattr(api, method)([], "")

    def test_contract_signed_rejects_missing_or_non_boolean_value(self):
        invalid = [
            "{}",
            '{"signed":null}',
            '{"signed":"false"}',
            '{"signed":"true"}',
            '{"signed":0}',
            '{"signed":1}',
            '{"signed":[]}',
            '{"signed":{}}',
            "not json",
        ]
        for raw in invalid:
            with self.subTest(raw=raw):
                self.assert_rejected("contract_signed", raw)

    def test_contract_signed_preserves_real_booleans(self):
        self.assertIs(self.api_with_response('{"signed":false}').contract_signed([], ""), False)
        self.assertIs(self.api_with_response('{"signed":true}').contract_signed([], ""), True)

    def test_cos_issue_date_rejects_missing_or_non_string_value(self):
        invalid = [
            "{}",
            '{"issue_date":null}',
            '{"issue_date":false}',
            '{"issue_date":0}',
            '{"issue_date":[]}',
            '{"issue_date":{}}',
            "not json",
        ]
        for raw in invalid:
            with self.subTest(raw=raw):
                self.assert_rejected("cos_issue_date", raw)

    def test_cos_issue_date_preserves_empty_and_nonempty_dates(self):
        self.assertEqual(
            self.api_with_response('{"issue_date":""}').cos_issue_date([], ""), "")
        self.assertEqual(
            self.api_with_response('{"issue_date":"2026-09-01"}').cos_issue_date([], ""),
            "2026-09-01",
        )

    def test_share_code_check_requires_both_fields_with_exact_types(self):
        invalid = [
            "{}",
            '{"check_date":""}',
            '{"work_permitted":false}',
            '{"check_date":null,"work_permitted":false}',
            '{"check_date":0,"work_permitted":false}',
            '{"check_date":"","work_permitted":null}',
            '{"check_date":"","work_permitted":"false"}',
            '{"check_date":"","work_permitted":0}',
            "not json",
        ]
        for raw in invalid:
            with self.subTest(raw=raw):
                self.assert_rejected("share_code_check", raw)

    def test_share_code_check_preserves_empty_date_and_false(self):
        result = self.api_with_response(
            '{"check_date":"","work_permitted":false}'
        ).share_code_check([], "")
        self.assertEqual(result, {"check_date": "", "work_permitted": False})
        self.assertIs(result["work_permitted"], False)

    def test_share_code_check_preserves_nonempty_date_and_true(self):
        result = self.api_with_response(
            '{"check_date":" 2026-09-01 ","work_permitted":true}'
        ).share_code_check([], "")
        self.assertEqual(
            result,
            {"check_date": "2026-09-01", "work_permitted": True},
        )

    def test_real_json_parser_still_accepts_wrapped_valid_response(self):
        api = self.api_with_response(
            'Result follows:\n```json\n{"signed": false}\n```\nDone.'
        )
        self.assertIs(api.contract_signed([], ""), False)

    def test_malformed_live_answer_is_failed_durably_and_replays_without_post(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            worker = root / "Worker"
            worker.mkdir()
            evidence = worker / "Employment Contract.pdf"
            evidence.write_bytes(b"synthetic offline evidence")
            operation = f"contract-signed:{app.file_hash(evidence)}"

            state = app.BatchState(root)
            state.data = {"version": 1, "workers": {}, "costs": {}}
            self.assertTrue(state.save())

            api = app.ClaudeAPI("offline-key", "claude-haiku-4-5")
            api._post = Mock(return_value="{}")
            engine = app.Engine.__new__(app.Engine)
            engine.api = api
            engine.escalation_api = None
            engine._batch_state = state
            engine._live_state = None
            engine._finishing_retry_active = False
            engine._authorized_finishing_retries = {}
            engine._persisted_live_cost_gbp = 0.0
            engine._persisted_live_tokens = 0
            engine._committed_batch_cost_gbp = 0.0
            engine._committed_batch_tokens = 0
            engine.on_cost = lambda *_args: None

            callback = lambda: api.contract_signed([], "")
            with self.assertRaises(ValueError):
                engine._finishing_operation(
                    worker, operation, callback, evidence_path=evidence
                )

            saved = app.BatchState(root)
            worker_state = saved.data["workers"][str(worker.resolve()).casefold()]
            failed = worker_state["finishing_operations"][operation]
            self.assertEqual(failed["status"], "failed")
            self.assertIsNone(failed["result"])
            self.assertIn("ValueError", failed["error"])
            self.assertTrue(failed["attempt_id"])
            self.assertEqual(api._post.call_count, 1)

            before_replay = state.path.read_bytes()
            self.assertIsNone(engine._finishing_operation(
                worker, operation, callback, evidence_path=evidence
            ))
            self.assertEqual(api._post.call_count, 1)
            self.assertEqual(state.path.read_bytes(), before_replay)


if __name__ == "__main__":
    unittest.main()
