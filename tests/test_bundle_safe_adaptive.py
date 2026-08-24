"""Adaptive page-one triage must not conceal a short document bundle."""

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from _load_app import load_app


app = load_app()


class StubAPI:
    def __init__(self):
        self.triage = Mock(return_value={
            "enough_from_page1": True,
            "match": True,
            "name": "Passport",
            "group": "Crucial",
            "confidence": 95,
            "rotation": 0,
        })
        self.classify = Mock(return_value={
            "match": True,
            "name": "Passport",
            "group": "Crucial",
            "confidence": 95,
            "rotation": 0,
            "documents": [
                {"pages": [1], "type": "Passport", "confidence": 95},
                {"pages": [2, 3], "type": "BRP", "confidence": 95},
            ],
        })


class TestBundleSafeAdaptivePath(unittest.TestCase):

    def _classify(self, *, bundle_split):
        api = StubAPI()
        with patch.object(app, "segmentation_pages",
                          return_value=([0, 1, 2], set(), True)), \
             patch.object(app, "detect_pdf_page_text_rotations",
                          return_value={}), \
             patch.object(app.DocRender, "render",
                          return_value=(["p1", "p2", "p3"], "text")):
            out = app.classify_document_core(
                api, "vocab", Path("stacked-ids.pdf"), resolution=1.0,
                adaptive_pages=True, p1_imgs=["p1"], p1_text="page one",
                total_pages=3, bundle_split=bundle_split)
        return api, out

    def test_full_short_pdf_bypasses_page_one_triage(self):
        api, out = self._classify(bundle_split=True)
        api.triage.assert_not_called()
        api.classify.assert_called_once()
        self.assertTrue(api.classify.call_args.kwargs["segment"])
        self.assertEqual(out["page_idxs"], [0, 1, 2])
        self.assertTrue(out["segment_view"])
        self.assertFalse(out["page1_only"])

    def test_disabling_bundle_split_preserves_page_one_optimisation(self):
        api, out = self._classify(bundle_split=False)
        api.triage.assert_called_once()
        api.classify.assert_not_called()
        self.assertTrue(out["page1_only"])
        self.assertEqual(out["result"]["name"], "Passport")


if __name__ == "__main__":
    unittest.main()
