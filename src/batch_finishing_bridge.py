"""Discounted Message-Batch bridge for Stage 2 finishing operations.

This module is deliberately separate from :mod:`Stage2_Processing`.  Preparing
requests is local-only, submitting is an explicit operation, and polling only
installs answers from already accepted batches.  It never runs the worker tail
or any document audit itself.

The caller must provide an Engine whose ``_batch_state`` is already loaded.
The normal Engine can then replay the installed ``finishing_operations`` while
its live ``_post`` and classification ``submit_batch`` paths are disabled by
the recovery runner.
"""

from __future__ import annotations

import copy
import datetime as _datetime
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from types import MethodType
from typing import Any, Dict, Iterable, List, Optional, Tuple


LEDGER_NAME = ".docreview_finishing_batch.json"
LEDGER_VERSION = 1
CHUNK_TARGET_BYTES = 8 * 1024 * 1024
MAX_REQUESTS_PER_CHUNK = 10_000


class BatchFinishingError(RuntimeError):
    """The bridge failed closed before it could prove a safe next action."""


class SubmissionAmbiguous(BatchFinishingError):
    """A batch POST may have been accepted and must never be retried."""


class ResultValidationError(BatchFinishingError):
    """Provider results were not an exact, usable answer set."""


class _CapturedPost(BaseException):
    """Escapes an existing API helper before its live ``_post`` can run."""

    def __init__(self, system: str, blocks: list, max_tokens: int,
                 cache_system: bool):
        super().__init__("captured")
        self.system = system
        self.blocks = blocks
        self.max_tokens = max_tokens
        self.cache_system = cache_system


def _now() -> str:
    return _datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _wire_size(requests: list) -> int:
    # ClaudeAPI._http serialises with json.dumps' normal separators.
    return len(json.dumps({"requests": requests},
                          ensure_ascii=True).encode("utf-8"))


def _state(engine):
    state = getattr(engine, "_batch_state", None)
    if state is None or not hasattr(state, "data") or not hasattr(state, "save"):
        raise BatchFinishingError(
            "an already-loaded native Engine._batch_state is required")
    return state


def default_ledger_path(engine) -> Path:
    state = _state(engine)
    state_path = Path(getattr(state, "path", ""))
    if not state_path.name:
        raise BatchFinishingError("the native batch state has no durable path")
    return state_path.with_name(LEDGER_NAME)


def _writer(engine):
    module = sys.modules.get(engine.__class__.__module__)
    writer = getattr(module, "_write_hidden_json", None) if module else None
    if not callable(writer):
        state = _state(engine)
        module = sys.modules.get(state.__class__.__module__)
        writer = getattr(module, "_write_hidden_json", None) if module else None
    if not callable(writer):
        raise BatchFinishingError(
            "Stage 2's atomic hidden-JSON writer is unavailable")
    return writer


