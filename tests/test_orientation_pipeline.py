"""Per-page orientation signals combine without skipping mixed scans."""

import unittest
from pathlib import Path
from unittest.mock import patch

from _load_app import load_app


app = load_app()


def engine(local=False):
    value = app.Engine.__new__(app.Engine)
    value.auto_rotate = True
    value.local_ai_orientation = local
    value.local_ai_model = "gemma3:12b"
    value.stats = {}
    value.redact_logs = False
    value.log = lambda *_args, **_kwargs: None
    return value


class TestOrientationConsistency(unittest.TestCase):

    def test_known_probe_turn_has_expected_inverse(self):
        agree = app.OllamaOrientationDetector.decisions_agree
        self.assertTrue(agree(0, 270))
        self.assertTrue(agree(90, 0))
        self.assertTrue(agree(180, 90))
        self.assertTrue(agree(270, 180))
        self.assertFalse(agree(90, 180))


class TestCombinedPageFixes(unittest.TestCase):

    def test_text_fix_no_longer_suppresses_a_scanned_page_fix(self):
        captured = {}

        def apply(_path, fixes):
            captured.update(fixes)
            return len(fixes)

        with patch.object(app.DocRender, "page_count", return_value=3), \
             patch.object(app, "detect_pdf_page_text_rotations",
                          return_value={0: 90, 1: 0}), \
             patch.object(app, "fix_pdf_page_rotations", side_effect=apply), \
             patch.object(app, "file_hash", return_value="new-hash"):
            changed = engine()._maybe_fix_rotation(
                Path("mixed.pdf"),
                {"confidence": 95, "rotations": [0, 0, 270]},
                page_idxs=[0, 1, 2])

        self.assertEqual(changed, "new-hash")
        self.assertEqual(captured, {0: 90, 2: 270})

    def test_double_confirmed_local_decision_precedes_cloud_sample(self):
        captured = {}

        def apply(_path, fixes):
            captured.update(fixes)
            return len(fixes)

        with patch.object(app.DocRender, "page_count", return_value=3), \
             patch.object(app, "detect_pdf_page_text_rotations",
                          return_value={0: 0, 1: 0}), \
             patch.object(app.OllamaOrientationDetector, "detect_pdf_pages",
                          return_value={2: 180}), \
             patch.object(app, "fix_pdf_page_rotations", side_effect=apply), \
             patch.object(app, "file_hash", return_value="new-hash"):
            engine(local=True)._maybe_fix_rotation(
                Path("mixed.pdf"),
                {"confidence": 95, "rotations": [0, 0, 270]},
                page_idxs=[0, 1, 2])

        self.assertEqual(captured, {2: 180})


if __name__ == "__main__":
    unittest.main()
