"""Second-pass evidence must stay bound to the file bytes it came from.

Reproduces two ranking defects from the 2026-09-07 five-worker trial with
synthetic, non-personal fixtures:

* After an exact-duplicate deletion, records were re-paired with files by
  base label in directory-listing order. Two same-type files whose processing
  order differed from their listing order swapped cached page evidence, so
  each right-to-work check was dated with the OTHER file's check date and a
  signed contract was ranked as the unsigned copy.
* The employee-signed check saw the ordinary review sample (pages 1, 2 and the
  last page). A contract signed on page 7 of 8 followed by a certificate or a
  blank page was judged without its signature block, so the crisp unsigned
  twin could take the higher rank.

And the independent-QA finding of 2026-09-08: an UNANSWERED signed check
(a fresh failure, or a failed finishing operation replayed as None after a
restart) must not be read as 'unsigned'. The contract family keeps its
current names and ranks and is recorded as unresolved instead.

The ranking policy itself (date, signed bonus, quality, legibility,
completeness; worst first, best last) is unchanged and asserted as a control.
"""
import csv
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load_app import load_app
from _pdf_fixtures import pdf_bytes

import fitz

app = load_app()

FILLER = "lorem ipsum terms and conditions clause " * 45   # ~1.8k chars/page
MARK = "EMPLOYEE-SIGNATURE-PRESENT"


def page_text(path, index):
    doc = fitz.open(path)
    try:
        return doc[index].get_text()
    finally:
        doc.close()


def all_text(path):
    doc = fitz.open(path)
    try:
        return "\n".join(doc[i].get_text() for i in range(doc.page_count))
    finally:
        doc.close()


class StubKB:
    def vocabulary_block(self):
        return ""

    def canonical_name(self, name):
        return name

    def group_of(self, name):
        return "Crucial"


class EvidenceAPI:
    """Offline stand-in that answers from the evidence it is actually shown,
    mirroring the real client's windows: at most MAX_PAGES images and the
    first 5000 characters of text."""
    model_id = "offline"
    api_key = "offline"
    in_tokens = 0
    out_tokens = 0

    def __init__(self, fail_signed=False):
        self.signed_calls = []
        self.fail_signed = fail_signed

    def share_code_check(self, imgs, text):
        m = re.search(r"CHECKDATE (\d{4}-\d{2}-\d{2})", text[:5000])
        return {"check_date": m.group(1) if m else "", "work_permitted": True}

    def cos_issue_date(self, imgs, text):
        return ""

    def contract_signed(self, imgs, text):
        self.signed_calls.append((len(imgs), text[:5000]))
        if self.fail_signed:
            raise RuntimeError("synthetic signature-check outage")
        return MARK in text[:5000]

    def doc_quality(self, imgs, text, doc_type):
        m = re.search(r"QUALITY (\d+)", text[:5000])
        return {"score": int(m.group(1)) if m else 50, "legible": True,
                "complete": True, "date": "", "note": ""}