def _save_ledger(engine, path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _writer(engine)(path, data)


def _load_ledger(engine, path: Optional[Path]) -> Tuple[Path, dict]:
    target = Path(path) if path is not None else default_ledger_path(engine)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BatchFinishingError(f"finishing ledger does not exist: {target}") from exc
    except Exception as exc:
        raise BatchFinishingError(f"finishing ledger is unreadable: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != LEDGER_VERSION:
        raise BatchFinishingError("finishing ledger version is missing or unsupported")
    if not isinstance(data.get("operations"), dict) or not isinstance(
            data.get("chunks"), list):
        raise BatchFinishingError("finishing ledger structure is incomplete")
    return target, data


def _file_hash(engine, path: Path) -> str:
    module = sys.modules.get(engine.__class__.__module__)
    helper = getattr(module, "file_hash", None) if module else None
    if callable(helper):
        return str(helper(path)).casefold()
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _worker_key(worker_dir: Path) -> str:
    return str(Path(worker_dir).resolve()).casefold()


def _operation_state(engine, worker_dir: Path, operation: str) -> dict:
    worker = ((_state(engine).data.get("workers") or {}).get(
        _worker_key(worker_dir)) or {})
    return ((worker.get("finishing_operations") or {}).get(operation) or {})


def _capture_params(api, method: str, args: tuple) -> dict:
    """Invoke a real ClaudeAPI helper but stop exactly at its `_post` call."""
    clone = copy.copy(api)

    def capture(_self, system, content_blocks, max_tokens=400,
                cache_system=False):
        raise _CapturedPost(system, content_blocks, max_tokens, cache_system)

    clone._post = MethodType(capture, clone)
    try:
        getattr(clone, method)(*args)
    except _CapturedPost as captured:
        system_field = clone._system_field(
            captured.system, captured.cache_system)
        return {
            "model": clone.model_id,
            "max_tokens": captured.max_tokens,
            "temperature": 0,
            "system": system_field,
            "messages": [{"role": "user", "content": captured.blocks}],
        }
    raise BatchFinishingError(
        f"{method} returned without reaching ClaudeAPI._post")


def _dedup_survivors(engine, worker_dir: Path, records: list) -> list:
    """Mirror ``dedup_worker`` without deleting or renaming source files."""
    direct_root = Path(worker_dir).resolve()
    buckets: Dict[Tuple[str, str, str], list] = {}
    untouched = []
    for record in records:
        path = Path(record.get("path") or "")
        if not path.is_file():
            continue
        digest = _file_hash(engine, path)
        try:
            direct = path.parent.resolve() == direct_root
        except Exception:
            direct = False
        if not direct:
            untouched.append(record)
            continue
        base = re.sub(r"\s*\(\d+\)\s*$", "", path.stem).strip().casefold()
        buckets.setdefault((base, path.suffix.casefold(), digest), []).append(record)

    module = sys.modules.get(engine.__class__.__module__)
    natural_key = getattr(module, "natural_key", None) if module else None
    if not callable(natural_key):
        state_module = sys.modules.get(_state(engine).__class__.__module__)
        natural_key = (getattr(state_module, "natural_key", None)
                       if state_module else None)
    if not callable(natural_key):
        natural_key = lambda value: [str(value).casefold()]

    kept = list(untouched)
    for members in buckets.values():
        members.sort(key=lambda row: (
            len(Path(row["path"]).name), natural_key(Path(row["path"]).name)))
        kept.append(members[0])
    # _records_from_manifest is naturally sorted. Restore that deterministic
    # order after the grouping above without needing Stage 2's natural_key.
    kept.sort(key=lambda row: str(Path(row["path"])).casefold())
    return kept


def _candidate_operations(engine, worker_dir: Path, records: list) -> list:
    records = _dedup_survivors(engine, worker_dir, records)
    groups: Dict[str, list] = {}
    for record in records:
        groups.setdefault(str(record.get("name") or ""), []).append(record)

    family_state = {}
    worker = ((_state(engine).data.get("workers") or {}).get(
        _worker_key(worker_dir)) or {})
    family_state = worker.get("ranking_families") or {}
    out, seen = [], set()

    def add(kind: str, record: dict, method: str, signature: bool = False):
        path = Path(record["path"])
        digest = _file_hash(engine, path)
        operation = f"{kind}:{digest}"
        if operation in seen:
            return
        prior = _operation_state(engine, worker_dir, operation)
        if prior.get("status") == "complete":
            seen.add(operation)
            return
        if prior.get("status") in ("submission_started", "ambiguous"):
            raise SubmissionAmbiguous(
                f"native operation may already have been submitted: {operation}")
        if prior.get("status") == "failed":
            raise BatchFinishingError(
                f"native failed operation needs a separately authorised retry: {operation}")
        pages = (engine._signature_pages(record, path) if signature
                 else engine._pages_for_review(record, path))
        imgs, text = pages
        args = ((imgs, text, str(record.get("name") or ""))
                if method == "doc_quality" else (imgs, text))
        params = _capture_params(engine.api, method, args)
        payload_hash = _digest(params)
        binding = {
            "worker": _worker_key(worker_dir),
            "path": str(path.resolve()),
            "hash": digest,
            "operation": operation,
            "model": str(engine.api.model_id),
            "payload_hash": payload_hash,
        }
        binding_hash = _digest(binding)
        custom_id = "fin-" + binding_hash[:60]
        out.append({
            "custom_id": custom_id,
            "worker": binding["worker"],
            "worker_name": Path(worker_dir).name,
            "path": binding["path"],
            "hash": digest,
            "family": str(record.get("name") or ""),
            "operation": operation,
            "kind": kind.split(":", 1)[0],
            "method": method,
            "model": binding["model"],
            "payload_hash": payload_hash,
            "binding_hash": binding_hash,
            "request": {"custom_id": custom_id, "params": params},
            "prior": copy.deepcopy(prior) if prior else None,
            "status": "prepared",
        })
        seen.add(operation)

    dated = {
        "Certificate of Sponsorship": ("cos-date", "cos_issue_date"),
        "Share Code Check Result": ("share-code-date", "share_code_check"),
    }
    for base_name, family in groups.items():
        if not base_name or base_name == "Other" or base_name.casefold().startswith(
                "other"):
            continue
        if (family_state.get(base_name) or {}).get("status") == "complete":
            continue
        if base_name in dated:
            kind, method = dated[base_name]
            for record in family:
                add(kind, record, method)
        if len(family) < 2:
            continue
        for record in family:
            add(f"quality:{base_name}", record, "doc_quality")
            if base_name == "Employment Contract":
                add("contract-signed", record, "contract_signed", signature=True)
    return out


def _make_chunks(operations: list, target_bytes: int) -> list:
    if target_bytes <= _wire_size([]):
        raise BatchFinishingError("chunk byte limit is too small")
    chunks, current = [], []
    for operation in operations:
        request = operation["request"]
        if _wire_size([request]) > target_bytes:
            raise BatchFinishingError(
                f"one finishing request exceeds the {target_bytes}-byte limit: "
                f"{operation['operation']}")
        proposed = current + [request]
        if current and (_wire_size(proposed) > target_bytes
                        or len(proposed) > MAX_REQUESTS_PER_CHUNK):
            chunks.append(current)
            current = [request]
        else:
            current = proposed
    if current:
        chunks.append(current)
    return [{
        "chunk_id": f"finishing-{index:04d}",
        "custom_ids": [row["custom_id"] for row in requests],
        "wire_bytes": _wire_size(requests),
        "status": "prepared",
    } for index, requests in enumerate(chunks, 1)]


def prepare(engine, worker_dirs: Iterable[Path], *,
            ledger_path: Optional[Path] = None,
            chunk_target_bytes: int = CHUNK_TARGET_BYTES) -> dict:
    """Prepare exact finishing requests locally and write a durable ledger.

    No API method capable of network I/O is reached.  Existing ledgers are
    never overwritten, because they may be the sole record of an uncertain
    or accepted batch.
    """
    _state(engine)
    target = Path(ledger_path) if ledger_path is not None else default_ledger_path(engine)
    if target.exists():
        raise BatchFinishingError(
            f"refusing to overwrite existing finishing ledger: {target}")
    workers = [Path(item) for item in worker_dirs]
    if not workers:
        raise BatchFinishingError("at least one worker directory is required")

    operations = []
    seen_workers = set()
    for worker_dir in workers:
        key = _worker_key(worker_dir)
        if key in seen_workers:
            continue
        seen_workers.add(key)
        if not worker_dir.is_dir() or worker_dir.is_symlink():
            raise BatchFinishingError(
                f"worker is missing or is not a regular directory: {worker_dir}")
        records = engine._records_from_manifest(worker_dir)
        # This is the same full-coverage gate used immediately before native
        # finishing. It prevents paying for a subset when one current worker
        # document is not represented by an applied, hash-bound record.
        engine._validate_batch_worker_records(
            worker_dir, records, _state(engine))
        operations.extend(_candidate_operations(engine, worker_dir, records))

    ids = [row["custom_id"] for row in operations]
    if len(ids) != len(set(ids)):
        raise BatchFinishingError("prepared custom IDs are not unique")
    chunks = _make_chunks(operations, int(chunk_target_bytes))
    preparation_id = _digest({
        "model": str(engine.api.model_id),
        "workers": sorted(seen_workers),
        "bindings": [row["binding_hash"] for row in operations],
    })[:32]
    ledger = {
        "version": LEDGER_VERSION,
        "kind": "stage2-finishing-message-batch",
        "preparation_id": preparation_id,
        "prepared_ts": _now(),
        "model_id": str(engine.api.model_id),
        "chunk_target_bytes": int(chunk_target_bytes),
        "workers": sorted(seen_workers),
        "operations": {row["custom_id"]: row for row in operations},
        "chunks": chunks,
        "status": "prepared" if chunks else "nothing_to_submit",
    }
    _save_ledger(engine, target, ledger)
    return {
        "status": ledger["status"],
        "preparation_id": preparation_id,
        "ledger_path": str(target),
        "operations": len(operations),
        "chunks": len(chunks),
        "wire_bytes": sum(chunk["wire_bytes"] for chunk in chunks),
    }


def _validate_operation_binding(engine, operation: dict) -> None:
    required = ("custom_id", "worker", "path", "hash", "operation", "model",
                "payload_hash", "binding_hash", "request")
    if any(not operation.get(key) for key in required):
        raise BatchFinishingError("a prepared operation binding is incomplete")
    request = operation["request"]
    if request.get("custom_id") != operation["custom_id"]:
        raise BatchFinishingError("prepared request custom ID changed")
    if _digest(request.get("params")) != operation["payload_hash"]:
        raise BatchFinishingError("prepared request payload changed")
    binding = {
        "worker": operation["worker"], "path": operation["path"],
        "hash": operation["hash"], "operation": operation["operation"],
        "model": operation["model"], "payload_hash": operation["payload_hash"],
    }
    if _digest(binding) != operation["binding_hash"]:
        raise BatchFinishingError("prepared request binding changed")
    if request.get("params", {}).get("model") != operation["model"]:
        raise BatchFinishingError("prepared payload/model binding changed")
    path = Path(operation["path"])
    if (not path.is_file() or path.is_symlink()
            or _file_hash(engine, path) != operation["hash"]):
        raise BatchFinishingError(
            f"finishing evidence changed or disappeared: {path}")
    if _worker_key(path.parent) != operation["worker"]:
        # The current bridge is intentionally scoped to loose, not-yet-filed
        # worker documents. A moved path must be reconciled manually.
        raise BatchFinishingError("finishing evidence is no longer in its bound worker")


def submit(engine, *, ledger_path: Optional[Path] = None,
           preparation_id: str) -> dict:
    """Explicitly submit newly prepared chunks; never retry an uncertain POST."""
    target, ledger = _load_ledger(engine, ledger_path)
    if not preparation_id or preparation_id != ledger.get("preparation_id"):
        raise BatchFinishingError("the explicit preparation ID does not match")
    if str(engine.api.model_id) != ledger.get("model_id"):
        raise BatchFinishingError("the current API model differs from the prepared model")
    uncertain = [chunk["chunk_id"] for chunk in ledger["chunks"]
                 if chunk.get("status") in ("submission_started", "ambiguous")]
    if uncertain:
        raise SubmissionAmbiguous(
            "refusing to submit while a prior chunk is uncertain: "
            + ", ".join(uncertain))

    submitted = []
    for chunk in ledger["chunks"]:
        if chunk.get("status") != "prepared":
            continue
        requests = []
        for custom_id in chunk.get("custom_ids") or []:
            operation = ledger["operations"].get(custom_id)
            if operation is None:
                raise BatchFinishingError("chunk references an unknown operation")
            _validate_operation_binding(engine, operation)
            requests.append(operation["request"])
        if not requests or _wire_size(requests) != int(chunk.get("wire_bytes", -1)):
            raise BatchFinishingError("prepared chunk bytes or membership changed")
        if _wire_size(requests) > int(ledger.get("chunk_target_bytes", 0)):
            raise BatchFinishingError("prepared chunk exceeds its saved byte limit")

        chunk.update({
            "status": "submission_started",
            "attempt_id": hashlib.sha256(
                f"{_now()}:{chunk['chunk_id']}:{ledger['preparation_id']}".encode()
            ).hexdigest()[:24],
            "submission_started_ts": _now(),
        })
        ledger["status"] = "submitting"
        _save_ledger(engine, target, ledger)
        try:
            created = engine.api.submit_batch(copy.deepcopy(requests))
            batch_id = str((created or {}).get("id") or "")
            if not batch_id:
                raise BatchFinishingError("provider returned no batch ID")
        except BaseException:
            chunk["status"] = "ambiguous"
            chunk["ambiguous_ts"] = _now()
            try:
                _save_ledger(engine, target, ledger)
            finally:
                raise
        chunk.update({
            "status": "accepted", "batch_id": batch_id,
            "accepted_ts": _now(),
            "provider_status_at_submit": str(
                (created or {}).get("processing_status") or ""),
        })
        try:
            _save_ledger(engine, target, ledger)
        except BaseException as exc:
            # The durable on-disk marker remains submission_started. A caller
            # must reconcile it; repeating the POST is forbidden.
            raise SubmissionAmbiguous(
                f"batch {batch_id} was accepted but its ID could not be saved") from exc
        submitted.append(batch_id)

    remaining = sum(1 for row in ledger["chunks"] if row.get("status") == "prepared")
    ledger["status"] = "submitted" if not remaining else "partially_submitted"
    _save_ledger(engine, target, ledger)
    return {"status": ledger["status"], "batch_ids": submitted,
            "submitted_chunks": len(submitted), "remaining_chunks": remaining,
            "ledger_path": str(target)}


def _raw_text(result: dict) -> str:
    message = result.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), list):
        raise ResultValidationError("succeeded result has no message content")
    text = "\n".join(str(block.get("text", ""))
                     for block in message["content"]
                     if isinstance(block, dict) and block.get("type") == "text").strip()
    if not text:
        raise ResultValidationError("succeeded result has no text answer")
    return text


