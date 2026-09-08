"""Shared OS-backed path locks used by Stage 2 and its review helper.

The lock filename is the coordination identity, including for older Stage 2
versions.  On Windows a normal Python CRT handle denies file deletion while it
is open, so an unlocked/closed anchor can be removed without racing a contender:
if another process has opened it, removal fails and the anchor is retained.
POSIX can unlink an open inode, so anchors remain there permanently.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path


DOCUMENT_WRITER_LOCK = ".docreview_batch_writer.lock"


class WriterLockBusy(RuntimeError):
    """The path lock is held or unavailable; callers must fail closed."""


class PathWriterLock:
    """Nonblocking one-byte lock on a stable path shared with legacy builds."""

    def __init__(self, root, name=DOCUMENT_WRITER_LOCK, *, cleanup_windows=None):
        self.root = Path(root).resolve(strict=True)
        if Path(name).name != name or name in ("", ".", ".."):
            raise ValueError("Lock name must be one filename")
        self.path = self.root / name
        self.cleanup_windows = (name == DOCUMENT_WRITER_LOCK
                                if cleanup_windows is None else bool(cleanup_windows))
        self.stream = None

    def acquire(self):
        if self.stream is not None:
            raise RuntimeError("Writer lock is already acquired")
        stream = None
        try:
            stream = self.path.open("a+b")
            stream.seek(0, 2)
            if not stream.tell():
                stream.write(b"\0")
                stream.flush()
                os.fsync(stream.fileno())
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.stream = stream
            return self
        except OSError as exc:
            if stream is not None:
                stream.close()
            raise WriterLockBusy("Another operation owns this folder's writer lock") from exc

    def release(self):
        stream, self.stream = self.stream, None
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
            # Keep the exact legacy pathname throughout protected work.  A
            # contender's ordinary Windows CRT handle denies this deletion,
            # so failure means "leave it", never replace/unlink its anchor.
            if os.name == "nt" and self.cleanup_windows:
                try:
                    self.path.unlink(missing_ok=True)
                except OSError:
                    pass

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_args):
        self.release()


@contextlib.contextmanager
def writer_lock(root, name=DOCUMENT_WRITER_LOCK, *, cleanup_windows=None):
    lock = PathWriterLock(root, name, cleanup_windows=cleanup_windows)
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()