class SecondPassFixture:
    def __init__(self, base, api=None):
        self.base = Path(base)
        self.appdata = self.base / "appdata"
        self.appdata.mkdir()
        self.root = self.base / "Home"
        self.worker = self.root / "Worker"
        self.worker.mkdir(parents=True)
        self.logs = []
        self.api = api or EvidenceAPI()
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
        self.engine = self.new_engine(self.api)

    def new_engine(self, api):
        return app.Engine(
            self.root, StubKB(), api, "Synthetic Home",
            log=self.logs.append, set_status=lambda _m: None,
            set_progress=lambda *_a: None, set_preview=lambda *_a: None,
            ask_unknown=lambda *_a: None, on_cost=lambda *_a: None,
            on_done=lambda *_a: None, resolution=1.0, convert_pdf=False,
            bundle_split=False, orientation_mode="off", post_run_audit=False)

    def close(self):
        for p in self._patches:
            p.stop()

    def file(self, name, data):
        path = self.worker / name
        path.write_bytes(data)
        return path

    def record(self, path, name, pages="first"):
        """A first-pass record exactly as the live loop stores it: the page
        images/text the classifier used, cached for the second pass."""
        imgs, text = app.DocRender.render(path, zoom=1.0, pages=pages)
        return {"path": path, "name": name, "group": "Crucial",
                "original": path.name, "imgs": imgs, "text": text}

    def failed_rows(self):
        with open(self.appdata / "failed.csv", newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def batch_state(self, operations):
        """A durable batch state for this worker with the given stored
        finishing operations, as a restarted run would reload it."""
        state = app.BatchState(self.root)
        state.init("Synthetic Home", "offline", 1.0, {})
        key = str(self.worker.resolve()).casefold()
        state.data["workers"][key] = {
            "name": self.worker.name, "source_path": str(self.worker),
            "finishing_operations": operations}
        assert state.save()
        return state

    def contract_pair(self):
        """The trial's shape: the crisper copy is UNSIGNED and holds the bare
        name; the signed copy is poorer and carries a collision suffix."""
        unsigned = self.file("Employment Contract.pdf", contract(signed=False, quality=90))
        signed = self.file("Employment Contract (2).pdf", contract(signed=True, quality=70))
        records = [self.record(unsigned, "Employment Contract", pages="all"),
                   self.record(signed, "Employment Contract", pages="all")]
        return unsigned, signed, records

    def contract_names(self):
        return {p.name: all_text(p) for p in self.worker.glob("Employment Contract*.pdf")}


def share_code(date, quality):
    return pdf_bytes(page_texts=[f"Home Office right to work result CHECKDATE {date} QUALITY {quality}"])


def contract(signed, quality, pages=8):
    """An eight-page contract: filler on pages 1-6, the signature block on
    page 7, and a certificate (signed) or blank sheet (unsigned) on page 8."""
    body = [f"Principal statement of terms QUALITY {quality} " + FILLER]
    body += [FILLER] * (pages - 3)
    block = ("Signed by Employee: " + (MARK if signed else "________")
             + " Signed on behalf of the company: HR")
    body.append(block)
    body.append("Electronic signature certificate" if signed else " ")
    return pdf_bytes(page_texts=body)


def signed_op(path):
    return f"contract-signed:{app.file_hash(path)}"


class TestRecordsStayBoundToTheirBytes(unittest.TestCase):

    def test_rebuild_after_dedup_keeps_each_record_on_its_own_file(self):
        with tempfile.TemporaryDirectory() as temp:
            f = SecondPassFixture(temp)
            try:
                # processing order: bare name first, then '(2)' - but a
                # directory listing sorts '(2)' BEFORE '.pdf'
                first = f.file("Share Code Check Result.pdf", share_code("2023-07-31", 60))
                second = f.file("Share Code Check Result (2).pdf", share_code("2025-11-28", 60))
                dup_a = f.file("Probation Review.pdf", pdf_bytes("identical probation form"))
                dup_b = f.file("Probation Review (2).pdf", pdf_bytes("identical probation form"))
                records = [f.record(first, "Share Code Check Result"),
                           f.record(second, "Share Code Check Result"),
                           f.record(dup_a, "Probation Review"),
                           f.record(dup_b, "Probation Review")]
                expected = {id(r): r["text"] for r in records}
                self.assertEqual(app.dedup_worker(f.worker), 1)
                rebuilt = f.engine._rebuild_records(f.worker, records)
                self.assertEqual(len(rebuilt), 3)
                self.assertFalse(any(r["path"] == dup_b for r in rebuilt))
                for r in rebuilt:
                    self.assertTrue(r["path"].exists())
                    # the cached evidence must describe the bytes at r["path"]
                    self.assertEqual(r["text"].strip(), page_text(r["path"], 0).strip())
                    self.assertEqual(r["text"], expected[id(r)])
            finally:
                f.close()

    def test_rebuild_without_any_deletion_is_the_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            f = SecondPassFixture(temp)
            try:
                a = f.file("DBS Document.pdf", pdf_bytes("dbs one"))
                b = f.file("DBS Document (2).pdf", pdf_bytes("dbs two"))
                records = [f.record(b, "DBS Document"), f.record(a, "DBS Document")]
                self.assertEqual(app.dedup_worker(f.worker), 0)
                self.assertEqual([r["path"] for r in f.engine._rebuild_records(f.worker, records)],
                                 [b, a])
            finally:
                f.close()

    def test_dated_names_follow_the_check_date_printed_on_each_file(self):
        with tempfile.TemporaryDirectory() as temp:
            f = SecondPassFixture(temp)
            try:
                first = f.file("Share Code Check Result.pdf", share_code("2023-07-31", 60))
                second = f.file("Share Code Check Result (2).pdf", share_code("2025-11-28", 60))
                dup_a = f.file("Probation Review.pdf", pdf_bytes("identical probation form"))
                dup_b = f.file("Probation Review (2).pdf", pdf_bytes("identical probation form"))
                records = [f.record(first, "Share Code Check Result"),
                           f.record(second, "Share Code Check Result"),
                           f.record(dup_a, "Probation Review"),
                           f.record(dup_b, "Probation Review")]
                self.assertEqual(app.dedup_worker(f.worker), 1)
                records = f.engine._rebuild_records(f.worker, records)
                f.engine._second_pass(f.worker, records)
                names = {p.name: all_text(p) for p in f.worker.glob("Share Code Check Result*.pdf")}
                self.assertEqual(sorted(names), ["Share Code Check Result - (28-11-2025) (01).pdf",
                                                 "Share Code Check Result - (31-07-2023).pdf"])
                self.assertIn("CHECKDATE 2025-11-28", names["Share Code Check Result - (28-11-2025) (01).pdf"])
                self.assertIn("CHECKDATE 2023-07-31", names["Share Code Check Result - (31-07-2023).pdf"])
            finally:
                f.close()


class TestSignedCheckSeesTheSignatureBlock(unittest.TestCase):

    def test_signed_contract_on_page_seven_outranks_crisper_unsigned_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            f = SecondPassFixture(temp)
            try:
                _unsigned, _signed, records = f.contract_pair()
                f.engine._second_pass(f.worker, records)
                names = f.contract_names()
                self.assertEqual(sorted(names), ["Employment Contract (01).pdf", "Employment Contract.pdf"])
                self.assertIn(MARK, names["Employment Contract (01).pdf"])
                self.assertNotIn(MARK, names["Employment Contract.pdf"])
                for image_count, window in f.api.signed_calls:
                    self.assertLessEqual(image_count, app.DocRender.MAX_PAGES)
                    self.assertIn("Signed by Employee", window)
                self.assertEqual(f.engine.stats["errors"], 0)
                self.assertEqual(f.engine.stats.get("rank_deferred", 0), 0)
            finally:
                f.close()

    def test_unsigned_pair_still_ranks_by_quality(self):
        with tempfile.TemporaryDirectory() as temp:
            f = SecondPassFixture(temp)
            try:
                poor = f.file("Employment Contract.pdf", contract(signed=False, quality=40))
                crisp = f.file("Employment Contract (2).pdf", contract(signed=False, quality=95))
                records = [f.record(poor, "Employment Contract", pages="all"),
                           f.record(crisp, "Employment Contract", pages="all")]
                f.engine._second_pass(f.worker, records)
                names = f.contract_names()
                self.assertIn("QUALITY 95", names["Employment Contract (01).pdf"])
                self.assertIn("QUALITY 40", names["Employment Contract.pdf"])
                self.assertEqual(f.engine.stats["errors"], 0)
            finally:
                f.close()

    def test_short_contract_reuses_cached_review_pages(self):
        with tempfile.TemporaryDirectory() as temp:
            f = SecondPassFixture(temp)
            try:
                short = f.file("Employment Contract.pdf", pdf_bytes(page_texts=[
                    "terms QUALITY 50", "more terms", f"Signed by Employee: {MARK}"]))
                other = f.file("Employment Contract (2).pdf", pdf_bytes(page_texts=[
                    "terms QUALITY 80", "more terms", "Signed by Employee: ______"]))
                records = [f.record(short, "Employment Contract", pages="all"),
                           f.record(other, "Employment Contract", pages="all")]
                with patch.object(app.DocRender, "render", side_effect=AssertionError("re-rendered")):
                    f.engine._second_pass(f.worker, records)
                names = f.contract_names()
                self.assertIn(MARK, names["Employment Contract (01).pdf"])
            finally:
                f.close()


class TestUnavailableSignatureEvidenceIsNotUnsigned(unittest.TestCase):
    """Independent QA, 2026-09-08: 'unknown' must never rank as 'unsigned'.

    In every case the byte-to-name binding is asserted directly: the crisp
    unsigned copy must still hold the bare name and the signed copy its
    collision suffix, and no '(01)' rank may be minted from missing evidence.
    """

    def assert_family_untouched(self, f, records, unsigned, signed):
        names = f.contract_names()
        self.assertEqual(sorted(names), ["Employment Contract (2).pdf", "Employment Contract.pdf"])
        self.assertIn("QUALITY 90", names["Employment Contract.pdf"])
        self.assertNotIn(MARK, names["Employment Contract.pdf"])
        self.assertIn(MARK, names["Employment Contract (2).pdf"])
        self.assertEqual(unsigned.read_bytes(), (f.worker / "Employment Contract.pdf").read_bytes())
        self.assertEqual(signed.read_bytes(), (f.worker / "Employment Contract (2).pdf").read_bytes())
        # records still point at their own, unmoved files (no parking residue)
        self.assertEqual([r["path"] for r in records], [unsigned, signed])
        self.assertEqual([p.name for p in f.worker.glob("__rank_tmp_*")], [])
        self.assertEqual(f.engine.stats.get("rank_deferred", 0), 1)
        self.assertTrue(any("ranking deferred, names unchanged" in line for line in f.logs))
        rows = f.failed_rows()
        self.assertEqual([row["reason"] for row in rows], ["ranking deferred"] * 2)
        self.assertEqual({row["filename"] for row in rows},
                         {"Employment Contract.pdf", "Employment Contract (2).pdf"})

    def test_fresh_signature_check_failure_leaves_names_and_ranks_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            f = SecondPassFixture(temp, api=EvidenceAPI(fail_signed=True))
            try:
                unsigned, signed, records = f.contract_pair()
                f.engine._second_pass(f.worker, records)
                self.assert_family_untouched(f, records, unsigned, signed)
                self.assertEqual(f.engine.stats["errors"], 2)
                self.assertEqual(len(f.api.signed_calls), 2)
                self.assertTrue(any("signed check unavailable (RuntimeError" in line
                                    or "signed check unavailable (synthetic" in line
                                    for line in f.logs))
                self.assertTrue(all("signed check unavailable" in row["detail"] for row in f.failed_rows()))
            finally:
                f.close()

    def test_stored_failed_signature_check_on_restart_is_unavailable_not_unsigned(self):
        with tempfile.TemporaryDirectory() as temp:
            f = SecondPassFixture(temp)
            try:
                unsigned, signed, records = f.contract_pair()
                # a previous session's signed check for the SIGNED copy failed
                # and was persisted; the restarted run must not treat its
                # replayed None as 'unsigned' and must not pay for it again
                stored_error = "APIError: The read operation timed out"
                state = f.batch_state({
                    signed_op(signed): {"status": "failed", "result": None,
                                        "error": stored_error,
                                        "attempt_id": "earlier"}})
                f.engine._batch_state = state
                f.engine._committed_batch_cost_gbp = 0.0
                f.engine._committed_batch_tokens = 0
                f.engine._persisted_live_cost_gbp = 0.0
                f.engine._persisted_live_tokens = 0
                f.engine._second_pass(f.worker, records)
                self.assert_family_untouched(f, records, unsigned, signed)
                self.assertEqual(f.engine.stats["errors"], 1)
                # only the unsigned copy's check was asked afresh
                self.assertEqual(len(f.api.signed_calls), 1)
                self.assertNotIn(MARK, f.api.signed_calls[0][1])
                self.assertTrue(any(stored_error in line and "signed check unavailable" in line
                                    for line in f.logs))
                details = {row["filename"]: row["detail"] for row in f.failed_rows()}
                self.assertIn("signed check unavailable", details["Employment Contract (2).pdf"])
                self.assertIn("peer of a contract", details["Employment Contract.pdf"])
                # the durable state keeps the failure as it was: still visible,
                # not silently rewritten or retried
                saved = json.loads(state.path.read_text(encoding="utf-8"))
                ops = next(iter(saved["workers"].values()))["finishing_operations"]
                self.assertEqual(ops[signed_op(signed)]["status"], "failed")
                self.assertEqual(ops[signed_op(signed)]["error"], stored_error)
                self.assertEqual(ops[signed_op(unsigned)]["status"], "complete")
                self.assertIs(ops[signed_op(unsigned)]["result"], False)
            finally:
                f.close()

    def test_stored_complete_answers_are_replayed_and_rank_without_a_new_call(self):
        with tempfile.TemporaryDirectory() as temp:
            f = SecondPassFixture(temp)
            try:
                unsigned, signed, records = f.contract_pair()
                state = f.batch_state({
                    signed_op(signed): {"status": "complete", "result": True},
                    signed_op(unsigned): {"status": "complete", "result": False}})
                f.engine._batch_state = state
                f.engine._committed_batch_cost_gbp = 0.0
                f.engine._committed_batch_tokens = 0
                f.engine._persisted_live_cost_gbp = 0.0
                f.engine._persisted_live_tokens = 0
                f.engine._second_pass(f.worker, records)
                names = f.contract_names()
                self.assertEqual(sorted(names), ["Employment Contract (01).pdf", "Employment Contract.pdf"])
                self.assertIn(MARK, names["Employment Contract (01).pdf"])
                self.assertEqual(f.api.signed_calls, [])
                self.assertEqual(f.engine.stats["errors"], 0)
                self.assertEqual(f.engine.stats.get("rank_deferred", 0), 0)
            finally:
                f.close()

    def test_deferred_family_is_ranked_by_a_later_pass_that_can_answer(self):
        with tempfile.TemporaryDirectory() as temp:
            f = SecondPassFixture(temp, api=EvidenceAPI(fail_signed=True))
            try:
                unsigned, signed, records = f.contract_pair()
                f.engine._second_pass(f.worker, records)
                self.assert_family_untouched(f, records, unsigned, signed)
                # the outage clears; the same records, still bound to their
                # unmoved files, rank correctly on the next pass
                later = f.new_engine(EvidenceAPI())
                later._second_pass(f.worker, records)
                names = f.contract_names()
                self.assertEqual(sorted(names), ["Employment Contract (01).pdf", "Employment Contract.pdf"])
                self.assertIn(MARK, names["Employment Contract (01).pdf"])
                self.assertEqual(later.stats["errors"], 0)
            finally:
                f.close()


if __name__ == "__main__":
    unittest.main()