def _strict_result_shape(api, operation: dict, raw: str) -> None:
    parsed = api._json_from(raw)
    if not isinstance(parsed, dict):
        raise ResultValidationError("answer is not a JSON object")
    method = operation["method"]
    if method == "cos_issue_date":
        valid = isinstance(parsed.get("issue_date"), str)
    elif method == "share_code_check":
        valid = (isinstance(parsed.get("check_date"), str)
                 and type(parsed.get("work_permitted")) is bool)
    elif method == "contract_signed":
        valid = type(parsed.get("signed")) is bool
    elif method == "doc_quality":
        score = parsed.get("score")
        try:
            numeric = (not isinstance(score, bool)
                       and math.isfinite(float(score)))
        except (TypeError, ValueError):
            numeric = False
        valid = (numeric and type(parsed.get("legible")) is bool
                 and type(parsed.get("complete")) is bool
                 and isinstance(parsed.get("date"), str)
                 and isinstance(parsed.get("note"), str))
    else:
        valid = False
    if not valid:
        raise ResultValidationError(
            f"malformed {method} answer for {operation['custom_id']}")


def _replay_answer(api, operation: dict, raw: str):
    """Parse through the original API helper with `_post` replaying raw text."""
    _strict_result_shape(api, operation, raw)
    clone = copy.copy(api)

    def replay(_self, _system, _blocks, max_tokens=400, cache_system=False):
        return raw

    clone._post = MethodType(replay, clone)
    method = operation["method"]
    if method == "doc_quality":
        answer = clone.doc_quality([], "", operation["family"])
    else:
        answer = getattr(clone, method)([], "")
    # Result records are JSON, and a non-serialisable value would make the
    # native state impossible to save safely.
    _canonical(answer)
    return answer


