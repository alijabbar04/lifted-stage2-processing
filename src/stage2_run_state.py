"""Durable, read-only-observable metadata for a Stage 2 operation.

This module deliberately does not import the processing engine, provider clients,
or writer locks.  A :class:`RunPublisher` reports one operation; any number of
:class:`RunObserver` instances may inspect its files without acquiring ownership.

The registry is an observability surface, not processing or recovery authority.
In particular, ``operation-ended`` never means that the wider workflow has been
verified complete.
"""

from __future__ import annotations

import datetime as _datetime
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import uuid
from typing import Any, Mapping


SNAPSHOT_SCHEMA = "lifted-stage2-run/v1"
EVENT_SCHEMA = "lifted-stage2-run-event/v1"
MAX_SNAPSHOT_BYTES = 1024 * 1024
MAX_EVENT_BYTES = 512 * 1024
MAX_EVENT_LINE_BYTES = 32 * 1024
MAX_RUN_FILES = 10_000
MAX_TEXT = 2048
STALE_FLOOR_SECONDS = 15.0

_UPDATE_FIELDS = {
    "phase",
    "status",
    "completed",
    "total",
    "stats",
    "current_document",
    "cost_gbp",
    "last_activity",
    "audit_report",
}
_PUBLISHER_STATUSES = {"running", "operation-ended", "failed"}
_SNAPSHOT_FIELDS = {
    "schema", "run_id", "mode", "care_home_dir", "destination", "owner",
    "executable", "started_utc", "updated_utc", "heartbeat_seconds", "phase",
    "status", "completed", "total", "stats", "current_document", "cost_gbp",
    "last_activity", "audit_report", "operation_outcome", "error",
}
_EVENT_FIELDS = {
    "kind",
    "phase",
    "state",
    "label",
    "completed",
    "total",
    "stats",
    "current_document",
    "cost_gbp",
    "last_activity",
    "audit_report",
    "report",
    "errors",
    "needs_review",
}
_EVENT_RECORD_FIELDS = (_EVENT_FIELDS - {"report"}) | {
    "schema", "run_id", "timestamp_utc", "mode", "outcome", "error",
}


def _utc_now() -> _datetime.datetime:
    return _datetime.datetime.now(_datetime.timezone.utc)


def _utc_text() -> str:
    return _utc_now().isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> _datetime.datetime | None:
    try:
        parsed = _datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(_datetime.timezone.utc)


def _canonical(path: os.PathLike[str] | str) -> str:
    value = Path(path).expanduser().resolve(strict=False)
    if not value.is_absolute() or "\x00" in str(value):
        raise ValueError("run paths must be absolute filesystem paths")
    return str(value)


def registry_directory() -> Path:
    """Return the default registry path without creating it."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "Lifted" / "Stage2" / "runs"
        return Path.home() / "AppData" / "Local" / "Lifted" / "Stage2" / "runs"
    base = os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base) / "lifted" / "stage2" / "runs"
    return Path.home() / ".local" / "state" / "lifted" / "stage2" / "runs"


def process_identity(pid: int) -> dict[str, Any] | None:
    """Return an identity that distinguishes a process from later PID reuse.

    Failure to inspect a process is represented by ``None``.  Callers must treat
    that as unknown/ended evidence rather than falling back to PID-only liveness.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None

    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            query_limited = 0x1000
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetProcessTimes.argtypes = (
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            )
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            handle = kernel32.OpenProcess(query_limited, False, pid)
            if not handle:
                return None
            try:
                created = wintypes.FILETIME()
                exited = wintypes.FILETIME()
                kernel = wintypes.FILETIME()
                user = wintypes.FILETIME()
                if not kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(created),
                    ctypes.byref(exited),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                ):
                    return None
                if exited.dwHighDateTime or exited.dwLowDateTime:
                    return None
                token = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
                return {"pid": pid, "kind": "windows-filetime", "created": str(token)}
            finally:
                kernel32.CloseHandle(handle)
        except (OSError, AttributeError, TypeError, ValueError):
            return None

    # Linux provides a boot-relative process start token in /proc/<pid>/stat.
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii")
        close = stat.rfind(")")
        fields = stat[close + 2 :].split()
        start_ticks = fields[19]  # field 22 overall; fields begin at field 3
        boot_id_path = Path("/proc/sys/kernel/random/boot_id")
        boot_id = boot_id_path.read_text(encoding="ascii").strip()
        if not boot_id or not start_ticks:
            return None
        return {
            "pid": pid,
            "kind": "linux-proc-start",
            "created": f"{boot_id}:{start_ticks}",
        }
    except (OSError, IndexError, ValueError):
        return None


