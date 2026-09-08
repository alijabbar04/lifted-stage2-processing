"""Windows replacement-contention regression for durable JSON state saves.

Background: a Batch preflight aborted with ``[WinError 5] Access is denied`` while
``os.replace`` moved the fsynced temporary over
``.docreview_orientation_state.json``. A reader without FILE_SHARE_DELETE
reproduces that failure; the original handle holder was not established.
The save at the ``inspection_started`` step of
``Engine._orientation_preflight`` is outside its recovery ``try``, so one
transient reader aborted an otherwise healthy preflight.

These tests hold a *real* Windows file handle where the platform allows it and
inject the ``sleep`` used between retries, so the reader is released at a
deterministic point instead of after a timed wait.
"""

import ctypes
import errno
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from _load_app import load_app

app = load_app()
import local_orientation as lo  # noqa: E402  (src is on sys.path after load_app)

from test_orientation_pipeline import (  # noqa: E402
    StubPredictor, orientation_engine, page_rotations, prediction)

WINDOWS = os.name == "nt"
FIXTURES = Path(__file__).parent / "fixtures" / "orientation"
OLD = {"version": 1, "entries": {"old": {"status": "complete"}}, "warnings": []}
NEW = {"version": 1, "entries": {"new": {"status": "complete"}}, "warnings": []}


def dumped(data):
    return json.dumps(data, indent=2).encode("utf-8")


def temp_names(directory):
    return sorted(p.name for p in Path(directory).iterdir()
                  if p.name.endswith(".tmp"))


def transient_error(winerror=5):
    exc = PermissionError(errno.EACCES, "Access is denied")
    exc.winerror = winerror
    return exc


class SleepRecorder:
    """Deterministic stand-in for ``time.sleep`` between replace retries."""

    def __init__(self, on_call=None):
        self.delays = []
        self.on_call = on_call

    def __call__(self, seconds):
        self.delays.append(seconds)
        if self.on_call is not None:
            self.on_call(len(self.delays))


class TestRetrySchedule(unittest.TestCase):

    def test_schedule_is_bounded_positive_and_non_decreasing(self):
        delays = lo.REPLACE_RETRY_DELAYS
        self.assertTrue(1 <= len(delays) <= 20)
        self.assertTrue(all(delay > 0 for delay in delays))
        self.assertEqual(list(delays), sorted(delays))
        self.assertLessEqual(sum(delays), 5.0)

    def test_only_windows_sharing_codes_are_transient(self):
        with patch.object(lo, "_WINDOWS", True):
            for code in (5, 32, 33):
                with self.subTest(winerror=code):
                    self.assertTrue(lo.is_transient_replace_error(
                        transient_error(code)))
            self.assertFalse(lo.is_transient_replace_error(transient_error(2)))
            self.assertFalse(lo.is_transient_replace_error(
                OSError(errno.ENOENT, "missing")))
            self.assertFalse(lo.is_transient_replace_error(
                TypeError("not serialisable")))
        with patch.object(lo, "_WINDOWS", False):
            self.assertFalse(lo.is_transient_replace_error(transient_error(5)))


