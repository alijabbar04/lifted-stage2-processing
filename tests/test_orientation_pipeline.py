"""Deterministic, offline tests for the bundled page-orientation preflight."""

import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import fitz

from _load_app import load_app


app = load_app()
FIXTURES = Path(__file__).parent / "fixtures" / "orientation"


def prediction(label, confidence=0.99, runner=0.005):
    return {
        "predicted_orientation": label,
        "correction": (360 - label) % 360,
        "confidence": confidence,
        "runner_up_confidence": runner,
        "margin": confidence - runner,
    }


class StubPredictor:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def predict(self, images):
        self.calls += 1
        out, self.answers = self.answers[:len(images)], self.answers[len(images):]
        return out


class FailingPredictor:
    def predict(self, _images):
        raise RuntimeError("offline model unavailable")


def orientation_engine(root, predictor, mode="audit"):
    engine = app.Engine.__new__(app.Engine)
    engine.dir = Path(root)
    engine.orientation_mode = mode
    engine.orientation_confidence = 0.95
    engine.orientation_margin = 0.20
    engine._orientation_predictor = predictor
    engine._orientation_model_checked = True
    engine.orientation_state = app.OrientationState(root)
    engine.stats = {}
    engine.log = Mock()
    engine.redact_logs = False
    engine._stop = threading.Event()
    return engine


def page_rotations(path):
    doc = fitz.open(path)
    try:
        return [page.rotation for page in doc]
    finally:
        doc.close()


class StubKB:
    TYPES = {"Passport": "Crucial", "BRP": "Crucial"}

    def canonical_name(self, name):
        for item in self.TYPES:
            if item.casefold() == str(name or "").casefold():
                return item
        return None

    def group_of(self, name):
        return self.TYPES.get(name, "Other")


class TestOrientationDecisions(unittest.TestCase):

    def test_four_way_labels_have_inverse_clockwise_corrections(self):
        expected = {0: 0, 90: 270, 180: 180, 270: 90}
        for label, correction in expected.items():
            with self.subTest(label=label):
                result = app.orientation_decision(
                    prediction(label), {"blank": False, "sparse": False,
                                        "photograph_only": False},
                    mode="automatic", confidence_threshold=0.95,
                    margin_threshold=0.20)
                self.assertEqual(result["correction"], correction)
                self.assertEqual(result["rotate"], correction)

    def test_confidence_margin_blank_sparse_photo_and_conflict_veto_rotation(self):
        evidence = {"blank": False, "sparse": False,
                    "photograph_only": False}
        cases = (
            (prediction(90, 0.80, 0.10), evidence, None),
            (prediction(90, 0.96, 0.90), evidence, None),
            (prediction(90), {**evidence, "blank": True}, None),
            (prediction(90), {**evidence, "sparse": True}, None),
            (prediction(90), {**evidence, "photograph_only": True}, None),
            (prediction(90), evidence, 90),
        )
        for pred, page_evidence, text_correction in cases:
            with self.subTest(pred=pred, evidence=page_evidence,
                              text=text_correction):
                result = app.orientation_decision(
                    pred, page_evidence, mode="automatic",
                    confidence_threshold=0.95, margin_threshold=0.20,
                    text_correction=text_correction)
                self.assertEqual(result["rotate"], 0)
                self.assertTrue(result["uncertain"])