def _pid_definitely_absent(pid: int) -> bool:
    """Return True only when the OS gives affirmative owner-ended evidence."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            handle = kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                # ERROR_INVALID_PARAMETER is the documented missing-PID result;
                # access denied and all other failures remain unknown.
                return ctypes.get_last_error() == 87
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return int(exit_code.value) != 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except (OSError, AttributeError, TypeError, ValueError):
            return False
    try:
        os.kill(int(pid), 0)
        return False
    except ProcessLookupError:
        return True
    except (PermissionError, OSError, TypeError, ValueError):
        return False


def _identity_matches(saved: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    return (
        saved.get("pid") == current.get("pid")
        and saved.get("kind") == current.get("kind")
        and isinstance(saved.get("created"), str)
        and saved.get("created") == current.get("created")
    )


def _bounded_text(value: Any, *, field: str, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    value = value.strip()
    if not allow_empty and not value:
        raise ValueError(f"{field} must not be empty")
    if len(value) > MAX_TEXT or "\x00" in value:
        raise ValueError(f"{field} is invalid or too long")
    return value


def _safe_number(value: Any, *, field: str, integer: bool = False) -> int | float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    if integer:
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"{field} must be a non-negative integer")
        return value
    if not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"{field} must be a non-negative number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{field} must be a non-negative number")
    return value


def _safe_stats(value: Any) -> dict[str, int | float | str | bool | None]:
    if not isinstance(value, Mapping) or len(value) > 100:
        raise ValueError("stats must be a small mapping")
    result: dict[str, int | float | str | bool | None] = {}
    for key, item in value.items():
        key = _bounded_text(key, field="stats key", allow_empty=False)
        if isinstance(item, str):
            result[key] = _bounded_text(item, field=f"stats[{key}]")
        elif item is None or isinstance(item, (bool, int, float)):
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError(f"stats[{key}] is not finite")
            result[key] = item
        else:
            raise ValueError("stats values must be scalar")
    return result


def _normalise_update(fields: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(fields) - _UPDATE_FIELDS
    if unknown:
        raise ValueError(f"unsupported run fields: {', '.join(sorted(unknown))}")
    result: dict[str, Any] = {}
    for key, value in fields.items():
        if key in {"phase", "current_document", "last_activity", "audit_report"}:
            result[key] = _bounded_text(value, field=key)
        elif key == "status":
            status = _bounded_text(value, field=key, allow_empty=False)
            if status not in _PUBLISHER_STATUSES:
                raise ValueError("unsupported publisher status")
            result[key] = status
        elif key in {"completed", "total"}:
            result[key] = _safe_number(value, field=key, integer=True)
        elif key == "cost_gbp":
            result[key] = _safe_number(value, field=key)
        elif key == "stats":
            result[key] = _safe_stats(value)
    if "completed" in result and "total" in result and result["completed"] > result["total"]:
        raise ValueError("completed cannot exceed total")
    return result


def _write_atomic(path: Path, value: Mapping[str, Any]) -> None:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        raise ValueError("run snapshot is too large")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        # A Windows reader that opened the prior snapshot without delete sharing
        # can briefly block replacement.  Observer reads are intentionally short;
        # retry the atomic rename rather than falling back to in-place truncation.
        for attempt in range(50):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 49:
                    raise
                time.sleep(0.01)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _is_reparse(path: Path) -> bool:
    stat = path.lstat()
    attributes = getattr(stat, "st_file_attributes", 0)
    return path.is_symlink() or bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def _read_json(path: Path, maximum: int) -> Any | None:
    for attempt in range(5):
        try:
            if _is_reparse(path) or not path.is_file() or path.stat().st_size > maximum:
                return None
            raw = path.read_bytes()
            if not raw or len(raw) > maximum:
                return None
            return json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return None
        except OSError:
            # Windows replacement can briefly make a valid name unavailable to a
            # concurrent reader.  A bounded retry still fails closed for damage.
            if attempt == 4:
                return None
            time.sleep(0.005)
    return None


class RunPublisher:
    """Publish one operation's status for read-only viewers."""

    def __init__(
        self,
        care_home_dir: os.PathLike[str] | str,
        *,
        mode: str,
        destination: os.PathLike[str] | str | None = None,
        registry_root: os.PathLike[str] | str | None = None,
        heartbeat_seconds: float = 5,
    ) -> None:
        if isinstance(heartbeat_seconds, bool) or not isinstance(heartbeat_seconds, (int, float)):
            raise ValueError("heartbeat_seconds must be numeric")
        if heartbeat_seconds < 0.05 or heartbeat_seconds > 3600:
            raise ValueError("heartbeat_seconds is outside the supported range")
        self.run_id = str(uuid.uuid4())
        self.registry_root = Path(registry_root) if registry_root is not None else registry_directory()
        self.registry_root = self.registry_root.expanduser().resolve(strict=False)
        self.snapshot_path = self.registry_root / f"{self.run_id}.json"
        self.events_path = self.registry_root / f"{self.run_id}.events.jsonl"
        self._heartbeat_seconds = float(heartbeat_seconds)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._finished = False
        now = _utc_text()
        owner = process_identity(os.getpid())
        if owner is None:
            # Unknown creation identity is retained explicitly; viewers will never
            # downgrade to unsafe PID-only liveness.
            owner = {"pid": os.getpid(), "kind": "unknown", "created": ""}
        self._snapshot: dict[str, Any] = {
            "schema": SNAPSHOT_SCHEMA,
            "run_id": self.run_id,
            "mode": _bounded_text(mode, field="mode", allow_empty=False),
            "care_home_dir": _canonical(care_home_dir),
            "destination": _canonical(destination) if destination is not None else "",
            "owner": owner,
            "executable": _canonical(sys.executable),
            "started_utc": now,
            "updated_utc": now,
            "heartbeat_seconds": self._heartbeat_seconds,
            "phase": "starting",
            "status": "running",
            "completed": 0,
            "total": 0,
            "stats": {},
            "current_document": "",
            "cost_gbp": 0,
            "last_activity": "operation-started",
            "audit_report": "",
            "operation_outcome": "",
            "error": "",
        }
        _write_atomic(self.snapshot_path, self._snapshot)
        try:
            self._append_event({"kind": "operation-started", "mode": self._snapshot["mode"]})
        except Exception:
            # The atomic snapshot is the primary observer surface.  A transient
            # diagnostic-journal failure must not prevent heartbeat publication.
            pass
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"stage2-run-heartbeat-{self.run_id[:8]}",
            daemon=True,
        )
        self._thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self._heartbeat_seconds):
            try:
                with self._lock:
                    if self._finished:
                        return
                    self._snapshot["updated_utc"] = _utc_text()
                    _write_atomic(self.snapshot_path, self._snapshot)
            except Exception:
                # A reporting failure must not terminate the processing operation.
                # The parent callback guard may additionally surface this failure.
                continue

    def _append_event(self, event: Mapping[str, Any]) -> None:
        record = {
            "schema": EVENT_SCHEMA,
            "run_id": self.run_id,
            "timestamp_utc": _utc_text(),
            **event,
        }
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8") + b"\n"
        if len(encoded) > MAX_EVENT_LINE_BYTES:
            raise ValueError("run event is too large")
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            current_size = self.events_path.stat().st_size
        except FileNotFoundError:
            current_size = 0
        if current_size + len(encoded) > MAX_EVENT_BYTES:
            backup = self.events_path.with_name(self.events_path.name + ".1")
            try:
                os.replace(self.events_path, backup)
            except FileNotFoundError:
                pass
        with self.events_path.open("ab") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())

    def update(self, **fields: Any) -> dict[str, Any]:
        """Atomically update whitelisted observer fields and return a copy."""
        normalised = _normalise_update(fields)
        with self._lock:
            if self._finished:
                raise RuntimeError("run publisher is already finished")
            candidate = dict(self._snapshot)
            candidate.update(normalised)
            if candidate["completed"] > candidate["total"]:
                raise ValueError("completed cannot exceed total")
            candidate["updated_utc"] = _utc_text()
            _write_atomic(self.snapshot_path, candidate)
            self._snapshot = candidate
            return dict(candidate)

    def event(self, activity: Mapping[str, Any]) -> dict[str, Any]:
        """Map a native activity event into safe snapshot fields and the journal."""
        if not isinstance(activity, Mapping):
            raise ValueError("activity must be a mapping")
        filtered = {key: activity[key] for key in _EVENT_FIELDS if key in activity}
        kind = _bounded_text(filtered.get("kind", "activity"), field="kind", allow_empty=False)
        updates: dict[str, Any] = {}
        for key in ("phase", "completed", "total", "stats", "current_document", "cost_gbp", "audit_report"):
            if key in filtered:
                updates[key] = filtered[key]
        if "report" in filtered and "audit_report" not in updates:
            updates["audit_report"] = filtered["report"]
        updates["last_activity"] = kind
        snapshot = self.update(**updates)

        safe_event: dict[str, Any] = {"kind": kind}
        for key, value in filtered.items():
            if key == "kind":
                continue
            mapped = "audit_report" if key == "report" else key
            if mapped in _UPDATE_FIELDS:
                safe_event[mapped] = _normalise_update({mapped: value})[mapped]
            elif key in {"state", "label"}:
                safe_event[key] = _bounded_text(value, field=key)
            elif key in {"errors", "needs_review"}:
                safe_event[key] = _safe_number(value, field=key, integer=True)
        with self._lock:
            self._append_event(safe_event)
        return snapshot

    def finish(self, outcome: str = "operation-ended", error: str = "") -> dict[str, Any]:
        """End publication without asserting overall workflow verification."""
        outcome = _bounded_text(outcome, field="outcome", allow_empty=False)
        error = _bounded_text(error, field="error")
        self._stop.set()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=min(2.0, max(0.2, self._heartbeat_seconds * 2)))
        with self._lock:
            if self._finished:
                return dict(self._snapshot)
            candidate = dict(self._snapshot)
            candidate["status"] = "failed" if error or outcome == "failed" else "operation-ended"
            candidate["operation_outcome"] = outcome
            candidate["error"] = error
            candidate["last_activity"] = "operation-finished"
            candidate["updated_utc"] = _utc_text()
            _write_atomic(self.snapshot_path, candidate)
            self._snapshot = candidate
            self._finished = True
            self._append_event({"kind": "operation-finished", "outcome": outcome, "error": error})
            return dict(candidate)


