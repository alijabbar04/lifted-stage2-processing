"""Long bundles require boundary evidence plus independent classifications."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _load_app import load_app


app = load_app()


class StubKB:
    TYPES = {
        "Passport": "Crucial",
        "BRP": "Crucial",
        "Employment Contract": "Important",
    }

    def canonical_name(self, name):
        for item in self.TYPES:
            if item.casefold() == str(name or "").casefold():
                return item
        return None

    def group_of(self, name):
        return self.TYPES.get(name, "Other")


def core(name, confidence=95):
    return {
        "result": {
            "match": True,
            "name": name,
            "group": "Crucial",
            "confidence": confidence,
            "rotation": 0,
        },
        "page_idxs": [0],
    }


@unittest.skipUnless(app.HAS_FITZ, "PyMuPDF is not installed")
class TestLongBundleConfirmation(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "long-pack.pdf"
        doc = app.fitz.open()
        for number in range(4):
            page = doc.new_page()
            page.insert_text((72, 72), f"page {number + 1}")
        doc.save(str(self.path))
        doc.close()

        self.engine = app.Engine.__new__(app.Engine)
        self.engine.api = object()
        self.engine.resolution = 1.0
        self.engine.escalation_api = None
        self.engine.kb = StubKB()
        self.engine._emit_cost = lambda: None

    def tearDown(self):
        self.tmp.cleanup()

    def test_distinct_confident_children_confirm_the_cut(self):
        with patch.object(app, "classify_document_core",
                          side_effect=[core("Passport"), core("BRP")]):
            plan = self.engine._confirm_long_bundle_plan(
                self.path, [3], "vocab", 4)
        self.assertEqual([p["pages"] for p in plan], [[0, 1], [2, 3]])
        self.assertEqual([p["type"] for p in plan], ["Passport", "BRP"])

    def test_same_type_on_both_sides_fails_closed(self):
        with patch.object(app, "classify_document_core",
                          side_effect=[core("Employment Contract"),
                                       core("Employment Contract")]):
            self.assertIsNone(self.engine._confirm_long_bundle_plan(
                self.path, [3], "vocab", 4))

    def test_low_confidence_child_fails_closed(self):
        with patch.object(app, "classify_document_core",
                          side_effect=[core("Passport"), core("BRP", 65)]):
            self.assertIsNone(self.engine._confirm_long_bundle_plan(
                self.path, [3], "vocab", 4))


class TestBundleRanges(unittest.TestCase):

    def test_invalid_and_duplicate_boundaries_are_ignored(self):
        self.assertEqual(
            app.bundle_page_ranges(8, [1, 3, "3", 9, "bad", 6]),
            [[0, 1], [2, 3, 4], [5, 6, 7]])


if __name__ == "__main__":
    unittest.main()