def _usage_and_cost(engine, result: dict, model_id: str) -> Tuple[dict, float]:
    usage = (result.get("message") or {}).get("usage") or {}
    if not isinstance(usage, dict):
        raise ResultValidationError("result usage is malformed")
    try:
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        cache_write = int(usage.get("cache_creation_input_tokens", 0) or 0)
        cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise ResultValidationError("result token usage is malformed") from exc
    if min(input_tokens, output_tokens, cache_write, cache_read) < 0:
        raise ResultValidationError("result token usage cannot be negative")
    equivalent_input = input_tokens + int(cache_write * 1.25) + int(cache_read * .10)
    module = sys.modules.get(engine.__class__.__module__)
    cost_fn = getattr(module, "tokens_cost_gbp", None) if module else None
    if not callable(cost_fn):
        state_module = sys.modules.get(_state(engine).__class__.__module__)
        cost_fn = (getattr(state_module, "tokens_cost_gbp", None)
                   if state_module else None)
    if not callable(cost_fn):
        raise BatchFinishingError("Stage 2 batch cost helper is unavailable")
    cost = float(cost_fn(model_id, equivalent_input, output_tokens, batch=True))
    return {
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_write,
        "cache_read_input_tokens": cache_read,
        "equivalent_input_tokens": equivalent_input,
    }, cost