def _valid_snapshot(raw: Any, expected_run_id: str | None = None) -> dict[str, Any] | None:
    if (not isinstance(raw, dict) or raw.get("schema") != SNAPSHOT_SCHEMA
            or set(raw) != _SNAPSHOT_FIELDS):
        return None
    try:
        run_id = str(uuid.UUID(raw["run_id"]))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if run_id != raw.get("run_id") or (expected_run_id is not None and run_id != expected_run_id):
        return None
    required_text = (
        "mode", "care_home_dir", "destination", "executable", "started_utc",
        "updated_utc", "phase", "status", "current_document", "last_activity",
        "audit_report", "operation_outcome", "error",
    )
    try:
        for key in required_text:
            _bounded_text(raw.get(key), field=key, allow_empty=key not in {"mode", "care_home_dir", "executable", "status"})
    except ValueError:
        return None
    if (not Path(raw["care_home_dir"]).is_absolute()
            or not Path(raw["executable"]).is_absolute()
            or (raw["destination"] and not Path(raw["destination"]).is_absolute())):
        return None
    if _parse_utc(raw["started_utc"]) is None or _parse_utc(raw["updated_utc"]) is None:
        return None
    if raw["status"] not in _PUBLISHER_STATUSES:
        return None
    owner = raw.get("owner")
    if not isinstance(owner, dict) or not isinstance(owner.get("pid"), int) or owner["pid"] <= 0:
        return None
    if (not isinstance(owner.get("kind"), str) or not isinstance(owner.get("created"), str)
            or set(owner) != {"pid", "kind", "created"}
            or len(owner["kind"]) > MAX_TEXT or len(owner["created"]) > MAX_TEXT):
        return None
    heartbeat = raw.get("heartbeat_seconds")
    if isinstance(heartbeat, bool) or not isinstance(heartbeat, (int, float)) or not 0.05 <= heartbeat <= 3600:
        return None
    if not isinstance(raw.get("completed"), int) or not isinstance(raw.get("total"), int):
        return None
    if raw["completed"] < 0 or raw["total"] < 0 or raw["completed"] > raw["total"]:
        return None
    try:
        _safe_number(raw.get("cost_gbp"), field="cost_gbp")
        if _safe_stats(raw.get("stats")) != raw["stats"]:
            return None
    except ValueError:
        return None
    return dict(raw)


