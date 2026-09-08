"""Cross-process contracts for the shared engine/review-helper path lock."""
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ai_review
import stage2_locking as locking
from _load_app import load_app


HOLDER = r"""
import msvcrt, pathlib, sys, time
path = pathlib.Path(sys.argv[1])
stream = path.open('a+b')
stream.seek(0, 2)
if not stream.tell():
    stream.write(b'\0'); stream.flush()
stream.seek(0)
msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
print('LOCKED', flush=True)
time.sleep(30)
"""

OPEN_ONLY = r"""
import pathlib, sys, time
stream = pathlib.Path(sys.argv[1]).open('a+b')
print('OPEN', flush=True)
time.sleep(30)
"""

CONTENDER = r"""
import msvcrt, pathlib, sys
path = pathlib.Path(sys.argv[1])
stream = path.open('a+b')
stream.seek(0, 2)
if not stream.tell():
    stream.write(b'\0'); stream.flush()
stream.seek(0)
try:
    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
except OSError:
    sys.exit(3)
sys.exit(0)
"""


def test_engine_and_review_helper_contend_on_exact_same_path(tmp_path):
    app = load_app()
    expected = tmp_path / locking.DOCUMENT_WRITER_LOCK
    engine = app.CareHomeWriterLock(tmp_path)
    assert engine.path == expected
    with engine:
        assert expected.exists()
        with pytest.raises(ai_review.ReviewError, match="writer lock"):
            with ai_review.writer_lock(tmp_path):
                pass
    assert expected.exists() is (os.name != "nt")


def test_windows_standard_open_denies_cleanup_while_contender_holds(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows CRT sharing contract")
    path = tmp_path / locking.DOCUMENT_WRITER_LOCK
    child = subprocess.Popen([sys.executable, "-c", HOLDER, str(path)],
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "LOCKED"
        with pytest.raises(PermissionError) as caught:
            path.unlink()
        assert caught.value.winerror == 32
        with pytest.raises(locking.WriterLockBusy):
            locking.PathWriterLock(tmp_path).acquire()
    finally:
        child.terminate()
        child.wait(timeout=10)
    # A normal new lifecycle safely removes the crash/stale anchor.
    with locking.PathWriterLock(tmp_path):
        pass
    assert not path.exists()


def test_windows_open_unlocked_legacy_handle_prevents_release_cleanup(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows CRT sharing contract")
    path = tmp_path / locking.DOCUMENT_WRITER_LOCK
    holder = locking.PathWriterLock(tmp_path).acquire()
    child = subprocess.Popen([sys.executable, "-c", OPEN_ONLY, str(path)],
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "OPEN"
        holder.release()  # Cleanup must fail quietly because child has it open.
        assert path.exists()
    finally:
        child.terminate()
        child.wait(timeout=10)
    with locking.PathWriterLock(tmp_path):
        pass
    assert not path.exists()


def test_legacy_contender_is_blocked_while_new_holder_owns_same_lock(tmp_path):
    if os.name != "nt":
        pytest.skip("Legacy subprocess uses Windows msvcrt")
    path = tmp_path / locking.DOCUMENT_WRITER_LOCK
    with locking.PathWriterLock(tmp_path):
        child = subprocess.run([sys.executable, "-c", CONTENDER, str(path)],
                               capture_output=True, text=True, timeout=10)
        assert child.returncode == 3
    assert not path.exists()


def test_windows_cleanup_failure_is_swallowed_and_anchor_retained(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows-only cleanup")
    path = tmp_path / locking.DOCUMENT_WRITER_LOCK
    lock = locking.PathWriterLock(tmp_path).acquire()
    with patch.object(Path, "unlink", side_effect=PermissionError(32, "in use")) as remove:
        lock.release()
    remove.assert_called_once_with(missing_ok=True)
    assert path.exists()
    with locking.PathWriterLock(tmp_path):
        pass
    assert not path.exists()


def test_exception_releases_and_windows_cleanup_is_best_effort(tmp_path):
    path = tmp_path / locking.DOCUMENT_WRITER_LOCK
    with pytest.raises(RuntimeError, match="synthetic"):
        with locking.writer_lock(tmp_path):
            raise RuntimeError("synthetic")
    with locking.writer_lock(tmp_path):
        assert path.exists()
    assert path.exists() is (os.name != "nt")


def test_custom_request_and_ledger_anchors_keep_existing_lifecycle(tmp_path):
    for name in (".ai_review_request.lock", ".review_records.writer.lock"):
        with locking.writer_lock(tmp_path, name):
            assert (tmp_path / name).exists()
        assert (tmp_path / name).exists()


def test_invalid_root_or_lock_name_fails_before_protected_work(tmp_path):
    with pytest.raises(FileNotFoundError):
        locking.PathWriterLock(tmp_path / "missing")
    for name in ("", "../escape.lock", "folder/lock"):
        with pytest.raises(ValueError):
            locking.PathWriterLock(tmp_path, name)
