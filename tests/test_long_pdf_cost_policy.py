"""v1.3.1 long-PDF cost guard and short-bundle regression coverage."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from _load_app import load_app


app = load_app()


class StubKB:
    TYPES = {
        "Passport": "Crucial",
        "Visa Vignette": "Crucial",
        "BRP": "Crucial",
        "Employee Handbook": "Important",
    }

    def canonical_name(self, name):
        for item in self.TYPES:
            if item.casefold() == str(name or "").casefold():
                return item
        return None

    def group_of(self, name):
        return self.TYPES.get(name, "Other")


def bare_engine():
    engine = app.Engine.__new__(app.Engine)
    engine.kb = StubKB()
    engine.api = Mock()
    engine.escalation_api = None
    engine.resolution = 1.0
    engine.stats = {"bundles_split": 0, "renamed": 0, "unknown": 0,
                    "errors": 0, "possible_bundles": 0}
    engine.possible_bundles = []
    engine.redact_logs = False
    engine.log = Mock()
    engine._stop = threading.Event()
    return engine


@unittest.skipUnless(app.HAS_FITZ, "PyMuPDF is not installed")
class TestLongPdfCostGuard(unittest.TestCase):

    def _long_case(self, result):
        with tempfile.TemporaryDirectory() as tmp:
            worker = Path(tmp) / "Worker"
            worker.mkdir()
            path = worker / "long.pdf"
            path.write_bytes(b"placeholder")
            engine = bare_engine()
            with patch.object(app.DocRender, "page_count", return_value=88), \
                 patch.object(app, "page_ink_fractions",
                              return_value=[0.1] * 88), \
                 patch.object(app, "segmentation_pages",
                              return_value=([0, 1, 87], set(), False)), \
                 patch.object(app, "detect_long_bundle_starts") as scan, \
                 patch.object(engine, "_confirm_long_bundle_plan") as confirm:
                output = engine._maybe_split_bundle(
                    worker, path, result, "vocab", [], interactive=False,
                    default_source="batch-AI", unknown_queue=None, depth=0)
            return output, scan.call_count, confirm.call_count, engine

    def test_88_page_employee_handbook_never_scans_or_confirms_children(self):
        output, scans, confirmations, engine = self._long_case({
            "match": True, "name": "Employee Handbook", "confidence": 98,
        })
        self.assertIsNone(output)
        self.assertEqual(scans, 0)
        self.assertEqual(confirmations, 0)
        self.assertEqual(engine.stats["possible_bundles"], 0)

    def test_other_representative_long_and_unknown_pdfs_never_full_scan(self):
        cases = (
            {"match": True, "name": "Employment Contract", "confidence": 95},
            {"match": False, "other_label": "Unknown", "confidence": 10},
            {},
        )
        for result in cases:
            with self.subTest(result=result):
                _output, scans, confirmations, _engine = self._long_case(result)
                self.assertEqual(scans, 0)
                self.assertEqual(confirmations, 0)

    def test_sampled_bundle_evidence_flags_but_leaves_long_original_intact(self):
        output, scans, confirmations, engine = self._long_case({
            "match": False, "guess": "mixed documents bundle",
            "confidence": 75,
        })
        self.assertIsNone(output)
        self.assertEqual(scans, 0)
        self.assertEqual(confirmations, 0)
        self.assertEqual(engine.stats["possible_bundles"], 1)
        self.assertIn("left intact", engine.possible_bundles[0]["reason"])


@unittest.skipUnless(app.HAS_FITZ, "PyMuPDF is not installed")
class TestShortBundleStillSplits(unittest.TestCase):

    def test_batch_view_requests_the_short_bundle_map_in_one_payload(self):
        engine = bare_engine()
        engine.bundle_split = True
        path = Path("short-passport-visa-brp.pdf")
        with patch.object(app.DocRender, "page_count", return_value=3), \
             patch.object(app, "segmentation_pages",
                          return_value=([0, 1, 2], set(), True)), \
             patch.object(app.DocRender, "render",
                          return_value=(["p1", "p2", "p3"], "text")) as render, \
             patch.object(app, "detect_long_bundle_starts") as long_scan:
            imgs, text, page_idxs, total, segment = \
                engine._batch_classification_view(path)
        self.assertEqual((imgs, text), (["p1", "p2", "p3"], "text"))
        self.assertEqual((page_idxs, total, segment), ([0, 1, 2], 3, True))
        render.assert_called_once_with(
            path, zoom=1.0, pages=[0, 1, 2], max_pages=app.MAX_SEG_PAGES)
        long_scan.assert_not_called()

    def test_passport_visa_brp_uses_one_result_and_archives_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "Worker"
            archive = root / "archive"
            worker.mkdir()
            archive.mkdir()
            original = worker / "stacked-identities.pdf"
            pdf = app.fitz.open()
            for page_number in range(3):
                page = pdf.new_page()
                page.insert_text((72, 72), f"identity page {page_number + 1}")
            pdf.save(str(original))
            pdf.close()

            engine = bare_engine()
            engine.bundle_split = True
            engine.care_home = "Test Home"
            engine.manifest = Mock()
            engine.rename_log = Mock()
            engine._bundle_archive_dir = Mock(return_value=archive)
            result = {
                "match": True, "name": "Passport", "group": "Crucial",
                "confidence": 98, "rotations": [0, 0, 0],
                "documents": [
                    {"pages": [1], "type": "Passport", "confidence": 98},
                    {"pages": [2], "type": "Visa Vignette", "confidence": 97},
                    {"pages": [3], "type": "BRP", "confidence": 99},
                ],
            }
            records = []
            with patch.object(app, "detect_pdf_page_text_rotations",
                              return_value={}), \
                 patch.object(engine.api, "classify") as classify:
                output = engine._maybe_split_bundle(
                    worker, original, result, "vocab", records,
                    interactive=False, default_source="batch-AI",
                    unknown_queue=None, depth=0)

            self.assertEqual(output, "vocab")
            classify.assert_not_called()
            self.assertFalse(original.exists())
            self.assertEqual(len(list(archive.glob("*.pdf"))), 1)
            self.assertEqual(
                sorted(path.name for path in worker.glob("*.pdf")),
                ["BRP.pdf", "Passport.pdf", "Visa Vignette.pdf"])
            self.assertEqual(len(records), 3)


if __name__ == "__main__":
    unittest.main()