def _valid_event(raw: Any, expected_run_id: str) -> dict[str, Any] | None:
    if (not isinstance(raw, dict) or raw.get("schema") != EVENT_SCHEMA
            or raw.get("run_id") != expected_run_id
            or not set(raw).issubset(_EVENT_RECORD_FIELDS)
            or _parse_utc(raw.get("timestamp_utc")) is None):
        return None
    try:
        _bounded_text(raw.get("kind"), field="kind", allow_empty=False)
        for key in ("mode", "phase", "state", "label", "current_document",
                    "last_activity", "audit_report", "outcome", "error"):
            if key in raw:
                _bounded_text(raw[key], field=key)
        for key in ("completed", "total", "errors", "needs_review"):
            if key in raw:
                _safe_number(raw[key], field=key, integer=True)
        if "cost_gbp" in raw:
            _safe_number(raw["cost_gbp"], field="cost_gbp")
        if "stats" in raw:
            _safe_stats(raw["stats"])
    except ValueError:
        return None
    return dict(raw)


class RunObserver:
    """Read registry state without creating files, locks, or provider clients."""

    def __init__(self, registry_root: os.PathLike[str] | str | None = None) -> None:
        root = Path(registry_root) if registry_root is not None else registry_directory()
        self.registry_root = root.expanduser().resolve(strict=False)

    def _heartbeat_age(self, snapshot: Mapping[str, Any]) -> float | None:
        updated = _parse_utc(snapshot["updated_utc"])
        if updated is None:
            return None
        return (_utc_now() - updated).total_seconds()

    def _display_state(self, snapshot: dict[str, Any], heartbeat_age: float | None) -> str:
        if snapshot["status"] == "failed":
            return "failed"
        if snapshot["status"] == "operation-ended":
            return "operation-ended"
        current = process_identity(snapshot["owner"]["pid"])
        # Inspection failure and an owner that could not establish its creation
        # token are uncertainty, not proof that the owner ended.
        if snapshot["owner"].get("kind") == "unknown":
            return "stale-unverified"
        if current is None:
            return "owner-ended" if _pid_definitely_absent(snapshot["owner"]["pid"]) else "stale-unverified"
        if not _identity_matches(snapshot["owner"], current):
            return "owner-ended"
        if heartbeat_age is None:
            return "stale-unverified"
        threshold = max(STALE_FLOOR_SECONDS, float(snapshot["heartbeat_seconds"]) * 3)
        if heartbeat_age < -300 or heartbeat_age > threshold:
            return "stale-unverified"
        return "running"

    def get(self, run_id: str) -> dict[str, Any] | None:
        try:
            canonical_id = str(uuid.UUID(run_id))
        except (TypeError, ValueError, AttributeError):
            return None
        if canonical_id != run_id:
            return None
        raw = _read_json(self.registry_root / f"{canonical_id}.json", MAX_SNAPSHOT_BYTES)
        snapshot = _valid_snapshot(raw, canonical_id)
        if snapshot is None:
            return None
        heartbeat_age = self._heartbeat_age(snapshot)
        snapshot["heartbeat_age_seconds"] = round(heartbeat_age, 3) if heartbeat_age is not None else None
        snapshot["display_state"] = self._display_state(snapshot, heartbeat_age)
        return snapshot

    def list_runs(self, care_home_dir: os.PathLike[str] | str | None = None) -> list[dict[str, Any]]:
        if not self.registry_root.is_dir():
            return []
        wanted = os.path.normcase(_canonical(care_home_dir)) if care_home_dir is not None else None
        try:
            paths = list(self.registry_root.glob("*.json"))[:MAX_RUN_FILES]
        except OSError:
            return []
        runs: list[dict[str, Any]] = []
        for path in paths:
            run = self.get(path.stem)
            if run is None:
                continue
            if wanted is not None and os.path.normcase(run["care_home_dir"]) != wanted:
                continue
            runs.append(run)
        runs.sort(key=lambda item: (item["updated_utc"], item["run_id"]), reverse=True)
        return runs

    def active_for(
        self,
        care_home_dir: os.PathLike[str] | str,
        exclude_pid: int | None = None,
    ) -> list[dict[str, Any]]:
        result = []
        for run in self.list_runs(care_home_dir):
            if exclude_pid is not None and run["owner"]["pid"] == exclude_pid:
                continue
            if run["display_state"] in {"running", "stale-unverified"}:
                result.append(run)
        return result

    def read_events(self, run_id: str, limit: int = 80) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("event limit must be between 1 and 1000")
        try:
            canonical_id = str(uuid.UUID(run_id))
        except (TypeError, ValueError, AttributeError):
            return []
        if canonical_id != run_id:
            return []
        path = self.registry_root / f"{canonical_id}.events.jsonl"
        backup = path.with_name(path.name + ".1")
        lines: list[bytes] = []
        for candidate in (backup, path):
            try:
                if not candidate.exists():
                    continue
                if _is_reparse(candidate) or not candidate.is_file() or candidate.stat().st_size > MAX_EVENT_BYTES:
                    continue
                lines.extend(candidate.read_bytes().splitlines())
            except OSError:
                continue
        lines = lines[-limit:]
        events: list[dict[str, Any]] = []
        for line in lines:
            if not line or len(line) > MAX_EVENT_LINE_BYTES:
                continue
            try:
                record = json.loads(line.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                continue
            validated = _valid_event(record, canonical_id)
            if validated is not None:
                events.append(validated)
        return events


__all__ = [
    "RunObserver",
    "RunPublisher",
    "process_identity",
    "registry_directory",
]