class TestOrientationEngine(unittest.TestCase):

    def _copy(self, root, name="three_pages.pdf"):
        target = Path(root) / name
        shutil.copy2(FIXTURES / name, target)
        return target

    @staticmethod
    def _plain_evidence():
        return {"blank": False, "sparse": False,
                "photograph_only": False, "ink_fraction": 0.2}

    def test_audit_only_records_every_page_without_changing_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._copy(tmp)
            before = app.file_hash(path)
            predictor = StubPredictor([prediction(90)] * 3)
            engine = orientation_engine(tmp, predictor, "audit")
            with patch.object(app, "thumbnail_evidence",
                              return_value=self._plain_evidence()), \
                 patch.object(app, "detect_pdf_page_text_rotations",
                              return_value={}):
                result = engine._orientation_preflight(path)
            self.assertFalse(result["changed"])
            self.assertEqual(app.file_hash(path), before)
            self.assertEqual(page_rotations(path), [0, 0, 0])
            self.assertEqual(len(result["pages"]), 3)

    def test_automatic_corrects_only_eligible_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._copy(tmp)
            predictor = StubPredictor([
                prediction(90), prediction(180, 0.70, 0.20), prediction(270)])
            engine = orientation_engine(tmp, predictor, "automatic")
            evidences = [self._plain_evidence(), self._plain_evidence(),
                         {**self._plain_evidence(), "photograph_only": True}]
            with patch.object(app, "thumbnail_evidence", side_effect=evidences), \
                 patch.object(app, "detect_pdf_page_text_rotations",
                              return_value={}):
                result = engine._orientation_preflight(path)
            self.assertTrue(result["changed"])
            self.assertEqual(page_rotations(path), [270, 0, 0])
            entry = engine.orientation_state.entry(path)
            self.assertEqual(entry["current_hash"], app.file_hash(path))
            self.assertNotEqual(entry["original_hash"], entry["current_hash"])

    def test_model_failure_leaves_document_unchanged_and_audits_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._copy(tmp)
            before = app.file_hash(path)
            engine = orientation_engine(tmp, FailingPredictor(), "automatic")
            with patch.object(app, "thumbnail_evidence",
                              return_value=self._plain_evidence()):
                result = engine._orientation_preflight(path)
            self.assertIn("warning", result)
            self.assertEqual(app.file_hash(path), before)
            self.assertTrue(engine.orientation_state.audit_rows())

    def test_orientation_makes_zero_external_api_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._copy(tmp, "known_text.pdf")
            engine = orientation_engine(
                tmp, StubPredictor([prediction(0)]), "audit")
            with patch.object(app, "thumbnail_evidence",
                              return_value=self._plain_evidence()), \
                 patch.object(app.ClaudeAPI, "_post") as live_call, \
                 patch.object(app.ClaudeAPI, "submit_batch") as batch_call:
                engine._orientation_preflight(path)
            live_call.assert_not_called()
            batch_call.assert_not_called()

    def test_atomic_replace_failure_preserves_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._copy(tmp, "known_text.pdf")
            before = path.read_bytes()
            replacements = []

            def fail_replace(source, destination):
                replacements.append((Path(source), Path(destination)))
                raise OSError("simulated replace failure")

            with patch.object(app.os, "replace", side_effect=fail_replace):
                changed = app.fix_pdf_page_rotations(path, {0: 90})
            self.assertEqual(changed, 0)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(replacements[0][0].suffix, ".tmp")
            self.assertFalse(any(app.is_orientation_temp_file(p)
                                 for p in Path(tmp).iterdir()))

    def test_interrupted_orientation_temp_is_invisible_and_narrowly_cleaned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "Worker"
            worker.mkdir()
            source = worker / "known_text.pdf"
            shutil.copy2(FIXTURES / "known_text.pdf", source)
            residue = worker / ".known_text.orientation-abcdefgh.pdf"
            shutil.copy2(source, residue)
            hidden = worker / ".legitimate.hidden.pdf"
            shutil.copy2(source, hidden)
            lookalike = worker / ".orphan.orientation-abcdefgh.pdf"
            shutil.copy2(source, lookalike)

            with patch.object(app.ClaudeAPI, "_post") as api_call:
                docs = app.list_worker_docs(worker)
                scan = app.preflight_scan(root, 50)
            api_call.assert_not_called()
            self.assertFalse(residue.exists())
            self.assertTrue(hidden.exists())
            self.assertTrue(lookalike.exists())
            self.assertEqual(set(docs), {source, hidden})
            self.assertEqual(scan["files"], 2)

            empty_worker = root / "Empty Worker"
            empty_worker.mkdir()
            empty_source = empty_worker / "document.pdf"
            shutil.copy2(source, empty_source)
            empty_residue = empty_worker / ".document.orientation-1234abcd.pdf"
            shutil.copy2(source, empty_residue)
            self.assertEqual(
                app.cleanup_orientation_temp_files(empty_worker), 1)
            empty_source.unlink()
            self.assertTrue(app.batch_worker_ready_to_move(empty_worker, []))

    def test_stop_before_orientation_pdf_does_not_open_or_mutate_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._copy(tmp, "known_text.pdf")
            before = path.read_bytes()
            predictor = StubPredictor([prediction(90)])
            engine = orientation_engine(tmp, predictor, "automatic")
            engine.stop()
            with self.assertRaises(app.StopRequested):
                engine._orientation_preflight(path)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(predictor.calls, 0)

    def test_audit_stop_between_pages_propagates_and_preserves_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._copy(tmp)
            before = path.read_bytes()
            engine = orientation_engine(
                tmp, StubPredictor([prediction(90)] * 3), "audit")
            evidence_calls = 0

            def stop_after_first_page(_image):
                nonlocal evidence_calls
                evidence_calls += 1
                engine.stop()
                return self._plain_evidence()

            with patch.object(app, "thumbnail_evidence",
                              side_effect=stop_after_first_page), \
                 patch.object(app, "detect_pdf_page_text_rotations",
                              return_value={}):
                with self.assertRaises(app.StopRequested):
                    engine._orientation_preflight(path)
            self.assertEqual(evidence_calls, 1)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(
                engine.orientation_state.entry(path).get("status"),
                "inspection_started")
            self.assertEqual(engine.orientation_state.data["warnings"], [])

    def test_automatic_stop_before_rewrite_has_no_partial_state_or_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._copy(tmp, "known_text.pdf")
            before = path.read_bytes()
            predictor = Mock()
            engine = orientation_engine(tmp, predictor, "automatic")

            def decide_then_stop(images):
                engine.stop()
                return [prediction(90) for _image in images]

            predictor.predict.side_effect = decide_then_stop
            with patch.object(app, "thumbnail_evidence",
                              return_value=self._plain_evidence()), \
                 patch.object(app, "detect_pdf_page_text_rotations",
                              return_value={}), \
                 patch.object(app, "fix_pdf_page_rotations") as rewrite:
                with self.assertRaises(app.StopRequested):
                    engine._orientation_preflight(path)
            rewrite.assert_not_called()
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(
                engine.orientation_state.entry(path).get("status"),
                "inspection_started")

    def test_restart_is_idempotent_after_successful_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._copy(tmp, "known_text.pdf")
            first = orientation_engine(
                tmp, StubPredictor([prediction(90)]), "automatic")
            with patch.object(app, "thumbnail_evidence",
                              return_value=self._plain_evidence()), \
                 patch.object(app, "detect_pdf_page_text_rotations",
                              return_value={}):
                first._orientation_preflight(path)
            self.assertEqual(page_rotations(path), [270])

            bomb = Mock()
            bomb.predict.side_effect = AssertionError(
                "completed orientation must not be inferred twice")
            restarted = orientation_engine(tmp, bomb, "automatic")
            restarted._orientation_preflight(path)
            bomb.predict.assert_not_called()
            self.assertEqual(page_rotations(path), [270])

    def test_orientation_state_follows_completed_worker_folder_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_worker = root / "source" / "Worker"
            old_worker.mkdir(parents=True)
            old_file = old_worker / "Document.pdf"
            old_file.write_bytes(b"synthetic")
            state = app.OrientationState(root)
            state.put(old_file, {
                "status": "complete", "original_hash": "a",
                "current_hash": "b", "pages": [],
            })
            state.warning(old_file, "synthetic warning")
            new_worker = root / "destination" / "Worker"
            new_worker.parent.mkdir()
            old_worker.rename(new_worker)
            state.move_tree(old_worker, new_worker)
            new_file = new_worker / old_file.name
            self.assertEqual(state.entry(old_file), {})
            self.assertEqual(state.entry(new_file).get("path"), str(new_file))
            self.assertEqual(state.data["warnings"][0]["path"], str(new_file))

    def test_rotate_then_split_rotates_every_page_exactly_once_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "Worker"
            worker.mkdir()
            parent = worker / "Synthetic bundle.pdf"
            shutil.copy2(FIXTURES / "three_pages.pdf", parent)
            engine = orientation_engine(
                root, StubPredictor([prediction(90)] * 3), "automatic")
            engine.kb = StubKB()
            engine.api = Mock(model_id="offline")
            engine.resolution = 1.0
            engine.bundle_split = True
            engine.care_home = "Synthetic Home"
            engine.rename_log = Mock()
            engine.manifest = Mock()
            engine.stats.update({"renamed": 0, "unknown": 0, "errors": 0,
                                 "bundles_split": 0})
            with patch.object(app, "APP_DIR", root / "appdata"), \
                 patch.object(app, "thumbnail_evidence",
                              return_value=self._plain_evidence()), \
                 patch.object(app, "detect_pdf_page_text_rotations",
                              return_value={}), \
                 patch.object(app, "segmentation_pages",
                              return_value=([0, 1, 2], set(), True)):
                changed = engine._orientation_preflight(parent)
                result = {
                    "match": True, "name": "Passport", "group": "Crucial",
                    "confidence": 99, "rotation": 270,
                    "rotations": [270, 270, 270],
                    "documents": [
                        {"pages": [1], "type": "Passport", "confidence": 99},
                        {"pages": [2, 3], "type": "BRP", "confidence": 99},
                    ],
                }
                self.assertTrue(changed["changed"])
                engine._consume_rotation_instructions(result)
                records = []
                handled = engine._maybe_split_bundle(
                    worker, parent, result, "vocab", records,
                    interactive=False, default_source="test",
                    unknown_queue=None, depth=0)
            self.assertEqual(handled, "vocab")
            children = sorted(worker.glob("*.pdf"))
            self.assertEqual(sum(len(page_rotations(p)) for p in children), 3)
            self.assertEqual([rotation for child in children
                              for rotation in page_rotations(child)],
                             [270, 270, 270])

            bomb = Mock()
            bomb.predict.side_effect = AssertionError(
                "split children inherited completed orientation state")
            restarted = orientation_engine(root, bomb, "automatic")
            for child in children:
                restarted._orientation_preflight(child)
            bomb.predict.assert_not_called()
            self.assertEqual([rotation for child in children
                              for rotation in page_rotations(child)],
                             [270, 270, 270])


if __name__ == "__main__":
    unittest.main()