def _validate_remote_chunk(engine, ledger: dict, chunk: dict,
                           remote: dict) -> Dict[str, dict]:
    expected = list(chunk.get("custom_ids") or [])
    if not expected or len(expected) != len(set(expected)):
        raise ResultValidationError("saved chunk identities are incomplete")
    if str(remote.get("id") or chunk["batch_id"]) != chunk["batch_id"]:
        raise ResultValidationError("provider returned the wrong batch identity")
    counts = remote.get("request_counts")
    if not isinstance(counts, dict):
        raise ResultValidationError("ended batch has no request counts")
    try:
        succeeded = int(counts.get("succeeded", -1))
        other = sum(int(counts.get(key, 0) or 0) for key in
                    ("processing", "errored", "canceled", "expired"))
    except (TypeError, ValueError) as exc:
        raise ResultValidationError("batch request counts are malformed") from exc
    if succeeded != len(expected) or other != 0:
        raise ResultValidationError(
            "batch did not succeed exactly once for every expected request")
    results_url = str(remote.get("results_url") or "")
    if not results_url:
        raise ResultValidationError("ended batch has no results URL")
    rows = list(engine.api.batch_results(results_url))
    ids = [row.get("custom_id") if isinstance(row, dict) else None for row in rows]
    if (len(ids) != len(expected) or len(set(ids)) != len(ids)
            or set(ids) != set(expected)):
        raise ResultValidationError(
            "batch results are missing, duplicated, or contain unexpected IDs")
    parsed = {}
    for row in rows:
        result = row.get("result")
        if not isinstance(result, dict) or result.get("type") != "succeeded":
            raise ResultValidationError(
                f"batch result was not succeeded: {row.get('custom_id')}")
        operation = ledger["operations"].get(row["custom_id"])
        if operation is None:
            raise ResultValidationError("result has no prepared operation")
        raw = _raw_text(result)
        answer = _replay_answer(engine.api, operation, raw)
        usage, cost = _usage_and_cost(engine, result, operation["model"])
        parsed[row["custom_id"]] = {
            "answer": answer, "raw_sha256": hashlib.sha256(
                raw.encode("utf-8")).hexdigest(),
            "usage": usage, "cost_gbp": cost,
            "message_id": str((result.get("message") or {}).get("id") or ""),
        }
    return parsed


