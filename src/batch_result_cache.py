"""Durable, hash-bound cache for completed Message Batch JSONL results.

The cache is deliberately conservative: a cache entry is usable only when its
batch identity, results URL binding, expected custom-id set, and full row
content all validate.  Partial or corrupt entries are treated as misses and
are never passed to the apply path.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Iterable


SCHEMA = 1


class ResultCacheError(ValueError):
    """A cache entry or result set is malformed or incomplete."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _expected_ids(expected_ids: Iterable[str]) -> list[str]:
    ids = list(expected_ids)
    if any(not isinstance(item, str) or not item for item in ids):
        raise ResultCacheError("expected custom IDs are malformed")
    if len(set(ids)) != len(ids):
        raise ResultCacheError("expected custom IDs contain duplicates")
    return sorted(ids)


def validate_rows(rows: Any, expected_ids: Iterable[str]) -> list[dict[str, Any]]:
    """Validate a complete provider result set and return a detached list."""
    expected = _expected_ids(expected_ids)
    if not isinstance(rows, list):
        raise ResultCacheError("batch results are not a list")
    if any(not isinstance(row, dict) for row in rows):
        raise ResultCacheError("batch result row is not an object")
    ids = [row.get("custom_id") for row in rows]
    if any(not isinstance(item, str) or not item for item in ids):
        raise ResultCacheError("batch result custom ID is malformed")
    if any(not isinstance(row.get("result"), dict) for row in rows):
        raise ResultCacheError("batch result envelope is missing")
    if len(ids) != len(set(ids)):
        raise ResultCacheError("batch results contain duplicate custom IDs")
    if sorted(ids) != expected:
        raise ResultCacheError("batch results are incomplete or outside the saved request set")
    return json.loads(json.dumps(rows, ensure_ascii=False))


def cache_path(root: Path, batch_id: str) -> Path:
    if not isinstance(batch_id, str) or not batch_id:
        raise ResultCacheError("batch ID is malformed")
    digest = hashlib.sha256(batch_id.encode("utf-8")).hexdigest()
    return Path(root) / ".batch-result-cache" / f"{digest}.json"


def load(root: Path, batch_id: str, expected_ids: Iterable[str],
         results_url: str | None = None) -> list[dict[str, Any]] | None:
    """Return a validated cached result set, or ``None`` for any cache miss."""
    path = cache_path(root, batch_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        expected = _expected_ids(expected_ids)
        if (not isinstance(payload, dict) or payload.get("schema") != SCHEMA
                or payload.get("batch_id") != batch_id
                or payload.get("expected_ids_sha256") != _sha(expected)
                or payload.get("expected_count") != len(expected)):
            return None
        if results_url is not None and payload.get("results_url_sha256") != _sha(results_url):
            return None
        rows = validate_rows(payload.get("rows"), expected)
        if payload.get("content_sha256") != _sha(rows):
            return None
        return rows
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def load_candidate(root: Path, batch_id: str,
                   results_url: str | None = None) -> list[dict[str, Any]] | None:
    """Load a self-bound legacy candidate for later phase-union validation.

    Legacy state may not retain the per-batch ID set.  The candidate is still
    checked against its own payload hashes, but callers must validate the
    combined IDs against the trusted phase union before using or re-caching it.
    """
    path = cache_path(root, batch_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(payload, dict) or payload.get("schema") != SCHEMA
                or payload.get("batch_id") != batch_id):
            return None
        if results_url is not None and payload.get("results_url_sha256") != _sha(results_url):
            return None
        rows = payload.get("rows")
        if not isinstance(rows, list):
            return None
        ids = [row.get("custom_id") for row in rows
               if isinstance(row, dict)]
        if (len(ids) != len(rows) or payload.get("expected_count") != len(ids)
                or payload.get("expected_ids_sha256") != _sha(_expected_ids(ids))):
            return None
        validated = validate_rows(rows, ids)
        if payload.get("content_sha256") != _sha(validated):
            return None
        return validated
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def store(root: Path, batch_id: str, expected_ids: Iterable[str],
          results_url: str, rows: Any) -> Path:
    """Atomically persist one complete, hash-bound result set."""
    expected = _expected_ids(expected_ids)
    validated = validate_rows(rows, expected)
    destination = cache_path(root, batch_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": SCHEMA,
        "batch_id": batch_id,
        "expected_count": len(expected),
        "expected_ids_sha256": _sha(expected),
        "results_url_sha256": _sha(results_url),
        "content_sha256": _sha(validated),
        "rows": validated,
    }
    encoded = _canonical(payload)
    temp_name = None
    try:
        with NamedTemporaryFile("wb", dir=destination.parent, prefix=".tmp-",
                                suffix=".json", delete=False) as handle:
            temp_name = handle.name
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, destination)
        temp_name = None
        try:
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
    return destination
