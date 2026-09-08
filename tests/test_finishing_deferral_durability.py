"""Unavailable finishing evidence must survive the real batch workflow.

Independent QA of 2026-09-08 (second report), reproduced here with synthetic,
non-personal fixtures:

* A family whose dating / signed / quality check had no answer was deferred
  locally, but `_finish_worker` returned normally, so batch apply marked the
  worker's finishing complete, moved it, set `completed`, crossed the durable
  `processing_complete` boundary and deleted the state. Check batch status
  could never revisit the family. The worker must instead stay incomplete,
  unmoved, unfiled and outside the audit scope, with a durable, non-looping
  attention state; an ordinary check replays stored answers only and never
  re-buys a failed request; the only route to a new attempt is an explicitly
  confirmed, exact-state-bound retry that keeps the earlier attempt as lineage.

* `quality_for` manufactured score=0 / legible=False / complete=False when
  the quality answer was unavailable (a fresh failure, a failed operation
  replayed as None, or an answer that is not an assessment), and the family
  was ranked and renamed on that invented value. Missing evidence is not a
  low-quality assessment: the family is deferred. A genuine score of 0 and a
  legitimately absent legible/complete field remain assessments.

* The same principle for the dated types: a failed date operation is not an
  absent date. A legitimately absent date (an answer with no supported date)
  still names the copy undated and ranks it below dated peers.

Every test asserts which BYTES sit at which exact filename, the durable
worker/phase state, and the number of paid calls actually made.
"""
import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load_app import load_app
from _pdf_fixtures import pdf_bytes
from test_second_pass_identity import (
    EvidenceAPI, MARK, SecondPassFixture, all_text, contract, signed_op)
from test_batch_worker_scope import EndedBatchAPI

import fitz  # noqa: F401  (PyMuPDF must be present for the fixtures)

app = load_app()

SOURCE = Path(__file__).resolve().parents[1] / "src" / "Stage2_Processing.pyw"

TYPES = {"Employment Contract": "Important", "DBS Document": "Crucial",
         "Passport": "Crucial", "Share Code Check Result": "Crucial",
         "Certificate of Sponsorship": "Crucial"}


def dbs(quality, marker):
    return pdf_bytes(page_texts=[f"DBS certificate record {marker} QUALITY {quality}"])


def share_code(date, quality, marker=""):
    return pdf_bytes(page_texts=[
        f"Home Office right to work result {marker} CHECKDATE {date} QUALITY {quality}"])


def cos(date, marker=""):
    return pdf_bytes(page_texts=[f"Certificate of Sponsorship details {marker} COSDATE {date}"])


class KB:
    def vocabulary_block(self):
        return "\n".join(TYPES)

    def canonical_name(self, name):
        for known in TYPES:
            if str(name).casefold() == known.casefold():
                return known
        return None

    def group_of(self, name):
        return TYPES.get(name, "Other")


class FinishingAPI(EvidenceAPI):
    """Answers every finishing question from the evidence it is shown, and
    fails a check on demand: `fail_quality` / `fail_date` are sets of text
    markers whose documents raise; `fail_once` makes a failure clear itself
    after the first raise so a later pass can answer."""

    def __init__(self, fail_quality=(), fail_date=(), fail_signed=False,
                 fail_once=False):
        super().__init__(fail_signed=fail_signed)
        self.fail_quality = set(fail_quality)
        self.fail_date = set(fail_date)
        self.fail_once = fail_once
        self.quality_calls, self.date_calls = [], []

    def _maybe_fail(self, pool, text, what):
        hit = next((m for m in pool if m in text), None)
        if hit is None:
            return
        if self.fail_once:
            pool.discard(hit)
        raise RuntimeError(f"synthetic {what} outage")

    def doc_quality(self, imgs, text, doc_type):
        self.quality_calls.append((doc_type, text[:5000]))
        self._maybe_fail(self.fail_quality, text[:5000], "quality")
        return super().doc_quality(imgs, text, doc_type)

    def share_code_check(self, imgs, text):
        self.date_calls.append(("share-code", text[:5000]))
        self._maybe_fail(self.fail_date, text[:5000], "date")
        return super().share_code_check(imgs, text)

    def cos_issue_date(self, imgs, text):
        self.date_calls.append(("cos", text[:5000]))
        self._maybe_fail(self.fail_date, text[:5000], "date")
        import re
        m = re.search(r"COSDATE (\d{4}-\d{2}-\d{2})", text[:5000])
        return m.group(1) if m else ""


class BatchAPI(EndedBatchAPI, FinishingAPI):
    """An ended primary batch whose classification results are supplied per
    custom id, plus live finishing answers from FinishingAPI."""

    def __init__(self, **finishing):
        EndedBatchAPI.__init__(self)
        FinishingAPI.__init__(self, **finishing)
        self.results = []


def classified(name):
    return {"type": "succeeded",
            "message": {"usage": {"input_tokens": 10, "output_tokens": 2},
                        "content": [{"type": "text", "text": json.dumps({
                            "match": True, "name": name,
                            "group": TYPES[name], "confidence": 99})}]}}