def _batch_finishing_total(state_data: dict) -> float:
    total = 0.0
    for worker in (state_data.get("workers") or {}).values():
        for operation in (worker.get("finishing_operations") or {}).values():
            if operation.get("status") == "complete" and operation.get("source") == "batch":
                try:
                    total += float(operation.get("cost_gbp", 0) or 0)
                except (TypeError, ValueError):
                    pass
    return round(total, 8)


def _install_chunk(engine, ledger: dict, chunk: dict,
                   parsed: Dict[str, dict]) -> int:
    state = _state(engine)
    # Validate every binding and every native destination before changing one.
    already = set()
    for custom_id in chunk["custom_ids"]:
        operation = ledger["operations"][custom_id]
        _validate_operation_binding(engine, operation)
        worker = (state.data.get("workers") or {}).get(operation["worker"])
        if not isinstance(worker, dict):
            raise BatchFinishingError("bound worker is missing from native batch state")
        prior = (worker.get("finishing_operations") or {}).get(
            operation["operation"]) or {}
        if prior.get("status") == "complete":
            receipt = prior.get("receipt") or {}
            if (prior.get("source") == "batch"
                    and receipt.get("custom_id") == custom_id
                    and receipt.get("batch_id") == chunk["batch_id"]
                    and prior.get("payload_binding_hash") == operation["binding_hash"]
                    and prior.get("result") == parsed[custom_id]["answer"]):
                already.add(custom_id)
                continue
            raise BatchFinishingError(
                f"native operation changed after preparation: {operation['operation']}")
        if prior.get("status") in ("submission_started", "ambiguous"):
            raise SubmissionAmbiguous(
                f"native operation is uncertain: {operation['operation']}")
        if prior and prior.get("status") != "failed":
            raise BatchFinishingError(
                f"native operation has an unsupported state: {operation['operation']}")

    before = copy.deepcopy(state.data)
    completed_ts = _now()
    installed = 0
    try:
        for custom_id in chunk["custom_ids"]:
            if custom_id in already:
                continue
            operation = ledger["operations"][custom_id]
            worker = state.data["workers"][operation["worker"]]
            operations = worker.setdefault("finishing_operations", {})
            prior = operations.get(operation["operation"]) or {}
            receipt_data = parsed[custom_id]
            record = {
                "status": "complete",
                "attempt_id": chunk.get("attempt_id"),
                "started_ts": chunk.get("submission_started_ts"),
                "completed_ts": completed_ts,
                "result": receipt_data["answer"],
                "source": "batch",
                "model_id": operation["model"],
                "input_hash": operation["hash"],
                "payload_hash": operation["payload_hash"],
                "payload_binding_hash": operation["binding_hash"],
                "cost_gbp": round(float(receipt_data["cost_gbp"]), 8),
                "receipt": {
                    "batch_id": chunk["batch_id"],
                    "custom_id": custom_id,
                    "message_id": receipt_data["message_id"],
                    "result_type": "succeeded",
                    "raw_sha256": receipt_data["raw_sha256"],
                    "usage": receipt_data["usage"],
                },
            }
            if prior.get("status") == "failed":
                lineage = list(prior.get("attempts") or [])
                lineage.append({key: value for key, value in prior.items()
                                if key != "attempts"})
                record["attempts"] = lineage
                record["retry_of"] = prior.get("attempt_id")
            operations[operation["operation"]] = record
            installed += 1
        costs = state.data.setdefault("costs", {})
        costs["finishing_batch_actual_gbp"] = _batch_finishing_total(state.data)
        costs["finishing_batch_updated_ts"] = completed_ts
        if not state.save():
            raise BatchFinishingError(
                "batch answers could not be persisted to native state")
    except BaseException:
        state.data = before
        raise
    return installed


