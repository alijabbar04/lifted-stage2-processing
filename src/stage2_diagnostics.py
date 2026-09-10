"""Small, defensive diagnostics sink for the Stage 2 desktop shell.

This module records UI and lifecycle evidence only.  It deliberately does not
capture stdout, prompts, documents, configuration values, or provider payloads.
"""
from __future__ import annotations

import atexit
import faulthandler
import json
import logging
import logging.handlers
import os
from pathlib import Path
import re
import sys
import threading
import traceback
from datetime import datetime, timezone

_SECRET = re.compile(r"(?i)(api[_ -]?key|token|secret|password|authorization|cookie)\s*[:=]\s*[^,;\s]+")
_PATH = re.compile(r"(?i)([A-Z]:\\|/)[^\s\"']+")
_CONTENT_KEYS = {"prompt", "document", "filename", "path", "config", "settings", "payload", "headers"}


def _safe(value, key=""):
    if key.lower() in _CONTENT_KEYS:
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): _safe(v, str(k)) for k, v in value.items() if str(k).lower() not in _CONTENT_KEYS}
    if isinstance(value, (list, tuple)):
        return [_safe(v, key) for v in value[:20]]
    text = str(value)
    text = _SECRET.sub(r"\1=<redacted>", text)
    return _PATH.sub("<path>", text)[:500]


class Diagnostics:
    def __init__(self, root=None, *, max_bytes=512 * 1024, backups=3):
        self.directory = Path(root or (Path.home() / "AppData" / "Local" / "Lifted" / "Stage2Diagnostics"))
        self.directory.mkdir(parents=True, exist_ok=True)
        self.process_id = os.getpid()
        self.process_start = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        self.path = self.directory / f"stage2-diagnostics-{self.process_id}-{self.process_start}.log"
        self._logger = logging.getLogger(f"stage2.diagnostics.{self.process_id}.{id(self)}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        if not self._logger.handlers:
            handler = logging.handlers.RotatingFileHandler(self.path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)
        self._old_excepthook = sys.excepthook
        self._old_thread_hook = getattr(threading, "excepthook", None)
        self._tk = None
        self._tk_pending = {}
        self._fault_stream = None
        self._old_tk_callback = None
        self.record("startup", pid=os.getpid(), thread=threading.current_thread().name)

    def record(self, event, **metadata):
        try:
            row = {"ts": datetime.now(timezone.utc).isoformat(), "event": str(event)[:80]}
            row.update({str(k): _safe(v, str(k)) for k, v in metadata.items()})
            self._logger.info(json.dumps(row, ensure_ascii=True, sort_keys=True))
        except Exception:
            return None

    def attach_tk(self, tkroot):
        self._tk = tkroot
        self._old_tk_callback = getattr(tkroot, "report_callback_exception", None)
        tkroot.report_callback_exception = self._tk_callback_exception
        for sequence, event in (("<Map>", "window_map"), ("<Unmap>", "window_unmap"),
                                ("<Configure>", "window_configure"), ("<Destroy>", "window_close")):
            try:
                tkroot.bind(sequence, lambda tk_event, name=event: self._tk_root_event(tk_event, name), add="+")
            except Exception:
                self.record("tk_bind_failed", sequence=sequence)
        self.record("tk_attached")
        return self

    def _tk_root_event(self, tk_event, name):
        if getattr(tk_event, "widget", self._tk) is self._tk:
            self._tk_event(name)

    def _tk_event(self, name):
        """Debounce noisy Windows Configure/Map notifications on the Tk loop."""
        pending = self._tk_pending.get(name)
        if pending is not None:
            try:
                self._tk.after_cancel(pending)
            except Exception:
                pass
        try:
            self._tk_pending[name] = self._tk.after(250, lambda: self._record_tk(name))
        except Exception:
            self._record_tk(name)

    def _record_tk(self, name):
        self._tk_pending.pop(name, None)
        self.record(name)

    def _tk_callback_exception(self, exc_type, exc, tb):
        frame = traceback.extract_tb(tb)[-1] if tb else None
        self.record("tk_callback_exception", type=getattr(exc_type, "__name__", "Exception"),
                    detail=traceback.format_exception_only(exc_type, exc)[-1].strip(),
                    file=os.path.basename(frame.filename) if frame else "",
                    function=frame.name if frame else "", line=frame.lineno if frame else 0)
        if callable(self._old_tk_callback):
            try:
                return self._old_tk_callback(exc_type, exc, tb)
            except Exception:
                self.record("original_tk_callback_failed")

    def install_hooks(self):
        def excepthook(exc_type, exc, tb):
            frame = traceback.extract_tb(tb)[-1] if tb else None
            self.record("fatal_exception", type=getattr(exc_type, "__name__", "Exception"), detail=traceback.format_exception_only(exc_type, exc)[-1].strip(), file=os.path.basename(frame.filename) if frame else "", function=frame.name if frame else "", line=frame.lineno if frame else 0)
            try:
                return self._old_excepthook(exc_type, exc, tb)
            except Exception:
                self.record("original_excepthook_failed")
        sys.excepthook = excepthook
        if self._old_thread_hook:
            def thread_hook(args):
                frame = traceback.extract_tb(args.exc_traceback)[-1] if args.exc_traceback else None
                self.record("thread_exception", type=getattr(args.exc_type, "__name__", "Exception"), thread=args.thread.name if args.thread else "unknown", file=os.path.basename(frame.filename) if frame else "", function=frame.name if frame else "", line=frame.lineno if frame else 0)
                try:
                    return self._old_thread_hook(args)
                except Exception:
                    self.record("original_thread_hook_failed")
            threading.excepthook = thread_hook
        try:
            self._fault_stream = open(self.directory / f"stage2-fault-{self.process_id}-{self.process_start}.log", "a", encoding="utf-8")
            faulthandler.enable(file=self._fault_stream)
        except Exception:
            self.record("fault_handler_unavailable")
        atexit.register(self.close)
        return self

    def close(self):
        self.record("shutdown")
        try:
            if self._fault_stream is not None:
                faulthandler.disable()
                self._fault_stream.close()
                self._fault_stream = None
        except Exception:
            pass
        for handler in list(self._logger.handlers):
            try:
                handler.flush()
                handler.close()
                self._logger.removeHandler(handler)
            except Exception:
                pass


def install(root=None):
    """Install and return the process diagnostics sink."""
    global _DEFAULT
    requested = Path(root).resolve() if root is not None else None
    if _DEFAULT is not None and (requested is None or _DEFAULT.directory == requested):
        return _DEFAULT
    try:
        _DEFAULT = Diagnostics(root).install_hooks()
    except Exception:
        _DEFAULT = NullDiagnostics(root)
    return _DEFAULT


_DEFAULT = None


class NullDiagnostics:
    """Startup-safe fallback when the diagnostics directory cannot be created."""
    def __init__(self, root=None):
        self.directory = Path(root) if root is not None else Path()
    def record(self, *_args, **_kwargs):
        return None
    def attach_tk(self, _tkroot):
        return self
    def install_hooks(self):
        return self
    def close(self):
        return None
