"""Regression tests for Overnight Batch worker-folder movement."""

import tempfile
import unittest
from pathlib import Path

from _load_app import load_app


app = load_app()


class TestBatchWorkerMovePolicy(unittest.TestCase):

    def test_processed_worker_is_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = Path(tmp) / "Worker"
            worker.mkdir()
            (worker / "left-behind.pdf").write_bytes(b"pdf")
            self.assertTrue(app.batch_worker_ready_to_move(
                worker, [{"path": worker / "processed.pdf"}]))

    def test_empty_worker_is_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = Path(tmp) / "Worker"
            worker.mkdir()
            self.assertTrue(app.batch_worker_ready_to_move(worker, []))

    def test_junk_only_worker_is_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = Path(tmp) / "Worker"
            worker.mkdir()
            (worker / "desktop.ini").write_text("", encoding="utf-8")
            self.assertTrue(app.batch_worker_ready_to_move(worker, []))

    def test_unprocessed_file_keeps_worker_in_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = Path(tmp) / "Worker"
            worker.mkdir()
            (worker / "failed.pdf").write_bytes(b"pdf")
            self.assertFalse(app.batch_worker_ready_to_move(worker, []))

    def test_unreadable_or_unsupported_file_keeps_worker_in_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = Path(tmp) / "Worker"
            worker.mkdir()
            (worker / "archive.zip").write_bytes(b"not processed")
            self.assertFalse(app.batch_worker_ready_to_move(worker, []))


if __name__ == "__main__":
    unittest.main()