def poll(engine, *, ledger_path: Optional[Path] = None) -> dict:
    """Poll only saved batch IDs and install fully validated succeeded answers."""
    target, ledger = _load_ledger(engine, ledger_path)
    if str(engine.api.model_id) != ledger.get("model_id"):
        raise BatchFinishingError("the current API model differs from the prepared model")
    uncertain = [row["chunk_id"] for row in ledger["chunks"]
                 if row.get("status") in ("submission_started", "ambiguous")]
    if uncertain:
        raise SubmissionAmbiguous(
            "poll cannot infer IDs for uncertain submissions: " + ", ".join(uncertain))

    installed = 0
    waiting = 0
    for chunk in ledger["chunks"]:
        if chunk.get("status") == "installed":
            continue
        if chunk.get("status") == "prepared":
            continue
        if chunk.get("status") not in ("accepted", "ended_validated"):
            raise BatchFinishingError(
                f"unsupported chunk state: {chunk.get('status')}")
        remote = engine.api.get_batch(chunk["batch_id"])
        if not isinstance(remote, dict):
            raise ResultValidationError("provider batch status is malformed")
        processing = str(remote.get("processing_status") or "")
        chunk["last_polled_ts"] = _now()
        chunk["provider_status"] = processing
        chunk["request_counts"] = copy.deepcopy(remote.get("request_counts") or {})
        if processing != "ended":
            waiting += 1
            _save_ledger(engine, target, ledger)
            continue
        try:
            parsed = _validate_remote_chunk(engine, ledger, chunk, remote)
            chunk["status"] = "ended_validated"
            _save_ledger(engine, target, ledger)
            installed += _install_chunk(engine, ledger, chunk, parsed)
        except BaseException as exc:
            chunk["validation_error"] = f"{type(exc).__name__}: {exc}"
            chunk["validation_failed_ts"] = _now()
            _save_ledger(engine, target, ledger)
            raise
        chunk["status"] = "installed"
        chunk["installed_ts"] = _now()
        for custom_id in chunk["custom_ids"]:
            ledger["operations"][custom_id]["status"] = "installed"
            ledger["operations"][custom_id]["batch_id"] = chunk["batch_id"]
        _save_ledger(engine, target, ledger)

    remaining_prepared = sum(1 for row in ledger["chunks"]
                             if row.get("status") == "prepared")
    remaining_accepted = sum(1 for row in ledger["chunks"]
                             if row.get("status") in ("accepted", "ended_validated"))
    if not remaining_prepared and not remaining_accepted and not waiting:
        ledger["status"] = "installed"
    elif waiting or remaining_accepted:
        ledger["status"] = "waiting"
    else:
        ledger["status"] = "prepared"
    _save_ledger(engine, target, ledger)
    return {
        "status": ledger["status"], "installed_operations": installed,
        "waiting_chunks": remaining_accepted,
        "prepared_chunks": remaining_prepared,
        "finishing_batch_actual_gbp": float(
            (_state(engine).data.get("costs") or {}).get(
                "finishing_batch_actual_gbp", 0) or 0),
        "ledger_path": str(target),
    }


def inspect(engine, *, ledger_path: Optional[Path] = None) -> dict:
    """Return a compact, read-only ledger summary."""
    target, ledger = _load_ledger(engine, ledger_path)
    counts: Dict[str, int] = {}
    for chunk in ledger["chunks"]:
        status = str(chunk.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {
        "status": ledger.get("status"),
        "preparation_id": ledger.get("preparation_id"),
        "model_id": ledger.get("model_id"),
        "operations": len(ledger["operations"]),
        "chunks": counts,
        "ledger_path": str(target),
    }


__all__ = [
    "BatchFinishingError", "SubmissionAmbiguous", "ResultValidationError",
    "prepare", "submit", "poll", "inspect", "default_ledger_path",
]