class BatchFixture:
    """A two-worker care home with an ended primary batch, exactly as
    run_batch_apply finds it after the provider finished."""

    def __init__(self, base, workers):
        self.base = Path(base)
        self.appdata = self.base / "appdata"
        self.appdata.mkdir()
        self.root = self.base / "Files"
        self.root.mkdir()
        self.dest = self.base / "Processed"
        self.dest.mkdir()
        self.logs, self.statuses = [], []
        self._patches = [
            patch.object(app, "APP_DIR", self.appdata),
            patch.object(app, "RENAME_CSV", self.appdata / "renames.csv"),
            patch.object(app, "FAILED_CSV", self.appdata / "failed.csv"),
            patch.object(app, "OVERRIDE_XLSX", self.appdata / "override.xlsx"),
            patch.object(app, "RECORD_XLSX", self.appdata / "record.xlsx"),
            patch.object(app, "CONFIG_PATH", self.appdata / "config.json"),
        ]
        for p in self._patches:
            p.start()
        self.workers = {}
        self.results = {}
        self.state = app.BatchState(self.root)
        self.state.init("Synthetic Home", "offline", 1.0,
                        {"post_run_audit": False, "move_mode": True})
        selected = []
        index = 0
        for worker_name, files in workers.items():
            worker = self.root / worker_name
            worker.mkdir()
            selected.append(worker)
            self.workers[worker_name] = worker
            for filename, (name, data) in files.items():
                path = worker / filename
                path.write_bytes(data)
                cid = f"request-{index:03d}"
                index += 1
                self.state.add_request(cid, path, worker, app.file_hash(path), 1)
                self.results[cid] = classified(name)
        self.state.data["submitted_worker_scope"] = [
            {"name": w.name, "source_path": str(w.resolve())} for w in selected]
        self.state.add_batch("primary", len(self.results), "in_progress",
                             request_ids=list(self.results))
        self.state.data["primary_submission_complete"] = True
        assert self.state.save()
        self.receipts = Mock()
        self.roster = Mock()

    def close(self):
        for p in self._patches:
            p.stop()

    def api(self, **finishing):
        api = BatchAPI(**finishing)
        api.results = [{"custom_id": cid, "result": result}
                       for cid, result in self.results.items()]
        return api

    def engine(self, api):
        return app.Engine(
            self.root, KB(), api, "Synthetic Home",
            log=self.logs.append, set_status=lambda _m: None,
            set_progress=lambda *_a: None, set_preview=lambda *_a: None,
            ask_unknown=lambda *_a: None, on_cost=lambda *_a: None,
            on_done=lambda _s, status: self.statuses.append(status),
            resolution=1.0, convert_pdf=False, bundle_split=False,
            orientation_mode="off", post_run_audit=False, move_mode=True,
            move_dest=self.dest, cleanup_leftovers=False)

    def apply(self, api, retry=None):
        """One Check batch status (or one explicitly confirmed retry) with a
        fresh engine, as the desktop app does."""
        engine = self.engine(api)

        def audit():
            engine.stats["audit_status"] = "disabled"

        with patch.object(engine, "_maybe_fix_rotation", return_value=None), \
                patch.object(engine, "_record_roster_handover", self.roster), \
                patch.object(engine, "_record_review_processing", self.receipts), \
                patch.object(engine, "_run_post_run_audit", side_effect=audit):
            # the keyword exists only on the amended source; an ordinary check
            # is the plain call, so the controls also run on older snapshots
            if retry:
                engine.run_batch_apply(retry_unresolved=retry)
            else:
                engine.run_batch_apply()
        return engine

    def saved(self):
        return json.loads(self.state.path.read_text(encoding="utf-8"))

    def worker_state(self, name):
        for worker in self.saved()["workers"].values():
            if worker.get("name") == name:
                return worker
        return None

    def failed_rows(self):
        path = self.appdata / "failed.csv"
        if not path.exists():
            return []
        with open(path, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def rename_rows(self):
        with open(self.appdata / "renames.csv", newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    @staticmethod
    def names(folder):
        return {p.relative_to(folder).as_posix(): p.read_bytes()
                for p in Path(folder).rglob("*.pdf")}

    def attention(self):
        status = self.statuses[-1]
        assert status.startswith("batch_apply_attention:"), status
        return json.loads(status.partition(":")[2])


UNSIGNED = contract(signed=False, quality=90)
SIGNED = contract(signed=True, quality=70)
# The quality check is shown pages 1, 2 and the last page, so it recognises
# the signed copy by its page-1 score text, not by the page-7 signature mark.
SIGNED_PAGE1 = "QUALITY 70"
DBS_POOR = dbs(40, "DBS-A")
DBS_GOOD = dbs(95, "DBS-B")
PASSPORT = pdf_bytes("passport single copy")

TWO_WORKERS = {
    "Worker A": {"contract-a.pdf": ("Employment Contract", UNSIGNED),
                 "contract-b.pdf": ("Employment Contract", SIGNED),
                 "dbs-a.pdf": ("DBS Document", DBS_POOR),
                 "dbs-b.pdf": ("DBS Document", DBS_GOOD)},
    # not 'passport.pdf': on a case-insensitive disk that would be its own
    # target name and take the '(2)' collision suffix
    "Worker B": {"pp.pdf": ("Passport", PASSPORT)},
}


class TestValidBatchFlowsStillComplete(unittest.TestCase):
    """Controls: with every answer available the batch completes as before."""

    def test_two_workers_rank_organise_move_and_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = BatchFixture(tmp, TWO_WORKERS)
            try:
                engine = f.apply(f.api())
                self.assertTrue(f.statuses[-1].startswith("batch_applied:"), f.statuses)
                self.assertFalse(f.state.path.exists(), "applied state is deleted")
                self.assertFalse((f.root / "Worker A").exists())
                self.assertFalse((f.root / "Worker B").exists())
                a = f.names(f.dest / "Worker A")
                self.assertEqual(sorted(a), [
                    "Overwrite Documents/DBS Document (01).pdf",
                    "Overwrite Documents/DBS Document.pdf",
                    "Overwrite Documents/Employment Contract (01).pdf",
                    "Overwrite Documents/Employment Contract.pdf"])
                self.assertEqual(a["Overwrite Documents/Employment Contract (01).pdf"], SIGNED)
                self.assertEqual(a["Overwrite Documents/Employment Contract.pdf"], UNSIGNED)
                self.assertEqual(a["Overwrite Documents/DBS Document (01).pdf"], DBS_GOOD)
                self.assertEqual(a["Overwrite Documents/DBS Document.pdf"], DBS_POOR)
                self.assertEqual(f.names(f.dest / "Worker B"),
                                 {"Bulk/Batch 01/Passport.pdf": PASSPORT})
                self.assertEqual(engine.stats["workers"], 2)
                self.assertEqual(engine.stats.get("workers_deferred", 0), 0)
                self.assertEqual(engine.stats.get("rank_deferred", 0), 0)
                self.assertEqual(f.receipts.call_count, 1)
                self.assertEqual(f.roster.call_count, 2)
                self.assertEqual([r["reason"] for r in f.failed_rows()], [])
            finally:
                f.close()

    def test_resumed_worker_never_repeats_a_dated_or_ranked_family(self):
        """A crash between finishing and the worker's completion marker used
        to re-date and re-rank every family on resume (repeated renames and
        re-filing; equal-key copies could reorder). Completed families are now
        recorded per worker and skipped without API calls."""
        with tempfile.TemporaryDirectory() as tmp:
            f = BatchFixture(tmp, {
                "Worker A": {"cos.pdf": ("Certificate of Sponsorship", cos("2026-01-08")),
                             "dbs-a.pdf": ("DBS Document", DBS_POOR),
                             "dbs-b.pdf": ("DBS Document", DBS_GOOD)}})
            try:
                api = f.api()
                engine = f.engine(api)
                real_finish = engine._finish_worker

                def finish_then_crash(worker_dir, records):
                    real_finish(worker_dir, records)
                    raise RuntimeError("crash after finishing")

                # first pass: finishing completes, then the process dies before
                # the worker's finishing_status/completion could be written
                with patch.object(engine, "_maybe_fix_rotation", return_value=None), \
                        patch.object(engine, "_record_roster_handover", f.roster), \
                        patch.object(engine, "_finish_worker", side_effect=finish_then_crash):
                    engine.run_batch_apply()
                crashed = f.worker_state("Worker A")
                self.assertEqual(crashed["finishing_status"], "in_progress")
                # finishing itself completed (dated, ranked, organised) before
                # the simulated crash; only the worker markers are missing
                before = f.names(f.root / "Worker A")
                self.assertEqual(sorted(before), [
                    "Overwrite Documents/Certificate of Sponsorship - (08-01-2026).pdf",
                    "Overwrite Documents/DBS Document (01).pdf",
                    "Overwrite Documents/DBS Document.pdf"])
                renames_before = len(f.rename_rows())
                quality_before = len(api.quality_calls)
                date_before = len(api.date_calls)
                f.apply(api)
                self.assertTrue(f.statuses[-1].startswith("batch_applied:"), f.statuses)
                # the hazard on the handoff source: the resumed pass re-dated
                # and re-ranked the same files (three extra rename-log rows,
                # files moved out of and back into Overwrite Documents); with
                # equal sort keys the copies could also change order
                after = f.names(f.dest / "Worker A")
                self.assertEqual(sorted(after), [
                    "Overwrite Documents/Certificate of Sponsorship - (08-01-2026).pdf",
                    "Overwrite Documents/DBS Document (01).pdf",
                    "Overwrite Documents/DBS Document.pdf"])
                self.assertEqual(after["Overwrite Documents/DBS Document (01).pdf"], DBS_GOOD)
                self.assertEqual(len(api.quality_calls), quality_before)
                self.assertEqual(len(api.date_calls), date_before)
                self.assertEqual(len(f.rename_rows()), renames_before)
                self.assertTrue(any("already dated/ranked on an earlier pass" in line
                                    for line in f.logs))
                # the durable per-family records that made the skip possible
                # (read from the state as it was after the crash; the applied
                # state file is deleted on completion)
                self.assertEqual({k: v["status"] for k, v in
                                  crashed.get("ranking_families", {}).items()},
                                 {"Certificate of Sponsorship": "complete", "DBS Document": "complete"})
            finally:
                f.close()


class TestDeferralSurvivesTheBatchWorkflow(unittest.TestCase):

    def assert_worker_a_held(self, f, engine):
        names = f.names(f.root / "Worker A")
        # unranked, unfiled, byte-to-name unchanged
        self.assertEqual(sorted(names), ["DBS Document (01).pdf", "DBS Document.pdf",
                                         "Employment Contract (2).pdf", "Employment Contract.pdf"])
        self.assertEqual(names["Employment Contract.pdf"], UNSIGNED)
        self.assertEqual(names["Employment Contract (2).pdf"], SIGNED)
        self.assertEqual(names["DBS Document (01).pdf"], DBS_GOOD)
        self.assertFalse((f.root / "Worker A" / "Overwrite Documents").exists())
        self.assertFalse((f.dest / "Worker A").exists())
        # durable, truthful state
        saved = f.saved()
        self.assertEqual(saved["phase"], "processing_incomplete")
        self.assertFalse(saved.get("processing_complete"))
        self.assertFalse(saved.get("applied"))
        state = f.worker_state("Worker A")
        self.assertEqual(state["finishing_status"], "deferred")
        self.assertFalse(state["completed"])
        self.assertNotIn("completed_ts", state)
        self.assertEqual(state["movement_status"], "not_ready")
        self.assertEqual(state["finishing_attention"]["families"], ["Employment Contract"])
        families = state["ranking_families"]
        self.assertEqual(families["DBS Document"]["status"], "complete")
        self.assertEqual(families["Employment Contract"]["status"], "deferred")
        self.assertEqual(sorted(Path(m["path"]).name for m in families["Employment Contract"]["members"]),
                         ["Employment Contract (2).pdf", "Employment Contract.pdf"])
        # no receipt, no completion, no automatic review
        self.assertEqual(f.receipts.call_count, 0)
        self.assertEqual(engine.stats.get("workers_deferred", 0), 1)
        info = f.attention()
        self.assertEqual(info["workers"], [{"name": "Worker A", "families": ["Employment Contract"]}])
        return state, info

    def test_quality_failure_holds_worker_and_completes_the_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = BatchFixture(tmp, TWO_WORKERS)
            try:
                api = f.api(fail_quality={SIGNED_PAGE1})       # the signed copy's quality fails
                engine = f.apply(api)
                state, info = self.assert_worker_a_held(f, engine)
                # Worker B is complete and moved: one held worker does not
                # block the others, and the roster/receipt reflect only B
                self.assertEqual(f.names(f.dest / "Worker B"),
                                 {"Bulk/Batch 01/Passport.pdf": PASSPORT})
                self.assertTrue(f.worker_state("Worker B")["completed"])
                self.assertEqual(f.roster.call_count, 1)
                # the failed operation is stored as failed, once, with its error
                ops = state["finishing_operations"]
                failed = [op for op in ops.values() if op["status"] == "failed"]
                self.assertEqual(len(failed), 1)
                self.assertIn("synthetic quality outage", failed[0]["error"])
                self.assertEqual(info["operations"], 1)
                self.assertEqual(info["blocked"], 0)
                self.assertTrue(info["token"])
                self.assertGreater(info["estimated_extra_gbp"], 0)
                rows = [r for r in f.failed_rows() if r["reason"] == "ranking deferred"]
                self.assertEqual({r["filename"] for r in rows},
                                 {"Employment Contract.pdf", "Employment Contract (2).pdf"})
                details = {r["filename"]: r["detail"] for r in rows}
                self.assertIn("quality assessment unavailable", details["Employment Contract (2).pdf"])
                self.assertIn("peer of a contract whose quality assessment is unavailable",
                              details["Employment Contract.pdf"])
                self.assertTrue(any("RANKING NEEDS ATTENTION" in line for line in f.logs))
            finally:
                f.close()

    def test_ordinary_check_status_replays_without_paying_and_stays_deferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = BatchFixture(tmp, TWO_WORKERS)
            try:
                first = f.api(fail_quality={SIGNED_PAGE1})
                f.apply(first)
                op_before = dict(next(op for op in f.worker_state("Worker A")["finishing_operations"].values()
                                      if op["status"] == "failed"))
                rows_before = len(f.failed_rows())
                renames_before = len(f.rename_rows())
                # the outage has cleared, but an ordinary check must not re-buy
                second = f.api()
                engine = f.apply(second)
                state, info = self.assert_worker_a_held(f, engine)
                self.assertEqual(second.quality_calls, [])
                self.assertEqual(second.signed_calls, [])
                self.assertEqual(second.date_calls, [])
                op_after = next(op for op in state["finishing_operations"].values()
                                if op["status"] == "failed")
                self.assertEqual(op_after, op_before, "stored failure untouched, no lineage added")
                self.assertEqual(state["finishing_attention"]["passes"], 2)
                self.assertEqual(state["ranking_families"]["Employment Contract"]["passes"], 2)
                self.assertEqual(len(f.rename_rows()), renames_before,
                                 "the completed DBS family is not re-ranked")
                # unresolved rows are re-recorded for this pass, nothing else
                self.assertEqual(len(f.failed_rows()), rows_before + 2)
                self.assertTrue(any("no answer recorded (RuntimeError: synthetic quality outage)" in line
                                    for line in f.logs))
                self.assertEqual(info["token"], f.engine(second).assess_unresolved_finishing()["token"])
            finally:
                f.close()

    def test_confirmed_retry_buys_only_the_failed_operation_and_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = BatchFixture(tmp, TWO_WORKERS)
            try:
                f.apply(f.api(fail_quality={SIGNED_PAGE1}))
                token = f.attention()["token"]
                failed_attempt = next(op for op in f.worker_state("Worker A")["finishing_operations"].values()
                                      if op["status"] == "failed")["attempt_id"]
                api = f.api()
                engine = f.apply(api, retry=token)
                self.assertTrue(f.statuses[-1].startswith("batch_applied:"), f.statuses)
                # exactly one new paid request: the signed copy's quality
                self.assertEqual(len(api.quality_calls), 1)
                self.assertEqual(api.quality_calls[0][0], "Employment Contract")
                self.assertIn(SIGNED_PAGE1, api.quality_calls[0][1])
                self.assertEqual(api.signed_calls, [])
                a = f.names(f.dest / "Worker A")
                self.assertEqual(a["Overwrite Documents/Employment Contract (01).pdf"], SIGNED)
                self.assertEqual(a["Overwrite Documents/Employment Contract.pdf"], UNSIGNED)
                self.assertEqual(a["Overwrite Documents/DBS Document (01).pdf"], DBS_GOOD)
                self.assertEqual(a["Overwrite Documents/DBS Document.pdf"], DBS_POOR)
                self.assertFalse(f.state.path.exists())
                self.assertEqual(f.receipts.call_count, 1)
                self.assertEqual(engine.stats["workers"], 1)
                # lineage outlives the state file: the pre-retry snapshot
                snapshots = list(f.root.glob(app.BATCH_STATE_NAME + ".recovery-*.bak"))
                self.assertEqual(len(snapshots), 1)
                snap = json.loads(snapshots[0].read_text(encoding="utf-8"))
                worker = next(w for w in snap["workers"].values() if w["name"] == "Worker A")
                self.assertEqual(worker["finishing_status"], "deferred")
                stored = [op for op in worker["finishing_operations"].values() if op["status"] == "failed"]
                self.assertEqual(stored[0]["attempt_id"], failed_attempt)
                self.assertNotIn("finishing_retries", snap)
                terminals = list(f.root.glob(
                    app.BATCH_STATE_NAME + ".terminal-*.bak"))
                self.assertEqual(len(terminals), 1)
                terminal = json.loads(terminals[0].read_text(encoding="utf-8"))
                final_worker = next(w for w in terminal["workers"].values()
                                    if w["name"] == "Worker A")
                final_op = next(op for op in
                                final_worker["finishing_operations"].values()
                                if op.get("retry_of") == failed_attempt)
                self.assertEqual(final_op["status"], "complete")
                self.assertEqual([a["attempt_id"] for a in final_op["attempts"]],
                                 [failed_attempt])
                self.assertEqual(len(terminal["finishing_retries"]), 1)
                self.assertTrue(terminal["applied"])
                self.assertIn("live_actual_gbp", terminal["costs"])
            finally:
                f.close()

    def test_retry_that_fails_again_keeps_lineage_and_needs_a_new_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = BatchFixture(tmp, TWO_WORKERS)
            try:
                f.apply(f.api(fail_quality={SIGNED_PAGE1}))
                first_info = f.attention()
                first_attempt = next(op for op in f.worker_state("Worker A")["finishing_operations"].values()
                                     if op["status"] == "failed")["attempt_id"]
                api = f.api(fail_quality={SIGNED_PAGE1})
                engine = f.apply(api, retry=first_info["token"])
                state, info = self.assert_worker_a_held(f, engine)
                self.assertEqual(len(api.quality_calls), 1)
                op = next(op for op in state["finishing_operations"].values() if op["status"] == "failed")
                self.assertNotEqual(op["attempt_id"], first_attempt)
                self.assertEqual(op["retry_of"], first_attempt)
                self.assertEqual([a["attempt_id"] for a in op["attempts"]], [first_attempt])
                self.assertEqual(op["attempts"][0]["status"], "failed")
                self.assertIn("synthetic quality outage", op["attempts"][0]["error"])
                retries = f.saved()["finishing_retries"]
                self.assertEqual(len(retries), 1)
                self.assertEqual(retries[0]["token"], first_info["token"])
                self.assertEqual(retries[0]["operations"][0][3], first_attempt)
                self.assertTrue(Path(retries[0]["snapshot"]).is_file())
                # a new attempt id means a new token: the old confirmation is spent
                self.assertNotEqual(info["token"], first_info["token"])
                stale = f.api()
                f.apply(stale, retry=first_info["token"])
                self.assertEqual(stale.quality_calls, [])
                self.assertTrue(f.attention().get("stale"))
                self.assertEqual(len(list(f.root.glob("*.recovery-*.bak"))), 1,
                                 "a refused retry takes no snapshot")
            finally:
                f.close()

    def test_stale_or_foreign_token_is_refused_without_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = BatchFixture(tmp, TWO_WORKERS)
            try:
                f.apply(f.api(fail_quality={SIGNED_PAGE1}))
                api = f.api()
                f.apply(api, retry="not-the-assessed-token")
                self.assertEqual(api.quality_calls, [])
                info = f.attention()
                self.assertTrue(info["stale"])
                self.assertEqual(info["operations"], 1)
                self.assertEqual(f.worker_state("Worker A")["finishing_status"], "deferred")
                self.assertNotIn("finishing_retries", f.saved())
            finally:
                f.close()

    def test_submission_started_operation_is_never_retried_even_when_authorized(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = SecondPassFixture(tmp)
            try:
                unsigned, signed, _records = f.contract_pair()
                op = signed_op(signed)
                state = f.batch_state({op: {"status": "submission_started",
                                            "attempt_id": "ambiguous"}})
                f.engine._batch_state = state
                key = str(f.worker.resolve()).casefold()
                f.engine._authorized_finishing_retries = {(key, op): ("submission_started", "ambiguous")}
                callback = Mock()
                with self.assertRaises(app.FinishingAmbiguous):
                    f.engine._finishing_operation(f.worker, op, callback)
                callback.assert_not_called()
                assessment_state = app.BatchState(f.root)
                worker = next(iter(assessment_state.data["workers"].values()))
                worker["finishing_status"] = "deferred"
                worker["ranking_families"] = {"Employment Contract": {
                    "status": "deferred",
                    "members": [
                        {"path": str(unsigned), "hash": app.file_hash(unsigned)},
                        {"path": str(signed), "hash": app.file_hash(signed)}],
                    "unavailable": [{"path": str(signed), "operation": op,
                                     "kind": "contract-signed", "reason": "x"}]}}
                report = f.engine.assess_unresolved_finishing(assessment_state)
                self.assertEqual(report["operations"], 0)
                self.assertEqual([b["operation"] for b in report["blocked"]], [op])
                self.assertEqual(report["token"], "")
            finally:
                f.close()

    def test_retry_token_refuses_changed_member_before_assessment(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = BatchFixture(tmp, TWO_WORKERS)
            try:
                f.apply(f.api(fail_quality={SIGNED_PAGE1}))
                changed = f.root / "Worker A" / "Employment Contract (2).pdf"
                changed.write_bytes(pdf_bytes("changed after the failed attempt"))
                assessment = f.engine(f.api()).assess_unresolved_finishing()
                self.assertEqual(assessment["retryable"], [])
                self.assertEqual(assessment["token"], "")
                self.assertEqual(len(assessment["changed"]), 1)
                self.assertIn("changed", assessment["changed"][0]["input_error"])
            finally:
                f.close()

    def test_confirmed_retry_refuses_mutation_after_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = BatchFixture(tmp, TWO_WORKERS)
            try:
                f.apply(f.api(fail_quality={SIGNED_PAGE1}))
                token = f.attention()["token"]
                changed = f.root / "Worker A" / "Employment Contract (2).pdf"
                changed_bytes = pdf_bytes("changed after confirmation")
                changed.write_bytes(changed_bytes)
                api = f.api()
                f.apply(api, retry=token)
                info = f.attention()
                self.assertTrue(info["stale"])
                self.assertEqual(info["changed"], 1)
                self.assertEqual(api.quality_calls, [])
                self.assertTrue(f.state.path.exists())
                self.assertTrue((f.root / "Worker A").is_dir())
                self.assertFalse((f.dest / "Worker A").exists())
                self.assertEqual(changed.read_bytes(), changed_bytes)
                self.assertEqual(f.receipts.call_count, 0)
            finally:
                f.close()

    def test_retry_rehashes_at_call_boundary_and_never_buys_fresh_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = BatchFixture(tmp, TWO_WORKERS)
            try:
                f.apply(f.api(fail_quality={SIGNED_PAGE1}))
                state = app.BatchState(f.root)
                api = f.api()
                engine = f.engine(api)
                engine._batch_state = state
                assessment = engine.assess_unresolved_finishing(state)
                item = assessment["retryable"][0]
                engine._authorized_finishing_retries = {
                    (item["worker_key"], item["operation"]): {
                        "status": item["status"],
                        "attempt_id": item["attempt_id"],
                        "binding": item["binding"]}}
                engine._finishing_retry_active = True
                path = Path(item["path"])
                path.write_bytes(pdf_bytes("changed after execution recheck"))
                callback = Mock()
                with self.assertRaises(app.FinishingInputChanged):
                    engine._finishing_operation(
                        f.root / "Worker A", item["operation"], callback,
                        evidence_path=path)
                callback.assert_not_called()
                # A new hash/operation discovered during the confirmed retry is
                # not silently treated as an ordinary fresh request either.
                fresh = "quality:Employment Contract:" + app.file_hash(path)
                with self.assertRaises(app.FinishingInputChanged):
                    engine._finishing_operation(
                        f.root / "Worker A", fresh, callback,
                        evidence_path=path)
                callback.assert_not_called()
            finally:
                f.close()

    def test_retry_rechecks_the_whole_family_at_call_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = BatchFixture(tmp, TWO_WORKERS)
            try:
                f.apply(f.api(fail_quality={SIGNED_PAGE1}))
                state = app.BatchState(f.root)
                engine = f.engine(f.api())
                engine._batch_state = state
                assessment = engine.assess_unresolved_finishing(state)
                item = assessment["retryable"][0]
                engine._authorized_finishing_retries = {
                    (item["worker_key"], item["operation"]): {
                        "status": item["status"],
                        "attempt_id": item["attempt_id"],
                        "binding": item["binding"]}}
                engine._finishing_retry_active = True
                evidence = Path(item["path"])
                sibling = next(path for path in
                               (f.root / "Worker A").glob("Employment Contract*.pdf")
                               if path != evidence)
                sibling.write_bytes(pdf_bytes("peer changed after authorization"))
                callback = Mock()
                with self.assertRaises(app.FinishingInputChanged):
                    engine._finishing_operation(
                        f.root / "Worker A", item["operation"], callback,
                        evidence_path=evidence)
                callback.assert_not_called()
            finally:
                f.close()

    def test_unmatched_added_file_blocks_worker_completion_and_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = BatchFixture(tmp, TWO_WORKERS)
            try:
                added = f.root / "Worker A" / "added-after-submit.pdf"
                added_bytes = pdf_bytes("not in the submitted inventory")
                added.write_bytes(added_bytes)
                api = f.api()
                engine = f.apply(api)
                self.assertEqual(f.statuses[-1], "batch_apply_incomplete")
                self.assertTrue(f.state.path.exists())
                self.assertTrue((f.root / "Worker A").is_dir())
                self.assertFalse((f.dest / "Worker A").exists())
                self.assertEqual(added.read_bytes(), added_bytes)
                self.assertEqual(f.receipts.call_count, 0)
                self.assertTrue(any("unmatched current file" in line
                                    for line in f.logs))
                self.assertEqual(engine.stats["workers"], 1)
            finally:
                f.close()

    def test_batch_retry_missing_peer_stops_before_shared_organisation(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = BatchFixture(tmp, TWO_WORKERS)
            try:
                f.apply(f.api(fail_quality={SIGNED_PAGE1}))
                token = f.attention()["token"]
                signed = f.root / "Worker A" / "Employment Contract (2).pdf"
                api = f.api()
                engine = f.engine(api)
                original = engine._finish_worker

                def remove_then_finish(worker, records):
                    if worker.name == "Worker A":
                        signed.unlink()
                    return original(worker, records)

                with patch.object(engine, "_maybe_fix_rotation", return_value=None), \
                        patch.object(engine, "_record_roster_handover", f.roster), \
                        patch.object(engine, "_record_review_processing", f.receipts), \
                        patch.object(engine, "_run_post_run_audit"), \
                        patch.object(engine, "_finish_worker",
                                     side_effect=remove_then_finish):
                    engine.run_batch_apply(retry_unresolved=token)
                self.assertTrue(f.statuses[-1].startswith(
                    ("batch_apply_attention:", "batch_apply_incomplete")))
                self.assertTrue(f.state.path.exists())
                self.assertFalse((f.dest / "Worker A").exists())
                self.assertFalse((f.root / "Worker A" /
                                  "Overwrite Documents").exists())
                self.assertEqual(api.quality_calls, [])
                self.assertEqual(f.receipts.call_count, 0)
            finally:
                f.close()

    def test_batch_mocked_early_success_still_rejects_unconsumed_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = BatchFixture(tmp, TWO_WORKERS)
            try:
                f.apply(f.api(fail_quality={SIGNED_PAGE1}))
                token = f.attention()["token"]
                api = f.api()
                engine = f.engine(api)
                with patch.object(engine, "_maybe_fix_rotation", return_value=None), \
                        patch.object(engine, "_record_roster_handover", f.roster), \
                        patch.object(engine, "_record_review_processing", f.receipts), \
                        patch.object(engine, "_run_post_run_audit"), \
                        patch.object(engine, "_finish_worker", return_value={
                            "deferred": [], "organised": True}):
                    engine.run_batch_apply(retry_unresolved=token)
                self.assertEqual(f.statuses[-1], "batch_apply_incomplete")
                self.assertTrue(f.state.path.exists())
                self.assertFalse(f.worker_state("Worker A").get("completed"))
                self.assertFalse((f.dest / "Worker A").exists())
                self.assertEqual(api.quality_calls, [])
                self.assertEqual(len(engine._authorized_finishing_retries), 1)
                self.assertEqual(f.receipts.call_count, 0)
            finally:
                f.close()

    def test_terminal_receipt_failure_retains_resumable_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = BatchFixture(tmp, TWO_WORKERS)
            try:
                f.apply(f.api(fail_quality={SIGNED_PAGE1}))
                token = f.attention()["token"]
                api = f.api()
                with patch.object(app.BatchState, "terminal_snapshot",
                                  side_effect=OSError("synthetic receipt outage")):
                    f.apply(api, retry=token)
                saved = f.saved()
                self.assertTrue(saved["processing_complete"])
                self.assertFalse(saved["applied"])
                self.assertTrue(f.state.path.exists())
                worker = next(w for w in saved["workers"].values()
                              if w["name"] == "Worker A")
                final_op = next(op for op in
                                worker["finishing_operations"].values()
                                if op.get("retry_of"))
                self.assertEqual(final_op["status"], "complete")
                self.assertEqual(len(api.quality_calls), 1)
                # A later status check finalises without another paid request.
                resumed = f.api()
                f.apply(resumed)
                self.assertEqual(resumed.quality_calls, [])
                self.assertFalse(f.state.path.exists())
                self.assertEqual(len(list(f.root.glob(
                    app.BATCH_STATE_NAME + ".terminal-*.bak"))), 1)
            finally:
                f.close()


class TestUnavailableQualityIsNotScoreZero(unittest.TestCase):
    """Local `_second_pass` controls, mirroring the signature tri-state."""

    def dbs_pair(self, f):
        poor = f.file("DBS Document.pdf", DBS_POOR)
        good = f.file("DBS Document (2).pdf", DBS_GOOD)
        return poor, good, [f.record(poor, "DBS Document"), f.record(good, "DBS Document")]

    def dbs_names(self, f):
        return {p.name: p.read_bytes() for p in f.worker.glob("DBS Document*.pdf")}

    def assert_untouched(self, f, records, poor, good):
        names = self.dbs_names(f)
        self.assertEqual(sorted(names), ["DBS Document (2).pdf", "DBS Document.pdf"])
        self.assertEqual(names["DBS Document.pdf"], DBS_POOR)
        self.assertEqual(names["DBS Document (2).pdf"], DBS_GOOD)
        self.assertEqual([r["path"] for r in records], [poor, good])
        self.assertEqual([p.name for p in f.worker.glob("__rank_tmp_*")], [])
        self.assertEqual(f.engine.stats.get("rank_deferred", 0), 1)
        rows = f.failed_rows()
        self.assertEqual([r["reason"] for r in rows], ["ranking deferred"] * 2)

    def with_state(self, f, operations):
        state = f.batch_state(operations)
        f.engine._batch_state = state
        f.engine._committed_batch_cost_gbp = 0.0
        f.engine._committed_batch_tokens = 0
        f.engine._persisted_live_cost_gbp = 0.0
        f.engine._persisted_live_tokens = 0
        return state

    def test_fresh_quality_failure_defers_instead_of_demoting_the_best_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = SecondPassFixture(tmp, api=FinishingAPI(fail_quality={"DBS-B"}))
            try:
                poor, good, records = self.dbs_pair(f)
                outcome = f.engine._second_pass(f.worker, records)
                self.assert_untouched(f, records, poor, good)
                self.assertEqual(f.engine.stats["errors"], 1)
                self.assertEqual(len(f.api.quality_calls), 2, "the peer is still assessed once")
                self.assertEqual([d["name"] for d in outcome["deferred"]], ["DBS Document"])
                self.assertEqual(outcome["deferred"][0]["unavailable"][0]["kind"], "quality:DBS Document")
                self.assertTrue(any("quality assessment unavailable (RuntimeError: synthetic quality outage)" in line
                                    for line in f.logs))
                self.assertTrue(any("ranking deferred, names unchanged" in line for line in f.logs))
            finally:
                f.close()

    def test_stored_failed_quality_on_restart_is_unavailable_and_not_rebought(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = SecondPassFixture(tmp, api=FinishingAPI())
            try:
                poor, good, records = self.dbs_pair(f)
                op = f"quality:DBS Document:{app.file_hash(good)}"
                stored_error = "APIError: 529 overloaded"
                state = self.with_state(f, {op: {"status": "failed", "result": None,
                                                 "error": stored_error, "attempt_id": "earlier"}})
                f.engine._second_pass(f.worker, records)
                self.assert_untouched(f, records, poor, good)
                self.assertEqual(f.engine.stats["errors"], 1)
                self.assertEqual(len(f.api.quality_calls), 1)
                self.assertIn("DBS-A", f.api.quality_calls[0][1])
                self.assertTrue(any(stored_error in line and "quality assessment unavailable" in line
                                    for line in f.logs))
                saved = json.loads(state.path.read_text(encoding="utf-8"))
                worker = next(iter(saved["workers"].values()))
                self.assertEqual(worker["finishing_operations"][op]["status"], "failed")
                self.assertEqual(worker["finishing_operations"][op]["error"], stored_error)
                self.assertEqual(worker["ranking_families"]["DBS Document"]["status"], "deferred")
                self.assertEqual(worker["ranking_families"]["DBS Document"]["unavailable"][0]["operation"], op)
                self.assertTrue(worker["ranking_families"]["DBS Document"]["unavailable"][0]["stored"])
            finally:
                f.close()

    def test_invalid_stored_answer_is_unavailable_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = SecondPassFixture(tmp, api=FinishingAPI())
            try:
                poor, good, records = self.dbs_pair(f)
                op = f"quality:DBS Document:{app.file_hash(good)}"
                for garbage in ("not a dict", ["list"], {"legible": True}, {"score": "n/a"},
                                {"score": True}):
                    with self.subTest(garbage=garbage):
                        self.assertIsNone(app.quality_evidence(garbage))
                self.with_state(f, {op: {"status": "complete", "result": {"legible": True}}})
                f.engine._second_pass(f.worker, records)
                self.assert_untouched(f, records, poor, good)
                self.assertEqual(len(f.api.quality_calls), 1)
                self.assertTrue(any("invalid answer recorded (dict)" in line for line in f.logs))
            finally:
                f.close()

    def test_genuine_zero_score_and_absent_flags_are_assessments(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = SecondPassFixture(tmp, api=FinishingAPI())
            try:
                poor, good, records = self.dbs_pair(f)
                # the poor copy's stored answer is a real zero without the
                # optional flags; the good copy's answer is a stored 95
                self.with_state(f, {
                    f"quality:DBS Document:{app.file_hash(poor)}": {
                        "status": "complete", "result": {"score": 0}},
                    f"quality:DBS Document:{app.file_hash(good)}": {
                        "status": "complete", "result": {"score": 95, "legible": True, "complete": True}}})
                self.assertEqual(app.quality_evidence({"score": 0}),
                                 {"score": 0, "legible": False, "complete": False, "date": "", "note": ""})
                self.assertEqual(app.quality_evidence({"score": "100.0", "note": None})["score"], 100)
                outcome = f.engine._second_pass(f.worker, records)
                names = self.dbs_names(f)
                self.assertEqual(sorted(names), ["DBS Document (01).pdf", "DBS Document.pdf"])
                self.assertEqual(names["DBS Document (01).pdf"], DBS_GOOD)
                self.assertEqual(names["DBS Document.pdf"], DBS_POOR)
                self.assertEqual(f.api.quality_calls, [])
                self.assertEqual(f.engine.stats["errors"], 0)
                self.assertEqual(f.engine.stats.get("rank_deferred", 0), 0)
                self.assertEqual([c["name"] for c in outcome["completed"]], ["DBS Document"])
                self.assertEqual(outcome["deferred"], [])
                self.assertTrue(any(": score 0" in line for line in f.logs))
            finally:
                f.close()

    def test_complete_valid_pair_ranks_by_score_with_two_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = SecondPassFixture(tmp, api=FinishingAPI())
            try:
                poor, good, records = self.dbs_pair(f)
                f.engine._second_pass(f.worker, records)
                names = self.dbs_names(f)
                self.assertEqual(names["DBS Document (01).pdf"], DBS_GOOD)
                self.assertEqual(names["DBS Document.pdf"], DBS_POOR)
                self.assertEqual(len(f.api.quality_calls), 2)
                self.assertEqual(f.engine.stats.get("rank_deferred", 0), 0)
                self.assertEqual(f.failed_rows(), [])
            finally:
                f.close()

    def test_quality_and_signature_failures_on_different_copies_hold_the_family(self):
        """Both checks are still attempted for every copy, so a later retry
        needs only the operations that actually failed."""
        with tempfile.TemporaryDirectory() as tmp:
            f = SecondPassFixture(tmp, api=FinishingAPI(fail_quality={"QUALITY 90"}, fail_signed=True))
            try:
                unsigned, signed, records = f.contract_pair()
                outcome = f.engine._second_pass(f.worker, records)
                names = f.contract_names()
                self.assertEqual(sorted(names), ["Employment Contract (2).pdf", "Employment Contract.pdf"])
                self.assertIn(MARK, names["Employment Contract (2).pdf"])
                self.assertEqual(len(f.api.quality_calls), 2)
                self.assertEqual(len(f.api.signed_calls), 2)
                kinds = sorted(u["kind"] for u in outcome["deferred"][0]["unavailable"])
                self.assertEqual(kinds, ["contract-signed", "contract-signed", "quality:Employment Contract"])
            finally:
                f.close()


class TestDateEvidenceBoundary(unittest.TestCase):
    """A failed date operation is unavailable evidence; an answer with no
    supported date is a legitimate absence and still ranks lowest, undated."""

    def test_failed_share_code_date_defers_but_absent_date_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = SecondPassFixture(tmp, api=FinishingAPI(fail_date={"SC-B"}))
            try:
                a = f.file("Share Code Check Result.pdf", share_code("2023-07-31", 60, "SC-A"))
                b = f.file("Share Code Check Result (2).pdf", share_code("2025-11-28", 60, "SC-B"))
                records = [f.record(a, "Share Code Check Result"), f.record(b, "Share Code Check Result")]
                outcome = f.engine._second_pass(f.worker, records)
                names = {p.name: p.read_bytes() for p in f.worker.glob("Share Code*.pdf")}
                self.assertEqual(sorted(names), ["Share Code Check Result (2).pdf", "Share Code Check Result.pdf"])
                self.assertEqual(names["Share Code Check Result.pdf"], a.read_bytes())
                self.assertEqual([d["name"] for d in outcome["deferred"]], ["Share Code Check Result"])
                self.assertEqual(outcome["deferred"][0]["unavailable"][0]["kind"], "share-code-date")
                # both copies are still quality-assessed once, so a retry
                # needs only the failed date operation
                self.assertEqual(len(f.api.quality_calls), 2)
                self.assertTrue(any("Share Code check date unavailable" in line for line in f.logs))
            finally:
                f.close()
        with tempfile.TemporaryDirectory() as tmp:
            f = SecondPassFixture(tmp, api=FinishingAPI())
            try:
                # the same pair, but copy B's page carries no check date at all
                a = f.file("Share Code Check Result.pdf", share_code("2023-07-31", 60, "SC-A"))
                undated = pdf_bytes(page_texts=["Home Office right to work result SC-B QUALITY 60"])
                b = f.file("Share Code Check Result (2).pdf", undated)
                records = [f.record(a, "Share Code Check Result"), f.record(b, "Share Code Check Result")]
                outcome = f.engine._second_pass(f.worker, records)
                names = {p.name: p.read_bytes() for p in f.worker.glob("Share Code*.pdf")}
                self.assertEqual(sorted(names), ["Share Code Check Result - (31-07-2023) (01).pdf",
                                                 "Share Code Check Result.pdf"])
                self.assertEqual(names["Share Code Check Result.pdf"], undated)
                self.assertEqual(outcome["deferred"], [])
                self.assertEqual(f.engine.stats["errors"], 0)
            finally:
                f.close()

    def test_single_cos_with_failed_date_is_held_but_absent_date_stays_bare(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = SecondPassFixture(tmp, api=FinishingAPI(fail_date={"COS-X"}))
            try:
                only = f.file("Certificate of Sponsorship.pdf", cos("2026-01-08", "COS-X"))
                records = [f.record(only, "Certificate of Sponsorship")]
                outcome = f.engine._second_pass(f.worker, records)
                self.assertEqual([p.name for p in f.worker.glob("*.pdf")], ["Certificate of Sponsorship.pdf"])
                self.assertEqual([d["name"] for d in outcome["deferred"]], ["Certificate of Sponsorship"])
                rows = f.failed_rows()
                self.assertEqual([r["reason"] for r in rows], ["ranking deferred"])
                self.assertIn("CoS issue date unavailable", rows[0]["detail"])
            finally:
                f.close()
        with tempfile.TemporaryDirectory() as tmp:
            f = SecondPassFixture(tmp, api=FinishingAPI())
            try:
                only = f.file("Certificate of Sponsorship.pdf", pdf_bytes("CoS page without any date"))
                records = [f.record(only, "Certificate of Sponsorship")]
                state = f.batch_state({})
                f.engine._batch_state = state
                f.engine._committed_batch_cost_gbp = 0.0
                f.engine._committed_batch_tokens = 0
                f.engine._persisted_live_cost_gbp = 0.0
                f.engine._persisted_live_tokens = 0
                outcome = f.engine._second_pass(f.worker, records)
                self.assertEqual([p.name for p in f.worker.glob("*.pdf")], ["Certificate of Sponsorship.pdf"])
                self.assertEqual(outcome["deferred"], [])
                self.assertEqual([c["name"] for c in outcome["completed"]], ["Certificate of Sponsorship"])
                self.assertEqual(f.failed_rows(), [])
                worker = next(iter(json.loads(state.path.read_text(encoding="utf-8"))["workers"].values()))
                self.assertEqual(worker["ranking_families"]["Certificate of Sponsorship"]["status"], "complete")
            finally:
                f.close()


class TestLiveModeStaysTruthful(unittest.TestCase):

    def test_live_failed_operation_replays_free_then_explicit_retry_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = SecondPassFixture(tmp, api=FinishingAPI(
                fail_quality={SIGNED_PAGE1}))
            try:
                _unsigned, _signed, records = f.contract_pair()
                for record in records:
                    f.engine.manifest.record(
                        app.file_hash(record["path"]), f.api.model_id, 1.0,
                        record["name"], record["group"])
                f.engine.manifest.save()

                def run_with(api, token=None, patched=True):
                    statuses = []
                    engine = f.new_engine(api)
                    engine.on_done = lambda _stats, status: statuses.append(status)
                    common = [
                        patch.object(engine, "_record_review_processing"),
                        patch.object(engine, "_run_post_run_audit",
                                     side_effect=lambda: engine.stats.update(
                                         audit_status="disabled"))]
                    if patched:
                        common.append(patch.object(
                            engine, "_process_worker",
                            side_effect=lambda _w: engine._finish_worker(
                                f.worker, records)))
                    entered = [item.start() for item in common]
                    try:
                        engine.run(retry_unresolved=token)
                    finally:
                        for item in reversed(common):
                            item.stop()
                    return engine, statuses

                first, first_status = run_with(f.api)
                self.assertTrue(first_status[-1].startswith(
                    "live_finishing_attention:"))
                checkpoint = app.read_live_checkpoint(f.root)
                self.assertFalse(checkpoint["processing_complete"])
                worker = next(iter(checkpoint["workers"].values()))
                failed = next(op for op in
                              worker["finishing_operations"].values()
                              if op["status"] == "failed")
                failed_attempt = failed["attempt_id"]

                ordinary_api = FinishingAPI()
                before = {p.name: p.read_bytes() for p in f.worker.glob("*.pdf")}
                _ordinary, ordinary_status = run_with(
                    ordinary_api, patched=False)
                self.assertTrue(ordinary_status[-1].startswith(
                    "live_finishing_attention:"))
                self.assertEqual(ordinary_api.quality_calls, [])
                self.assertEqual(ordinary_api.signed_calls, [])
                self.assertEqual({p.name: p.read_bytes()
                                  for p in f.worker.glob("*.pdf")}, before)
                assessment = f.new_engine(ordinary_api).assess_unresolved_finishing(
                    app.LiveFinishingState(f.root))
                self.assertEqual(assessment["operations"], 1)

                retry_api = FinishingAPI()
                _retry, retry_status = run_with(
                    retry_api, assessment["token"], patched=False)
                self.assertEqual(retry_status, [None])
                self.assertEqual(len(retry_api.quality_calls), 1)
                self.assertFalse((f.root / app.LIVE_CHECKPOINT_NAME).exists())
                terminals = list(f.root.glob(
                    app.LIVE_CHECKPOINT_NAME + ".terminal-*.bak"))
                self.assertEqual(len(terminals), 1)
                terminal = json.loads(terminals[0].read_text(encoding="utf-8"))
                final_worker = next(iter(terminal["workers"].values()))
                final_op = next(op for op in
                                final_worker["finishing_operations"].values()
                                if op.get("retry_of") == failed_attempt)
                self.assertEqual(final_op["status"], "complete")
                self.assertEqual(final_op["attempts"][0]["attempt_id"],
                                 failed_attempt)
            finally:
                f.close()

    def test_finish_worker_does_not_organise_a_deferred_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = SecondPassFixture(tmp, api=FinishingAPI(fail_quality={"DBS-B"}))
            try:
                poor = f.file("DBS Document.pdf", DBS_POOR)
                good = f.file("DBS Document (2).pdf", DBS_GOOD)
                passport = f.file("Passport.pdf", PASSPORT)
                records = [f.record(poor, "DBS Document"), f.record(good, "DBS Document"),
                           f.record(passport, "Passport")]
                outcome = f.engine._finish_worker(f.worker, records)
                self.assertEqual([d["name"] for d in outcome["deferred"]], ["DBS Document"])
                self.assertFalse(outcome["organised"])
                self.assertFalse((f.worker / "Overwrite Documents").exists())
                self.assertFalse((f.worker / "Bulk").exists())
                self.assertEqual(sorted(p.name for p in f.worker.glob("*.pdf")),
                                 ["DBS Document (2).pdf", "DBS Document.pdf", "Passport.pdf"])
                self.assertEqual((f.worker / "DBS Document (2).pdf").read_bytes(), DBS_GOOD)
                # the same worker, once the outage clears, finishes in place
                later = f.new_engine(FinishingAPI())
                outcome = later._finish_worker(f.worker, records)
                self.assertEqual(outcome["deferred"], [])
                self.assertTrue(outcome["organised"])
                self.assertEqual((f.worker / "Overwrite Documents" / "DBS Document (01).pdf").read_bytes(), DBS_GOOD)
                self.assertEqual((f.worker / "Bulk" / "Batch 01" / "Passport.pdf").read_bytes(), PASSPORT)
            finally:
                f.close()

    def test_live_run_leaves_deferred_worker_in_source_and_out_of_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            appdata = base / "appdata"
            appdata.mkdir()
            root, dest = base / "Files", base / "Processed"
            (root / "Worker A").mkdir(parents=True)
            (root / "Worker B").mkdir()
            dest.mkdir()
            patches = [patch.object(app, "APP_DIR", appdata),
                       patch.object(app, "RENAME_CSV", appdata / "renames.csv"),
                       patch.object(app, "FAILED_CSV", appdata / "failed.csv"),
                       patch.object(app, "OVERRIDE_XLSX", appdata / "override.xlsx"),
                       patch.object(app, "RECORD_XLSX", appdata / "record.xlsx"),
                       patch.object(app, "CONFIG_PATH", appdata / "config.json")]
            for p in patches:
                p.start()
            try:
                statuses = []
                engine = app.Engine(
                    root, KB(), FinishingAPI(), "Synthetic Home",
                    log=lambda _m: None, set_status=lambda _m: None,
                    set_progress=lambda *_a: None, set_preview=lambda *_a: None,
                    ask_unknown=lambda *_a: None, on_cost=lambda *_a: None,
                    on_done=lambda _s, status: statuses.append(status),
                    resolution=1.0, convert_pdf=False, bundle_split=False,
                    orientation_mode="off", post_run_audit=False,
                    move_mode=True, move_dest=dest)

                def process(worker_dir):
                    if worker_dir.name == "Worker A":
                        return {"deferred": [{"name": "DBS Document", "members": 2,
                                              "unavailable": []}], "organised": False}
                    return {"deferred": [], "organised": True}

                roster = Mock()

                audit = Mock(side_effect=lambda: engine.stats.update(
                    audit_status="disabled"))

                receipt = Mock()
                with patch.object(engine, "_process_worker", side_effect=process), \
                        patch.object(engine, "_record_roster_handover", roster), \
                        patch.object(engine, "_record_review_processing", receipt), \
                        patch.object(engine, "_run_post_run_audit", side_effect=audit):
                    engine.run()
                self.assertTrue(statuses[-1].startswith(
                    "live_finishing_attention:"), statuses)
                self.assertTrue((root / "Worker A").is_dir(), "deferred worker stays in source")
                self.assertFalse((dest / "Worker A").exists())
                self.assertTrue((dest / "Worker B").is_dir())
                self.assertEqual([p.name for p in engine._audit_worker_dirs], ["Worker B"])
                self.assertEqual(roster.call_count, 1)
                self.assertEqual(engine.stats["workers"], 1)
                self.assertEqual(engine.stats["workers_deferred"], 1)
                self.assertEqual(engine.stats["moved"], 1)
                checkpoint = app.read_live_checkpoint(root)
                self.assertFalse(checkpoint["processing_complete"])
                self.assertEqual(checkpoint["workers_done"], 1)
                self.assertEqual(checkpoint["workers_total"], 2)
                self.assertEqual(checkpoint["deferred_workers"], ["Worker A"])
                self.assertEqual(receipt.call_count, 0)
                self.assertEqual(audit.call_count, 0)
            finally:
                for p in patches:
                    p.stop()


class TestLiveRetryCompletionBoundary(unittest.TestCase):
    """An accepted retry cannot disappear through a live early/success path."""

    def prepare_deferred(self, tmp):
        f = SecondPassFixture(tmp, api=FinishingAPI(
            fail_quality={SIGNED_PAGE1}))
        _unsigned, signed, records = f.contract_pair()
        for record in records:
            f.engine.manifest.record(
                app.file_hash(record["path"]), f.api.model_id, 1.0,
                record["name"], record["group"])
        f.engine.manifest.save()
        statuses = []
        engine = f.new_engine(f.api)
        engine.on_done = lambda _stats, status: statuses.append(status)
        with patch.object(engine, "_record_review_processing"), patch.object(
                engine, "_run_post_run_audit",
                side_effect=lambda: engine.stats.update(
                    audit_status="disabled")), patch.object(
                engine, "_process_worker",
                side_effect=lambda _worker: engine._finish_worker(
                    f.worker, records)):
            engine.run()
        self.assertTrue(statuses[-1].startswith("live_finishing_attention:"))
        assessment = f.new_engine(FinishingAPI()).assess_unresolved_finishing(
            app.LiveFinishingState(f.root))
        self.assertEqual(assessment["operations"], 1)
        return f, signed, records, assessment

    def run_retry(self, f, token, process_factory=None, move=False,
                  worker_dirs_side_effect=None):
        api = FinishingAPI()
        statuses = []
        engine = f.new_engine(api)
        engine.on_done = lambda _stats, status: statuses.append(status)
        destination = f.root.parent / "Processed"
        if move:
            destination.mkdir(exist_ok=True)
            engine.move_mode = True
            engine.move_dest = destination
        receipt, audit = Mock(), Mock(side_effect=lambda: engine.stats.update(
            audit_status="disabled"))
        patches = [patch.object(engine, "_record_review_processing", receipt),
                   patch.object(engine, "_run_post_run_audit", audit)]
        if process_factory is not None:
            patches.append(patch.object(
                engine, "_process_worker", side_effect=process_factory(engine)))
        if worker_dirs_side_effect is not None:
            patches.append(patch.object(
                app, "worker_dirs_in", side_effect=worker_dirs_side_effect))
        for item in patches:
            item.start()
        try:
            engine.run(retry_unresolved=token)
        finally:
            for item in reversed(patches):
                item.stop()
        return engine, api, statuses, receipt, audit, destination

    def assert_retry_held(self, f, engine, api, statuses, receipt, audit,
                          completed_workers=0):
        self.assertTrue(statuses[-1].startswith(
            "live_finishing_attention:"), statuses)
        payload = json.loads(statuses[-1].partition(":")[2])
        self.assertTrue(payload["stale"])
        self.assertTrue(
            "not consumed" in payload["input_error"]
            or "saved family member" in payload["input_error"], payload)
        checkpoint = app.read_live_checkpoint(f.root)
        self.assertFalse(checkpoint["processing_complete"])
        self.assertIn("finishing_input_error", checkpoint)
        self.assertEqual(len(engine._authorized_finishing_retries), 1)
        self.assertEqual(api.quality_calls, [])
        self.assertEqual(api.signed_calls, [])
        self.assertEqual(engine.stats["workers"], completed_workers)
        self.assertGreaterEqual(engine.stats["errors"], 1)
        self.assertEqual(receipt.call_count, 0)
        self.assertEqual(audit.call_count, 0)
        self.assertEqual(list(f.root.glob(
            app.LIVE_CHECKPOINT_NAME + ".terminal-*.bak")), [])
        operations = next(iter(checkpoint["workers"].values()))[
            "finishing_operations"].values()
        failed = [operation for operation in operations
                  if operation["status"] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertIsNone(failed[0]["result"])
        self.assertTrue(failed[0]["attempt_id"])
        self.assertIn("live_actual_gbp", checkpoint["costs"])
        # The accepted retry record is durable but no new attempt was fabricated.
        self.assertTrue(checkpoint["finishing_retries"])

    def test_removed_peer_after_confirmation_stops_before_organisation_or_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            f, signed, records, assessment = self.prepare_deferred(tmp)
            try:
                def factory(engine):
                    def process(_worker):
                        signed.unlink()
                        return engine._finish_worker(f.worker, records)
                    return process

                result = self.run_retry(
                    f, assessment["token"], factory, move=True)
                engine, api, statuses, receipt, audit, destination = result
                self.assert_retry_held(
                    f, engine, api, statuses, receipt, audit)
                self.assertFalse((f.worker / "Overwrite Documents").exists())
                self.assertFalse((f.worker / "Bulk").exists())
                self.assertTrue(f.worker.is_dir())
                self.assertFalse((destination / f.worker.name).exists())
            finally:
                f.close()

    def test_all_files_removed_after_confirmation_stops_no_document_return(self):
        with tempfile.TemporaryDirectory() as tmp:
            f, _signed, _records, assessment = self.prepare_deferred(tmp)
            try:
                def factory(engine):
                    original = engine._process_worker

                    def process(worker):
                        for path in list(worker.glob("*.pdf")):
                            path.unlink()
                        return original(worker)
                    return process

                engine, api, statuses, receipt, audit, _dest = self.run_retry(
                    f, assessment["token"], factory)
                self.assert_retry_held(
                    f, engine, api, statuses, receipt, audit)
                self.assertTrue(f.worker.is_dir())
            finally:
                f.close()

    def test_worker_folder_removed_after_confirmation_stops_empty_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            f, _signed, _records, assessment = self.prepare_deferred(tmp)
            try:
                def no_workers(_root):
                    shutil.rmtree(f.worker)
                    return []

                engine, api, statuses, receipt, audit, _dest = self.run_retry(
                    f, assessment["token"],
                    worker_dirs_side_effect=no_workers)
                self.assert_retry_held(
                    f, engine, api, statuses, receipt, audit)
                self.assertFalse(f.worker.exists())
            finally:
                f.close()

    def test_mocked_early_success_cannot_bypass_live_completion_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            f, _signed, _records, assessment = self.prepare_deferred(tmp)
            try:
                factory = lambda _engine: (lambda _worker: {
                    "deferred": [], "organised": True})
                engine, api, statuses, receipt, audit, _dest = self.run_retry(
                    f, assessment["token"], factory)
                self.assert_retry_held(
                    f, engine, api, statuses, receipt, audit)
                self.assertFalse((f.worker / "Overwrite Documents").exists())
            finally:
                f.close()

    def test_partial_multiworker_resume_keeps_prior_completion_and_holds_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            f, signed, records, assessment = self.prepare_deferred(tmp)
            try:
                signed_bytes = signed.read_bytes()
                safe = f.root / "A Safe Worker"
                safe.mkdir()
                (safe / "Passport.pdf").write_bytes(PASSPORT)

                def factory(engine):
                    def process(worker):
                        if worker == safe:
                            return {"deferred": [], "organised": True}
                        signed.unlink()
                        return engine._finish_worker(f.worker, records)
                    return process

                result = self.run_retry(
                    f, assessment["token"], factory, move=True)
                engine, api, statuses, receipt, audit, destination = result
                self.assertTrue((destination / safe.name).is_dir())
                self.assertEqual(engine.stats["workers"], 1)
                self.assert_retry_held(
                    f, engine, api, statuses, receipt, audit,
                    completed_workers=1)
                checkpoint = app.read_live_checkpoint(f.root)
                self.assertEqual(checkpoint["workers_done"], 1)
                self.assertEqual(checkpoint["workers_total"], 2)
                self.assertEqual([Path(path).name for path in
                                  checkpoint["audit_worker_dirs"]],
                                 [safe.name])
                safe_state = next(worker for worker in
                                  checkpoint["workers"].values()
                                  if worker.get("name") == safe.name)
                self.assertTrue(safe_state["completed"])
                self.assertEqual(Path(safe_state["final_path"]),
                                 destination / safe.name)
                costs_before = dict(checkpoint["costs"])
                failed_before = next(operation for worker in
                                     checkpoint["workers"].values()
                                     for operation in
                                     (worker.get("finishing_operations") or {}).values()
                                     if operation.get("status") == "failed")
                failed_attempt = failed_before["attempt_id"]

                signed.write_bytes(signed_bytes)
                resume_api = FinishingAPI()
                resume = f.new_engine(resume_api)
                resume.move_mode = True
                resume.move_dest = destination
                resume._audit_worker_dirs = [
                    Path(path) for path in checkpoint["audit_worker_dirs"]]
                retry = resume.assess_unresolved_finishing(
                    app.LiveFinishingState(f.root))
                self.assertTrue(retry["token"])
                resume_statuses, receipt_scope, audit_scope = [], [], []
                resume.on_done = lambda _stats, status: resume_statuses.append(status)

                def capture_receipt():
                    receipt_scope[:] = [path.name for path in
                                        resume._audit_worker_dirs]

                def capture_audit():
                    audit_scope[:] = [path.name for path in
                                      resume._audit_worker_dirs]
                    resume.stats["audit_status"] = "disabled"

                with patch.object(resume, "_record_review_processing",
                                  side_effect=capture_receipt), patch.object(
                        resume, "_run_post_run_audit",
                        side_effect=capture_audit), patch.object(
                        resume, "_process_worker",
                        side_effect=lambda _worker: resume._finish_worker(
                            f.worker, records)):
                    resume.run(retry_unresolved=retry["token"])
                self.assertEqual(resume_statuses, [None])
                self.assertEqual(len(resume_api.quality_calls), 1)
                self.assertEqual(set(receipt_scope), {safe.name, f.worker.name})
                self.assertEqual(set(audit_scope), {safe.name, f.worker.name})
                self.assertFalse((f.root / app.LIVE_CHECKPOINT_NAME).exists())
                terminal_path = next(f.root.glob(
                    app.LIVE_CHECKPOINT_NAME + ".terminal-*.bak"))
                terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
                self.assertTrue(terminal["processing_complete"])
                self.assertGreaterEqual(
                    terminal["costs"]["live_actual_gbp"],
                    costs_before["live_actual_gbp"])
                self.assertGreaterEqual(
                    terminal["costs"]["live_tokens"],
                    costs_before["live_tokens"])
                self.assertTrue(terminal["costs"]["updated_ts"])
                retried = next(operation for worker in
                               terminal["workers"].values()
                               for operation in
                               (worker.get("finishing_operations") or {}).values()
                               if operation.get("retry_of") == failed_attempt)
                self.assertEqual(retried["status"], "complete")
                self.assertEqual(retried["attempts"][0]["attempt_id"],
                                 failed_attempt)
            finally:
                f.close()

    def test_live_state_save_merges_newer_checkpoint_progress_and_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            care = Path(tmp) / "Home"
            audit_worker = Path(tmp) / "Processed" / "Completed Worker"
            care.mkdir()
            audit_worker.mkdir(parents=True)
            state = app.LiveFinishingState(care)
            state.data.update({
                "model_id": "offline", "resolution": 1.0,
                "workers": {"semantic-worker": {"completed": True}},
                "costs": {"live_actual_gbp": 1.25}})
            self.assertTrue(state.save())
            self.assertTrue(app.write_live_checkpoint(
                care, 1, 2, "Deferred Worker", 0, False,
                "review-123", [audit_worker], processing_complete=False,
                deferred_workers=["Deferred Worker"]))
            # The long-lived state still has the old view until its next save.
            state.data["workers"]["semantic-worker"]["attempt"] = "retained"
            state.data["costs"]["live_tokens"] = 42
            self.assertTrue(state.save())
            saved = app.read_live_checkpoint(care)
            self.assertEqual((saved["workers_done"], saved["workers_total"]),
                             (1, 2))
            self.assertEqual(saved["current_worker"], "Deferred Worker")
            self.assertEqual(saved["auto_review_run_id"], "review-123")
            self.assertEqual(saved["audit_worker_dirs"], [
                str(audit_worker.resolve())])
            self.assertEqual(saved["deferred_workers"], ["Deferred Worker"])
            self.assertEqual(saved["workers"]["semantic-worker"]["attempt"],
                             "retained")
            self.assertEqual(saved["costs"], {
                "live_actual_gbp": 1.25, "live_tokens": 42})

    def test_attention_marker_save_failure_never_manufactures_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            f, signed, records, assessment = self.prepare_deferred(tmp)
            try:
                safe = f.root / "A Safe Worker"
                safe.mkdir()
                (safe / "Passport.pdf").write_bytes(PASSPORT)

                def factory(engine):
                    def process(worker):
                        if worker == safe:
                            return {"deferred": [], "organised": True}
                        signed.unlink()
                        return engine._finish_worker(f.worker, records)
                    return process

                with patch.object(app.LiveFinishingState, "save_progress",
                                  return_value=False):
                    result = self.run_retry(
                        f, assessment["token"], factory, move=True)
                engine, api, statuses, receipt, audit, destination = result
                self.assertTrue(statuses[-1].startswith(
                    "live_finishing_attention:"), statuses)
                checkpoint = app.read_live_checkpoint(f.root)
                self.assertFalse(checkpoint["processing_complete"])
                self.assertEqual((checkpoint["workers_done"],
                                  checkpoint["workers_total"]), (1, 2))
                self.assertEqual([Path(path).name for path in
                                  checkpoint["audit_worker_dirs"]],
                                 [safe.name])
                self.assertTrue((destination / safe.name).is_dir())
                self.assertTrue(f.worker.is_dir())
                self.assertEqual(api.quality_calls, [])
                self.assertEqual(receipt.call_count, 0)
                self.assertEqual(audit.call_count, 0)
                self.assertEqual(engine.stats["workers"], 1)
                self.assertTrue((f.root / app.LIVE_CHECKPOINT_NAME).exists())
            finally:
                f.close()

    def test_ordinary_empty_worker_without_retry_completes_normally(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = SecondPassFixture(tmp, api=FinishingAPI())
            try:
                statuses = []
                engine = f.new_engine(f.api)
                engine.on_done = lambda _stats, status: statuses.append(status)
                receipt = Mock()
                with patch.object(engine, "_record_review_processing", receipt), \
                        patch.object(engine, "_run_post_run_audit",
                                     side_effect=lambda: engine.stats.update(
                                         audit_status="disabled")):
                    engine.run()
                self.assertEqual(statuses, [None])
                self.assertEqual(engine.stats["workers"], 1)
                self.assertEqual(receipt.call_count, 1)
                self.assertFalse((f.root / app.LIVE_CHECKPOINT_NAME).exists())
            finally:
                f.close()

    def test_ordinary_move_skip_without_retry_completes_normally(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = SecondPassFixture(tmp, api=FinishingAPI())
            try:
                destination = f.root.parent / "Processed"
                (destination / f.worker.name).mkdir(parents=True)
                statuses = []
                engine = f.new_engine(f.api)
                engine.move_mode = True
                engine.move_dest = destination
                engine.on_done = lambda _stats, status: statuses.append(status)
                process = Mock()
                with patch.object(engine, "_process_worker", process), \
                        patch.object(engine, "_record_review_processing"), \
                        patch.object(engine, "_run_post_run_audit",
                                     side_effect=lambda: engine.stats.update(
                                         audit_status="disabled")):
                    engine.run()
                self.assertEqual(statuses, [None])
                self.assertEqual(process.call_count, 0)
                self.assertEqual(engine.stats["skipped_done"], 1)
                self.assertFalse((f.root / app.LIVE_CHECKPOINT_NAME).exists())
            finally:
                f.close()

    def test_move_skip_with_pending_retry_is_caught_at_final_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            f, _signed, _records, assessment = self.prepare_deferred(tmp)
            try:
                destination = f.root.parent / "Processed"
                existing = destination / f.worker.name
                existing.mkdir(parents=True)
                marker = existing / "already-there.txt"
                marker.write_text("unchanged", encoding="utf-8")
                result = self.run_retry(
                    f, assessment["token"], move=True)
                engine, api, statuses, receipt, audit, _destination = result
                self.assert_retry_held(
                    f, engine, api, statuses, receipt, audit)
                self.assertEqual(engine.stats["skipped_done"], 1)
                self.assertTrue(f.worker.is_dir())
                self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged")
            finally:
                f.close()


class TestDesktopContracts(unittest.TestCase):
    """Source-level contracts for the desktop handling (no Tk needed)."""

    def setUp(self):
        self.source = SOURCE.read_text(encoding="utf-8-sig")

    def test_automatic_review_still_launches_only_after_a_completed_batch(self):
        start = self.source.index("    def _begin_automatic_review(self, stats, status):")
        body = self.source[start:start + 1200]
        self.assertIn('if kind not in ("", "batch_applied", "batch_audit_complete"):', body)
        self.assertNotIn("batch_apply_attention", body)

    def test_attention_status_has_its_own_dialog_with_explicit_retry_confirmation(self):
        start = self.source.index('        elif kind == "batch_apply_attention":')
        body = self.source[start:self.source.index('        elif kind == "batch_followup_ambiguous":', start)]
        self.assertIn("messagebox.askyesno", body)
        self.assertIn("never re-sends a failed request by itself", body)
        self.assertIn("self._batch_check_status(", body)
        self.assertIn("retry_token=token", body)
        self.assertIn("never retried automatically", body)

    def test_live_completion_wording_is_truthful_when_a_worker_was_deferred(self):
        self.assertIn('elif int(stats.get("workers_deferred", 0) or 0):', self.source)
        self.assertIn("ranking needs attention", self.source)
        self.assertIn("RANKING NEEDS ATTENTION:", self.source)


if __name__ == "__main__":
    unittest.main()