class TestAtomicWriteJsonContention(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.root = Path(self._tmp)
        self.target = self.root / ".docreview_orientation_state.json"
        self.target.write_bytes(dumped(OLD))
        # Temporaries the writer does not own must survive any cleanup.
        self.decoys = [self.root / ".unrelated.tmp",
                       self.root / f".{self.target.name}.decoy.tmp"]
        for decoy in self.decoys:
            decoy.write_bytes(b"not ours")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def assert_only_decoy_temps(self):
        self.assertEqual(temp_names(self.root),
                         sorted(d.name for d in self.decoys))
        for decoy in self.decoys:
            self.assertEqual(decoy.read_bytes(), b"not ours")

    def test_clean_save_uses_a_single_replace_and_no_sleep(self):
        recorder = SleepRecorder()
        retries = lo.atomic_write_json(self.target, NEW, sleep=recorder)
        self.assertEqual(retries, 0)
        self.assertEqual(recorder.delays, [])
        self.assertEqual(self.target.read_bytes(), dumped(NEW))
        self.assert_only_decoy_temps()

    @unittest.skipUnless(WINDOWS, "real Windows file-handle sharing semantics")
    def test_real_reader_without_delete_sharing_is_retried_until_released(self):
        reader = open(self.target, "rb")  # CRT share mode: READ|WRITE, no DELETE
        seen_while_held = []

        def release(call_count):
            seen_while_held.append(self.target.read_bytes())
            reader.close()

        recorder = SleepRecorder(on_call=release)
        try:
            retries = lo.atomic_write_json(self.target, NEW, sleep=recorder)
        finally:
            reader.close()
        self.assertEqual(retries, 1)
        self.assertEqual(recorder.delays, [lo.REPLACE_RETRY_DELAYS[0]])
        # The old state stayed intact for as long as the reader held it.
        self.assertEqual(seen_while_held, [dumped(OLD)])
        self.assertEqual(self.target.read_bytes(), dumped(NEW))
        self.assert_only_decoy_temps()

    @unittest.skipUnless(WINDOWS, "real Windows file-handle sharing semantics")
    def test_exhausted_retries_preserve_prior_bytes_and_clean_only_owned_temp(self):
        recorder = SleepRecorder()
        with open(self.target, "rb"):
            with self.assertRaises(PermissionError) as raised:
                lo.atomic_write_json(self.target, NEW, sleep=recorder)
            self.assertEqual(raised.exception.winerror, 5)
            self.assertEqual(recorder.delays, list(lo.REPLACE_RETRY_DELAYS))
            self.assertEqual(self.target.read_bytes(), dumped(OLD))
            self.assert_only_decoy_temps()
        # Once the holder is gone the next save succeeds without retries.
        again = SleepRecorder()
        self.assertEqual(lo.atomic_write_json(self.target, NEW, sleep=again), 0)
        self.assertEqual(again.delays, [])
        self.assertEqual(self.target.read_bytes(), dumped(NEW))
        self.assert_only_decoy_temps()

    def test_simulated_transient_conflict_retries_then_succeeds(self):
        real_replace = os.replace
        calls = []

        def flaky_replace(source, destination):
            calls.append(Path(source).name)
            if len(calls) <= 2:
                raise transient_error(32)
            return real_replace(source, destination)

        recorder = SleepRecorder()
        with patch.object(lo, "_WINDOWS", True), \
             patch.object(lo.os, "replace", side_effect=flaky_replace):
            retries = lo.atomic_write_json(self.target, NEW, sleep=recorder)
        self.assertEqual(retries, 2)
        self.assertEqual(recorder.delays, list(lo.REPLACE_RETRY_DELAYS[:2]))
        self.assertEqual(len(set(calls)), 1, "same fsynced temp is reused")
        self.assertEqual(self.target.read_bytes(), dumped(NEW))
        self.assert_only_decoy_temps()

    def test_nontransient_errors_propagate_immediately(self):
        cases = (
            ("missing directory", FileNotFoundError(errno.ENOENT, "gone"),
             FileNotFoundError),
            ("other windows code", transient_error(2), PermissionError),
            ("bare OSError", OSError("simulated replace failure"), OSError),
        )
        for label, error, expected in cases:
            with self.subTest(label=label):
                recorder = SleepRecorder()
                with patch.object(lo, "_WINDOWS", True), \
                     patch.object(lo.os, "replace", side_effect=error):
                    with self.assertRaises(expected):
                        lo.atomic_write_json(self.target, NEW, sleep=recorder)
                self.assertEqual(recorder.delays, [])
                self.assertEqual(self.target.read_bytes(), dumped(OLD))
                self.assert_only_decoy_temps()

    def test_serialisation_failure_never_reaches_replace(self):
        recorder = SleepRecorder()
        with patch.object(lo.os, "replace") as replace:
            with self.assertRaises(TypeError):
                lo.atomic_write_json(self.target, {"bad": {1, 2}},
                                     sleep=recorder)
        replace.assert_not_called()
        self.assertEqual(recorder.delays, [])
        self.assertEqual(self.target.read_bytes(), dumped(OLD))
        self.assert_only_decoy_temps()

    def test_default_sleep_is_module_level_and_patchable(self):
        recorder = SleepRecorder()
        with patch.object(lo, "_WINDOWS", True), \
             patch.object(lo, "_sleep", recorder), \
             patch.object(lo.os, "replace",
                          side_effect=[transient_error(5), None]):
            retries = lo.atomic_write_json(self.target, NEW)
        self.assertEqual(retries, 1)
        self.assertEqual(recorder.delays, [lo.REPLACE_RETRY_DELAYS[0]])


@unittest.skipUnless(WINDOWS, "real Windows file-handle sharing semantics")
class TestOrientationStateUnderContention(unittest.TestCase):

    @staticmethod
    def _hidden(path):
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return attrs != -1 and bool(attrs & 2)

    def test_state_put_survives_transient_reader_and_reloads_new_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            doc = root / "Worker" / "Document.pdf"
            doc.parent.mkdir()
            doc.write_bytes(b"synthetic")
            state = app.OrientationState(root)
            state.put(doc, {"status": "inspection_started",
                            "original_hash": "a", "current_hash": "a"})
            self.assertTrue(self._hidden(state.path))
            reader = open(state.path, "rb")
            recorder = SleepRecorder(on_call=lambda _n: reader.close())
            try:
                with patch.object(lo, "_sleep", recorder):
                    state.put(doc, {"status": "complete", "original_hash": "a",
                                    "current_hash": "a", "pages": []})
            finally:
                reader.close()
            self.assertEqual(len(recorder.delays), 1)
            reloaded = app.OrientationState(root)
            self.assertEqual(reloaded.entry(doc).get("status"), "complete")
            self.assertTrue(self._hidden(state.path),
                            "hidden attribute is still re-applied after save")

    def test_preflight_survives_reader_on_state_and_restart_uses_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "known_text.pdf"
            shutil.copy2(FIXTURES / "known_text.pdf", path)
            # An existing state file, as in a resumed care-home folder.
            seed = app.OrientationState(root)
            seed.put(root / "Other.pdf", {"status": "complete",
                                          "original_hash": "z",
                                          "current_hash": "z"})
            engine = orientation_engine(
                root, StubPredictor([prediction(90)]), "automatic")
            reader = open(engine.orientation_state.path, "rb")
            recorder = SleepRecorder(on_call=lambda _n: reader.close())
            plain = {"blank": False, "sparse": False,
                     "photograph_only": False, "ink_fraction": 0.2}
            try:
                with patch.object(lo, "_sleep", recorder), \
                     patch.object(app, "thumbnail_evidence",
                                  return_value=plain), \
                     patch.object(app, "detect_pdf_page_text_rotations",
                                  return_value={}):
                    result = engine._orientation_preflight(path)
            finally:
                reader.close()
            self.assertNotIn("warning", result)
            self.assertTrue(result["changed"])
            self.assertEqual(page_rotations(path), [270])
            self.assertEqual(len(recorder.delays), 1)
            entry = engine.orientation_state.entry(path)
            self.assertEqual(entry["status"], "complete")
            self.assertEqual(entry["current_hash"], app.file_hash(path))
            self.assertEqual(engine.orientation_state.data["warnings"], [])

            bomb = Mock()
            bomb.predict.side_effect = AssertionError(
                "completed orientation must not be inferred twice")
            restarted = orientation_engine(root, bomb, "automatic")
            cached = restarted._orientation_preflight(path)
            self.assertTrue(cached.get("cached"))
            bomb.predict.assert_not_called()
            self.assertEqual(page_rotations(path), [270])


if __name__ == "__main__":
    unittest.main()
