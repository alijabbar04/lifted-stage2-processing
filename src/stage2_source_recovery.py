"""Local source inspection, durable resolution and exact supplemental scopes.

No provider or GUI dependency. Passwords are arguments to authenticate only;
only allow-listed outcomes enter checkpoints. Call mutations under writer_lock.
"""
from __future__ import annotations

import copy
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid
import zipfile
import xml.etree.ElementTree as ET

import fitz
from PIL import Image, ImageStat

CAUSES = frozenset({"password_protected_pdf", "corrupt_or_truncated_pdf",
    "zero_page_pdf", "no_renderable_or_extractable_content", "unsupported_format",
    "source_missing", "source_changed", "unreadable_source", "file_size_limit"})
READY = {"ready", "unlocked_validated"}
PURCHASED = {"supplemental_submission_started", "submitted", "accepted_applied"}
ACCEPTED = {"submitted", "accepted_applied"}
TERMINAL = {"accepted_applied", "excluded_quarantined"}
LOCKED = {"locked_waiting_for_password", "still_locked"}


class RecoveryError(RuntimeError):
    pass


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def digest(path):
    with Path(path).open("rb") as stream:
        h = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def json_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".checkpoint-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def safe_path(path, root):
    """Reject redirected staging/source paths, including Windows junctions."""
    path, root = Path(path).absolute(), Path(root).absolute()
    if not path.resolve().is_relative_to(root.resolve()):
        raise RecoveryError("Recovery path is outside the saved scope.")
    current = path
    while current != root and current != current.parent:
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise RecoveryError("Recovery paths cannot use symbolic links or junctions.")
        current = current.parent
    if current != root:
        raise RecoveryError("Recovery path is outside the saved scope.")
    if root.is_symlink() or getattr(root, "is_junction", lambda: False)():
        raise RecoveryError("Recovery paths cannot use symbolic links or junctions.")
    return path


def durable_copy(source, target, expected_hash, *, exclusive=False):
    """Publish a fully flushed copy atomically; never expose a partial target."""
    source, target = Path(source), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=target.parent, prefix=".recovery-copy-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as out, source.open("rb") as inp:
            shutil.copyfileobj(inp, out)
            out.flush()
            os.fsync(out.fileno())
        if digest(temp) != expected_hash:
            raise RecoveryError("Recovery copy changed while being staged.")
        if exclusive:
            # A same-directory hard link installs the complete file without
            # overwriting anything created concurrently at the target.
            os.link(temp, target)
        else:
            os.replace(temp, target)
    finally:
        Path(temp).unlink(missing_ok=True)


def inspect_source(path, expected_hash=None, *, max_size_mb=0):
    """Inspect all PDF pages locally. Encryption is checked before page access.

    A uniform page image and empty text are not evidence. Rendering errors on
    any page fail the document, even if earlier pages were readable.
    """
    path = Path(path)
    result = {"path": str(path), "size_bytes": None, "hash": "",
        "type": path.suffix.lower(), "cause": "", "pages": None,
        "render_viable": False, "text_viable": False, "inspected_ts": now(),
        "structural_validation": "not_completed", "signatures": False}
    if not path.is_file() or path.is_symlink():
        result["cause"] = "source_missing"
        return result
    try:
        result["size_bytes"] = path.stat().st_size
        result["hash"] = digest(path)
    except OSError:
        result["cause"] = "source_missing"
        return result
    if expected_hash and result["hash"] != expected_hash:
        result["cause"] = "source_changed"
        return result
    if max_size_mb > 0 and result["size_bytes"] > max_size_mb * 1024 * 1024:
        result["cause"] = "file_size_limit"
        return result
    if path.suffix.lower() == ".pdf":
        try:
            with fitz.open(path) as doc:
                if doc.needs_pass:
                    result.update(cause="password_protected_pdf",
                        structural_validation="requires_decryption")
                    return result
                result["pages"] = len(doc)
                if not len(doc):
                    result["cause"] = "zero_page_pdf"
                    return result
                if doc.is_repaired:
                    result["cause"] = "corrupt_or_truncated_pdf"
                    return result
                # Explicit EOF check catches truncated files tolerated by MuPDF.
                with path.open("rb") as stream:
                    stream.seek(max(0, result["size_bytes"] - 2048))
                    if b"%%EOF" not in stream.read():
                        result["cause"] = "corrupt_or_truncated_pdf"
                        return result
                for page in doc:
                    if page.get_text().strip():
                        result["text_viable"] = True
                    pix = page.get_pixmap(matrix=fitz.Matrix(.5, .5),
                                          colorspace=fitz.csGRAY, alpha=False)
                    image = Image.frombytes("L", (pix.width, pix.height), pix.samples)
                    if ImageStat.Stat(image).extrema[0][0] != ImageStat.Stat(image).extrema[0][1]:
                        result["render_viable"] = True
                    for widget in page.widgets() or ():
                        if widget.field_type == fitz.PDF_WIDGET_TYPE_SIGNATURE:
                            result["signatures"] = True
                result["structural_validation"] = "complete"
        except Exception:
            result["cause"] = "corrupt_or_truncated_pdf"
    elif path.suffix.lower() in {".png", ".jpg", ".jpeg", ".tif", ".tiff",
                                ".bmp", ".webp"}:
        try:
            with Image.open(path) as image:
                result["pages"] = getattr(image, "n_frames", 1)
                for i in range(result["pages"]):
                    image.seek(i)
                    image.load()
                    extrema = image.convert("L").getextrema()
                    result["render_viable"] |= extrema[0] != extrema[1]
            result["structural_validation"] = "complete"
        except Exception:
            result["cause"] = "no_renderable_or_extractable_content"
    elif path.suffix.lower() == ".txt":
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as stream:
                result["text_viable"] = any(block.strip() for block in iter(lambda: stream.read(65536), ""))
            result.update(pages=1, structural_validation="complete")
        except OSError:
            result["cause"] = "no_renderable_or_extractable_content"
    elif path.suffix.lower() in {".docx", ".doc"}:
        # The application extracts OOXML text directly, including OOXML files
        # with a legacy .doc extension. Binary Word files need conversion.
        if not zipfile.is_zipfile(path):
            result["cause"] = "unsupported_format"
        else:
            try:
                with zipfile.ZipFile(path) as archive:
                    document = archive.getinfo("word/document.xml")
                    if document.file_size > 64 * 1024 * 1024 or archive.testzip():
                        raise ValueError("Invalid document container")
                    xml = ET.fromstring(archive.read(document))
                    result["text_viable"] = any(text.strip() for text in xml.itertext())
                result.update(pages=1, structural_validation="complete")
            except Exception:
                result["cause"] = "no_renderable_or_extractable_content"
    else:
        result["cause"] = "unsupported_format"
    if not result["cause"] and not (result["render_viable"] or result["text_viable"]):
        result["cause"] = "no_renderable_or_extractable_content"
    if not result["cause"]:
        try:
            if digest(path) != result["hash"]:
                result["cause"] = "source_changed"
        except OSError:
            result["cause"] = "source_missing"
    return result


def restrict_directory(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RecoveryError("Recovery staging cannot be a symbolic link.")
    if os.name == "nt":
        # Credentials never enter a child process. This call only establishes
        # the current Windows user's private staging ACL before writing files.
        import getpass
        completed = subprocess.run(["icacls", str(path), "/inheritance:r",
            "/grant:r", getpass.getuser() + ":(OI)(CI)F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if completed.returncode:
            raise RecoveryError("Private recovery staging permissions could not be set.")
    else:
        path.chmod(0o700)


class Recovery:
    """Single-writer state companion; every save is reopened and compared."""

    def __init__(self, state, version, build):
        self.state = state
        self.version, self.build = version, build
        if int(state.data.get("version", 5)) > 5:
            raise RecoveryError("This batch uses a newer unsupported state version.")
        self.root = state.dir.resolve() / ".stage2-source-recovery"
        safe_path(self.root, state.dir.resolve())
        self.expected = copy.deepcopy(state.data)
        self.data = state.data.get("source_recovery")
        if self.data is not None:
            if not isinstance(self.data, dict) or self.data.get("schema") != 1:
                raise RecoveryError("Unsupported source-recovery schema.")
            self.verify_ledger()

    def initialize(self):
        safe_path(self.root, self.state.dir.resolve())
        if self.data is None:
            if self.state.path.is_file():
                self.state.recovery_snapshot()
            restrict_directory(self.root)
            self.data = {"schema": 1, "run_id": uuid.uuid4().hex,
                "records": {}, "ledger": [], "scopes": [],
                "preflight_complete": False, "scope_outcome": "full_scope"}
            self.state.data["source_recovery"] = self.data
            self.checkpoint()
        return self

    def checkpoint(self):
        # Do not rely on bool/None returns from legacy or wrapped save methods.
        if self.state.path.is_file():
            try:
                saved = json.loads(self.state.path.read_text(encoding="utf-8"))
            except Exception:
                raise RecoveryError("Saved recovery checkpoint is unreadable.") from None
            if saved != self.expected:
                raise RecoveryError("Batch state changed; refresh the recovery screen.")
        self.state.save()
        try:
            saved = json.loads(self.state.path.read_text(encoding="utf-8"))
        except Exception:
            raise RecoveryError("Recovery checkpoint could not be verified.") from None
        if saved != self.state.data:
            raise RecoveryError("Recovery checkpoint did not persist; restart recovery.")
        self.expected = copy.deepcopy(saved)

    def verify_ledger(self):
        previous = ""
        for entry in self.data.get("ledger", []):
            unsigned = {k: v for k, v in entry.items() if k != "entry_hash"}
            if unsigned.get("previous_hash") != previous or json_hash(unsigned) != entry.get("entry_hash"):
                raise RecoveryError("Resolution ledger integrity check failed.")
            previous = entry["entry_hash"]

    def event(self, cid, action, **metadata):
        record = self.data["records"].get(cid, {})
        # Only known non-content fields are permitted. Never accept exception
        # text, freeform password hints, or arbitrary caller-supplied payloads.
        allowed = {"new_hash", "archive_path", "archive_hash", "working_path",
                   "confirmation", "scope_id", "request_id", "status"}
        if set(metadata) - allowed:
            raise RecoveryError("Unsupported resolution metadata.")
        entry = {"source_id": cid, "worker": record.get("worker", ""),
            "original_path": record.get("original_path", ""),
            "original_hash": record.get("original_hash", ""),
            "cause": record.get("detected_cause", ""), "resolution": action,
            "timestamp": now(), "app_version": self.version, "app_build": self.build,
            "previous_hash": (self.data["ledger"][-1]["entry_hash"]
                              if self.data["ledger"] else ""), **metadata}
        entry["entry_hash"] = json_hash(entry)
        self.data["ledger"].append(entry)

    def token(self, ids):
        return json_hash({"run": self.data["run_id"], "ids": sorted(ids),
            "state": self.state.data})

    def assert_decision(self, ids, token):
        if not ids or len(ids) != len(set(ids)) or any(i not in self.data["records"] for i in ids):
            raise RecoveryError("Select an exact source scope.")
        if self.token(ids) != token:
            raise RecoveryError("The source decision is stale; refresh and confirm again.")
        for cid in ids:
            self.assert_mutable(cid)

    def assert_mutable(self, cid):
        record = self.data["records"][cid]
        worker = (self.state.data.get("workers") or {}).get(
            str(Path(record["worker_dir"]).resolve()).casefold(), {})
        if worker.get("completed"):
            raise RecoveryError("Completed workers cannot be changed during recovery.")
        if record["state"] in PURCHASED or cid in self.state.data.get("requests", {}):
            raise RecoveryError("Accepted or uncertain source requests cannot be changed.")
        for scope in self.data["scopes"]:
            if (cid in scope["ids"] or cid in scope.get("aliases", {})) and scope["status"] in {"submission_started", "ambiguous", "accepted"}:
                raise RecoveryError("This source already belongs to a paid or uncertain scope.")

    def bound_path(self, record, *, allow_missing=False):
        path = safe_path(record["path"], self.state.dir.resolve())
        if not path.is_file():
            if allow_missing:
                return path
            raise RecoveryError("Source missing; re-inspect before continuing.")
        if digest(path) != record["hash"]:
            raise RecoveryError("Source changed since the saved decision.")
        return path

    def preflight(self, render, stop=lambda: None, progress=lambda *_: None):
        inventory = self.state.data.get("primary_inventory")
        if not isinstance(inventory, dict) or not inventory:
            raise RecoveryError("The saved primary inventory is missing or empty. Use primary submission reconciliation to reconstruct the verified scope before source recovery.")
        self.initialize()
        self.data["preflight_complete"] = False
        self.checkpoint()
        for index, (cid, meta) in enumerate(inventory.items(), 1):
            stop()
            progress(index - 1, len(inventory))
            existing = self.data["records"].get(cid)
            if cid in self.state.data.get("requests", {}):
                # Legacy accepted work may have been renamed or moved. Its
                # immutable request/worker markers, not old paths, are authority.
                continue
            if existing and existing["state"] in PURCHASED | {"excluded_quarantined"}:
                continue
            if existing and existing["state"] in READY and self.valid_prepared(existing):
                continue
            inspected = inspect_source(meta["path"], meta.get("fhash"),
                max_size_mb=float((self.state.data.get("settings") or {}).get("max_file_mb", 0) or 0))
            if not meta.get("fhash") and not inspected["cause"]:
                # An inventory hash failure is not permission to silently bind
                # a later file. Explicit replacement can acknowledge this copy.
                inspected["cause"] = "source_changed"
            record = existing or {"source_id": cid, "worker": meta["worker"],
                "worker_dir": meta["worker_dir"], "original_path": meta["path"],
                "original_hash": meta.get("fhash", ""), "detected_cause": inspected["cause"]}
            record.update(inspected)
            record["observed_hash"] = inspected["hash"]
            # Keep expected hash authority if the source has drifted.
            record["hash"] = meta.get("fhash") or inspected["hash"]
            record["state"] = ("locked_waiting_for_password" if inspected["cause"] == "password_protected_pdf"
                               else "needs_attention" if inspected["cause"] else "inspected")
            self.data["records"][cid] = record
            self.event(cid, "inspected", status=record["state"])
            self.checkpoint()
            if not record["cause"]:
                self.prepare(cid, render)
            else:
                self.sync_exception(cid)
                self.checkpoint()
        self.data["preflight_complete"] = True
        self.data["preflight_ts"] = now()
        self.state.data["phase"] = "source_preflight_ready" if not self.unresolved() else "waiting_source_recovery"
        self.checkpoint()
        progress(len(inventory), len(inventory))
        return self.summary()

    def sync_exception(self, cid):
        r = self.data["records"][cid]
        meta = self.state.data["primary_inventory"][cid]
        if r["state"] in READY | PURCHASED:
            self.state.data.setdefault("primary_render_exclusions", {}).pop(cid, None)
        else:
            self.state.data.setdefault("primary_render_exclusions", {})[cid] = {
                "schema": "stage2-primary-source-exception/v1", "custom_id": cid,
                **{k: meta.get(k, "") for k in ("path", "worker", "worker_dir", "fhash")},
                "size_bytes": r.get("size_bytes"), "reason": r.get("cause") or "unreadable_source",
                "detail": "Local inspection requires source recovery.",
                "provider_request_built": False, "worker_completion_allowed": False,
                "recorded_ts": now()}

    def prepare(self, cid, render):
        r = self.data["records"][cid]
        path = self.bound_path(r)
        limit = float((self.state.data.get("settings") or {}).get("max_file_mb", 0) or 0)
        if limit > 0 and path.stat().st_size > limit * 1024 * 1024:
            r.update(state="needs_attention", cause="file_size_limit")
            self.sync_exception(cid)
            self.checkpoint()
            return
        try:
            images, text, indices, pages, segment = render(path)
        except Exception:
            images, text, indices, pages, segment = [], "", [], 0, False
        if not images and not text.strip():
            r.update(state="needs_attention", cause="no_renderable_or_extractable_content")
            self.sync_exception(cid)
            self.checkpoint()
            return
        self.bound_path(r)
        target = safe_path(self.root / self.data["run_id"] / (cid + ".evidence.json"), self.root)
        atomic_json(target, {"images": images, "text": text, "indices": indices,
                            "pages": pages, "segment": segment, "source_hash": r["hash"]})
        r.update(evidence_path=str(target), evidence_hash=digest(target),
                 state="unlocked_validated" if r.get("working_path") else "ready", cause="")
        self.sync_exception(cid)
        self.event(cid, "evidence_prepared", status=r["state"])
        self.checkpoint()

    def valid_prepared(self, r):
        try:
            self.bound_path(r)
            p = safe_path(r["evidence_path"], self.root)
            return digest(p) == r["evidence_hash"]
        except (KeyError, OSError, RecoveryError):
            return False

    def evidence(self, cid):
        r = self.data["records"][cid]
        if r["state"] not in READY or not self.valid_prepared(r):
            raise RecoveryError("Prepared evidence or source changed; inspect locally again.")
        payload = json.loads(Path(r["evidence_path"]).read_text(encoding="utf-8"))
        if payload.get("source_hash") != r["hash"]:
            raise RecoveryError("Prepared evidence is not source-bound.")
        return payload

    def unresolved(self):
        return [cid for cid, r in self.data["records"].items()
                if r["state"] not in READY | ACCEPTED | {"excluded_quarantined"}]

    def summary(self):
        rows = list(self.data["records"].values()) if self.data else []
        costs = self.state.data.get("costs") or {}
        return {"total": len(rows), "ready": sum(r["state"] in READY for r in rows),
            "locked": sum(r["state"] in LOCKED for r in rows),
            "unlocked": sum(r["state"] == "unlocked_validated" for r in rows),
            "submitted": sum(r["state"] in ACCEPTED for r in rows),
            "ambiguous": sum(r["state"] == "supplemental_submission_started" for r in rows),
            "excluded": sum(r["state"] == "excluded_quarantined" for r in rows),
            "corrupt_after_decryption": sum(r["state"] == "decrypted_but_unreadable" for r in rows),
            "unresolved": len(self.unresolved()) if self.data else 0,
            "workers": len({r["worker"] for r in rows}),
            "completed_workers": sum(w.get("completed") is True for w in self.state.data.get("workers", {}).values()),
            "cost_incurred_gbp": sum(float(costs.get(k, 0) or 0) for k in
                ("primary_actual_gbp", "followup_actual_gbp", "live_actual_gbp")),
            "scope_outcome": self.data.get("scope_outcome", "full_scope") if self.data else "full_scope"}

    def archive(self, cid):
        r = self.data["records"][cid]
        source = self.bound_path(r)
        # Retain every historical source version; a second resolution must
        # neither overwrite the original nor collide with its archive.
        target = safe_path(self.root / self.data["run_id"] /
            (cid + "." + r["hash"] + ".original" + source.suffix), self.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            durable_copy(source, target, r["hash"], exclusive=True)
        if digest(target) != r["hash"]:
            raise RecoveryError("Original archive failed verification; source retained.")
        self.bound_path(r)
        historical = {"path": str(target), "hash": r["hash"]}
        if historical not in r.setdefault("archives", []):
            r["archives"].append(historical)
        return target

    def commit_working_copy(self, cid, candidate, inspection, render, confirmation):
        r = self.data["records"][cid]
        self.assert_mutable(cid)
        source = self.bound_path(r, allow_missing=r.get("cause") == "source_missing")
        missing = not source.exists()
        archive = None if missing else self.archive(cid)
        # Persist intent before replacing the active path. Restart can commit
        # this exact copy or leave the original and ask for local retry.
        r["pending_resolution"] = {"candidate": str(candidate), "new_hash": inspection["hash"],
            "old_hash": r["hash"], "archive_path": str(archive) if archive else "",
            "original_missing": missing, "confirmation": confirmation}
        self.event(cid, "replacement_prepared", new_hash=inspection["hash"],
                   archive_path=str(archive) if archive else "",
                   archive_hash=r["hash"] if archive else "",
                   status="original_unavailable" if missing else "original_preserved",
                   confirmation=confirmation)
        self.checkpoint()
        self.activate_pending(cid, render)

    def activate_pending(self, cid, render):
        r = self.data["records"][cid]
        pending = r.get("pending_resolution")
        if not pending:
            return
        source = safe_path(r["path"], self.state.dir.resolve())
        candidate = safe_path(pending["candidate"], self.root)
        archive = safe_path(pending["archive_path"], self.root) if pending["archive_path"] else None
        if ((archive is not None and digest(archive) != pending["old_hash"])
                or (archive is None and not pending.get("original_missing"))
                or digest(candidate) != pending["new_hash"]):
            raise RecoveryError("Resolution archive or staging copy changed.")
        current = digest(source) if source.is_file() and not source.is_symlink() else ""
        expected_old = "" if pending.get("original_missing") else pending["old_hash"]
        if current not in {expected_old, pending["new_hash"]}:
            raise RecoveryError("Source changed after resolution was prepared.")
        if pending.get("original_missing") and current == "":
            durable_copy(candidate, source, pending["new_hash"], exclusive=True)
        elif current == pending["old_hash"]:
            fd, temp = tempfile.mkstemp(dir=source.parent, prefix=".stage2-resolution-", suffix=".tmp")
            try:
                with candidate.open("rb") as inp, os.fdopen(fd, "wb") as out:
                    shutil.copyfileobj(inp, out)
                    out.flush()
                    os.fsync(out.fileno())
                if digest(temp) != pending["new_hash"] or digest(source) != pending["old_hash"]:
                    raise RecoveryError("Source changed while activating replacement.")
                os.replace(temp, source)
            finally:
                Path(temp).unlink(missing_ok=True)
        inspected = inspect_source(source, pending["new_hash"])
        if inspected["cause"]:
            raise RecoveryError("Activated replacement failed local inspection.")
        r.update(inspected)
        r.update(hash=pending["new_hash"], archive_path=str(archive) if archive else "",
            archive_hash=pending["old_hash"] if archive else "",
            working_path=str(candidate), cause="", state="inspected")
        if pending.get("original_missing"):
            r["original_unavailable"] = True
        self.state.data["primary_inventory"][cid]["fhash"] = r["hash"]
        self.state.data["primary_inventory"][cid]["pages"] = inspected["pages"]
        r.pop("pending_resolution")
        self.event(cid, "replacement_activated", new_hash=r["hash"], archive_path=str(archive) if archive else "",
                   archive_hash=r["archive_hash"], confirmation=pending["confirmation"])
        self.checkpoint()
        self.prepare(cid, render)

    def unlock(self, ids, password, token, render, *, acknowledge_signatures=False,
               stop=lambda: None):
        self.assert_decision(ids, token)
        outcome = {"unlocked": 0, "still_locked": 0, "unreadable": 0, "signature_warning": 0}
        try:
            for cid in ids:
                stop()
                r = self.data["records"][cid]
                if r["state"] not in LOCKED:
                    continue
                source = self.bound_path(r)
                r["state"] = "unlocking"
                self.checkpoint()
                candidate = safe_path(self.root / self.data["run_id"] / (uuid.uuid4().hex + ".pdf"), self.root)
                candidate.parent.mkdir(parents=True, exist_ok=True)
                authenticated = False
                try:
                    with fitz.open(source) as doc:
                        if not password or not doc.authenticate(password):
                            r["state"] = "locked_waiting_for_password"
                            outcome["still_locked"] += 1
                        else:
                            authenticated = True
                            signatures = any(w.field_type == fitz.PDF_WIDGET_TYPE_SIGNATURE
                                for p in doc for w in (p.widgets() or ()))
                            if signatures and not acknowledge_signatures:
                                r.update(state="locked_waiting_for_password", signatures=True)
                                outcome["signature_warning"] += 1
                            elif doc.is_repaired:
                                r.update(state="decrypted_but_unreadable", cause="corrupt_or_truncated_pdf")
                                outcome["unreadable"] += 1
                            else:
                                doc.save(candidate, encryption=fitz.PDF_ENCRYPT_NONE)
                    if candidate.exists():
                        with candidate.open("r+b") as staged:
                            os.fsync(staged.fileno())
                        checked = inspect_source(candidate)
                        if checked["cause"]:
                            r.update(state="decrypted_but_unreadable", cause=checked["cause"])
                            outcome["unreadable"] += 1
                        else:
                            self.commit_working_copy(cid, candidate, checked, render, token)
                            outcome["unlocked"] += int(r["state"] == "unlocked_validated")
                except RecoveryError:
                    raise
                except Exception:
                    r.update(state="decrypted_but_unreadable" if authenticated else "locked_waiting_for_password",
                             cause="corrupt_or_truncated_pdf" if authenticated else "password_protected_pdf")
                    outcome["unreadable" if authenticated else "still_locked"] += 1
                self.sync_exception(cid)
                self.event(cid, "unlock_outcome", status=r["state"], confirmation=token)
                self.checkpoint()
        finally:
            password = None
        return outcome

    def replace(self, cid, replacement, token, render):
        self.assert_decision([cid], token)
        replacement = Path(replacement)
        inspected = inspect_source(replacement)
        if inspected["cause"]:
            raise RecoveryError("Replacement failed local inspection: " + inspected["cause"])
        r = self.data["records"][cid]
        if r.get("cause") == "source_changed":
            source = safe_path(r["path"], self.state.dir.resolve())
            if not r.get("observed_hash") or digest(source) != r["observed_hash"]:
                raise RecoveryError("Source changed again; inspect locally and confirm the replacement again.")
            # The decision token binds the newly observed version. Preserve
            # that version while retaining the historical original hash.
            r["hash"] = r["observed_hash"]
            self.event(cid, "changed_source_acknowledged", new_hash=r["hash"], confirmation=token)
        if replacement.suffix.lower() != Path(r["path"]).suffix.lower():
            raise RecoveryError("Replacement must have the same file format as the saved source.")
        candidate = safe_path(self.root / self.data["run_id"] / (uuid.uuid4().hex + replacement.suffix), self.root)
        candidate.parent.mkdir(parents=True, exist_ok=True)
        durable_copy(replacement, candidate, inspected["hash"], exclusive=True)
        if digest(candidate) != inspected["hash"]:
            raise RecoveryError("Replacement changed while copying.")
        self.commit_working_copy(cid, candidate, inspected, render, token)

    def quarantine(self, ids, token):
        self.assert_decision(ids, token)
        # Archive and persist the complete intent before removing any exact source.
        for cid in ids:
            r = self.data["records"][cid]
            archive = self.archive(cid)
            r["quarantine_intent"] = {"archive_path": str(archive), "hash": r["hash"], "confirmation": token}
        self.checkpoint()
        self.finish_quarantines()

    def finish_quarantines(self):
        for cid, r in self.data["records"].items():
            intent = r.get("quarantine_intent")
            if not intent:
                continue
            archive = safe_path(intent["archive_path"], self.root)
            if digest(archive) != intent["hash"]:
                raise RecoveryError("Verified quarantine archive is unavailable.")
            source = self.bound_path(r, allow_missing=True)
            if source.exists():
                source.unlink()
            r.update(state="excluded_quarantined", quarantine_path=str(archive), quarantine_hash=intent["hash"])
            self.data["scope_outcome"] = "partial_scope"
            self.event(cid, "excluded_quarantined", archive_path=str(archive),
                       archive_hash=intent["hash"], confirmation=intent["confirmation"])
            r.pop("quarantine_intent")
            self.sync_exception(cid)
            self.checkpoint()

    def reinstate(self, ids, token):
        self.assert_decision(ids, token)
        for cid in ids:
            r = self.data["records"][cid]
            if r["state"] != "excluded_quarantined":
                raise RecoveryError("Only quarantined sources can be reinstated.")
            source = safe_path(r["path"], self.state.dir.resolve())
            archive = safe_path(r["quarantine_path"], self.root)
            if source.exists() or digest(archive) != r["quarantine_hash"]:
                raise RecoveryError("Reinstatement conflicts with current source or archive.")
            r["reinstate_intent"] = {"archive_path": str(archive),
                "hash": r["quarantine_hash"], "confirmation": token}
        self.checkpoint()
        self.finish_reinstatements()

    def finish_reinstatements(self):
        for cid, r in self.data["records"].items():
            intent = r.get("reinstate_intent")
            if not intent:
                continue
            source = safe_path(r["path"], self.state.dir.resolve())
            archive = safe_path(intent["archive_path"], self.root)
            if digest(archive) != intent["hash"]:
                raise RecoveryError("Verified reinstatement archive is unavailable.")
            if source.exists():
                if not source.is_file() or digest(source) != intent["hash"]:
                    raise RecoveryError("Reinstatement conflicts with current source.")
            else:
                durable_copy(archive, source, intent["hash"], exclusive=True)
            r.update(state="needs_attention", cause=r["detected_cause"])
            r.pop("reinstate_intent")
            self.event(cid, "reinstated", confirmation=intent["confirmation"])
            self.checkpoint()
        if not any(r["state"] == "excluded_quarantined" for r in self.data["records"].values()):
            self.data["scope_outcome"] = "full_scope"
            self.checkpoint()

    def resume(self, render):
        self.initialize()
        verify_excluded_archives(self.state)
        self.finish_quarantines()
        self.finish_reinstatements()
        for cid, r in self.data["records"].items():
            if r.get("pending_resolution"):
                self.activate_pending(cid, render)
            elif r["state"] == "unlocking":
                r["state"] = "locked_waiting_for_password"
                self.checkpoint()
            elif r["state"] in READY and not self.valid_prepared(r):
                inspected = inspect_source(r["path"], r["hash"])
                r.update(state="needs_attention", cause=inspected["cause"] or "source_changed")
                self.sync_exception(cid)
                self.checkpoint()
            elif r["state"] == "excluded_quarantined":
                archive = safe_path(r["quarantine_path"], self.root)
                if digest(archive) != r["quarantine_hash"]:
                    raise RecoveryError("Excluded source archive failed verification.")
        return self.summary()

    def scope(self, ids, token, *, allow_partial=False):
        self.assert_decision(ids, token)
        if not self.data["preflight_complete"]:
            raise RecoveryError("Whole-run local preflight has not completed.")
        if self.unresolved() and not allow_partial:
            raise RecoveryError("Waiting for all sources; no provider request submitted.")
        for cid in ids:
            self.evidence(cid)
        prior_hashes = {pair[1] for s in self.data["scopes"]
            if s["status"] in {"submission_started", "ambiguous", "accepted"}
            for pair in s["bindings"]}
        prior_hashes.update(meta.get("fhash") for meta in
            self.state.data.get("requests", {}).values() if meta.get("fhash"))
        hashes = [self.data["records"][cid]["hash"] for cid in ids]
        if prior_hashes.intersection(hashes) or len(set(hashes)) != len(hashes):
            raise RecoveryError("The selected scope duplicates already purchased or selected evidence.")
        record = {"id": uuid.uuid4().hex, "ids": list(ids), "status": "queued",
            "bindings": [[cid, self.data["records"][cid]["hash"]] for cid in ids],
            "confirmation": token, "allow_partial": bool(allow_partial), "created_ts": now()}
        self.data["scopes"].append(record)
        self.event(ids[0], "supplemental_submission_queued", scope_id=record["id"], confirmation=token)
        self.checkpoint()
        return record

    def validate_current_scope(self):
        """Reject new or changed local sources at each paid boundary.

        Accepted work may have legitimate hash-bound applied paths. Those
        worker receipts, not new directory contents, authorize renamed outputs.
        """
        import re

        inventory = self.state.data.get("primary_inventory") or {}
        workers = self.state.data.get("workers") or {}
        expected_paths = {str(Path(m["path"]).resolve()).casefold(): m.get("fhash")
                          for m in inventory.values()}
        for source in self.state.data.get("submitted_worker_scope", []):
            folder = safe_path(source["source_path"], self.state.dir)
            worker = workers.get(str(folder.resolve()).casefold(), {})
            if worker.get("completed"):
                continue
            applied = {str(Path(r["path"]).resolve()).casefold(): r.get("hash")
                for r in worker.get("applied_records", []) if r.get("path") and r.get("hash")}
            for p in folder.rglob("*") if folder.is_dir() else ():
                # Check redirected directories too, including empty junctions
                # that rglob may not otherwise visit as document files.
                p = safe_path(p, self.state.dir)
                name = p.name.lower()
                # Keep exactly the initial discovery's is_program_file and
                # _is_junk policies, without treating arbitrary dotfiles as junk.
                if (name.startswith("_") or "overwrite_order" in name
                        or name in {"desktop.ini", "thumbs.db", ".ds_store", "ehthumbs.db",
                                    ".dropbox", ".dropbox.attr", "icon\r"}
                        or re.fullmatch(r"\..+\.orientation-[a-z0-9_]{8}\.(pdf|tmp)", name)
                        or name.startswith("~$") or name.endswith(".tmp")):
                    continue
                if not p.is_file() and not p.is_symlink():
                    continue
                key = str(p.resolve()).casefold()
                # A receipt can authorize a transformed/renamed accepted output;
                # otherwise even an original inventory path remains hash-bound.
                # PURCHASED protects prior billing, not changed local bytes.
                expected = applied.get(key) or expected_paths.get(key)
                if not expected or digest(p) != expected:
                    raise RecoveryError("The worker scope contains an unexpected or changed source. No new request was sent; inspect the saved run before continuing.")
        for r in self.data["records"].values():
            if r["state"] in PURCHASED:
                continue
            if r["state"] == "excluded_quarantined":
                if Path(r["path"]).exists():
                    raise RecoveryError("A quarantined source reappeared outside reinstatement; no request sent.")
                continue
            self.bound_path(r, allow_missing=r.get("cause") == "source_missing")

    def export(self, path):
        # Complete metadata issue list, never prepared evidence/text/passwords.
        value = {"summary": self.summary(), "records": self.data["records"],
                 "source_exclusions": exclusion_summary(self.state),
                 "terminal_outcome": self.state.data.get("terminal_outcome", "run_open"),
                 "resolution_ledger": self.data["ledger"]}
        atomic_json(path, value)


def exclusion_summary(state):
    rows = (state.data.get("source_recovery") or {}).get("records", {})
    excluded = [r for r in rows.values() if r["state"] == "excluded_quarantined"]
    return {"count": len(excluded), "workers": sorted({r["worker"] for r in excluded}),
            "statement": f"{len(excluded)} source documents deliberately excluded and not processed"}


def verify_excluded_archives(state):
    """Read-only guard: quarantines and earlier original versions stay recoverable.

    Checking only when opening the recovery UI is insufficient: a subsequent
    Apply action can run without opening that window again. Never issue a
    completion report/receipt if an archive is missing, changed or redirected.
    """
    root = safe_path(state.dir.resolve() / ".stage2-source-recovery", state.dir.resolve())
    rows = (state.data.get("source_recovery") or {}).get("records", {})
    for record in rows.values():
        for original in record.get("archives", []):
            try:
                saved_original = safe_path(original["path"], root)
                if not original.get("hash") or digest(saved_original) != original["hash"]:
                    raise RecoveryError("Retained original archive is missing or changed; recovery and completion are blocked.")
            except (OSError, KeyError, TypeError, ValueError):
                raise RecoveryError("Retained original archive cannot be verified; saved state is retained.") from None
        if record.get("state") != "excluded_quarantined":
            continue
        try:
            path_text, expected = record.get("quarantine_path"), record.get("quarantine_hash")
            if not path_text or not expected:
                raise RecoveryError("Excluded source archive has no verified identity.")
            archive = safe_path(path_text, root)
            if not archive.is_file() or digest(archive) != expected:
                raise RecoveryError("Excluded source archive is missing or changed; completion is blocked and active state is retained.")
        except (OSError, TypeError, ValueError):
            raise RecoveryError("Excluded source archive cannot be verified; completion is blocked and active state is retained.") from None


def accepted_request_hashes(state):
    """Hash -> immutable primary request with a durably acknowledged batch."""
    data = state.data
    requests = data.get("requests", {})
    accepted_ids = set()
    for batch in data.get("batches", []):
        if batch.get("phase", "primary") == "primary" and batch.get("id"):
            accepted_ids.update(batch.get("request_ids", []))
    for scope in (data.get("source_recovery") or {}).get("scopes", []):
        if scope.get("status") == "accepted" and scope.get("batch_id"):
            accepted_ids.update(scope["ids"])
    # Older states did not save per-batch request identities. Their completed
    # submission marker authorizes the fixed primary request dictionary only.
    marker = data.get("primary_submission") or {}
    if (data.get("primary_submission_complete") is True and data.get("batches")
            and marker.get("status") not in {"submission_started", "ambiguous"}
            and not any(b.get("phase", "primary") == "primary" and b.get("request_ids")
                        for b in data.get("batches", []))):
        accepted_ids.update(requests)
    return {meta["fhash"]: cid for cid, meta in requests.items()
            if cid in accepted_ids and meta.get("fhash")}


def request_aliases(state):
    """Return only hash-bound source aliases of acknowledged paid requests.

    Consumers replay classification for each alias's own path/worker without
    adding a second request or counting the representative's provider usage twice.
    """
    accepted = accepted_request_hashes(state)
    result = {}
    for cid, r in (state.data.get("source_recovery") or {}).get("records", {}).items():
        rid = r.get("request_id")
        if (r.get("state") in ACCEPTED and rid and rid != cid
                and accepted.get(r.get("hash")) == rid):
            result[cid] = rid
    return result


def reuse_accepted(recovery, cid, request_id):
    r = recovery.data["records"][cid]
    r.update(state="submitted", request_id=request_id, reused_accepted_evidence=True)
    recovery.sync_exception(cid)
    worker = recovery.state.data.get("workers", {}).get(
        str(Path(r["worker_dir"]).resolve()).casefold())
    if worker and not worker.get("completed"):
        worker["classification_status"] = "pending"
    recovery.event(cid, "accepted_evidence_reused", request_id=request_id)


def require_accounted_batch_costs(state):
    """Finite budgets cannot ignore earlier accepted, unaccounted purchases.

    Accounting markers are written only after complete validated result unions.
    Legacy runs without them must refresh through Apply accepted before buying
    a later source scope. This guard does not poll or contact any provider.
    """
    if float((state.data.get("settings") or {}).get("max_budget_gbp", 0) or 0) <= 0:
        return
    costs = state.data.get("costs") or {}
    followup = state.data.get("followup") or {}
    marker = followup.get("submission") or {}
    if (followup.get("phase") in {"submission_started", "ambiguous"}
            or marker.get("status") in {"submission_started", "ambiguous"}):
        raise RecoveryError("Earlier follow-up billing is uncertain. Reconcile that submission before buying another source scope.")
    for phase, batches in (("primary", state.data.get("batches", [])),
                           ("followup", followup.get("batches", []))):
        accepted = {b["id"] for b in batches if isinstance(b.get("id"), str) and b["id"]}
        accounted = costs.get(phase + "_accounted_batch_ids", [])
        if not isinstance(accounted, list) or not accepted.issubset(set(accounted)):
            raise RecoveryError("Earlier accepted batch costs are not yet accounted for. Use Apply accepted / Check batch status to settle their results and costs before buying another source scope under the saved budget.")


def submit_ready(recovery, ids, token, api, vocabulary, *, allow_partial=False,
                 stop=lambda: None, max_bytes=40 * 1024 * 1024, max_requests=1000):
    """Persist exact scopes before POST. Started/ambiguous scopes never replay.

    Evidence is prepared before this call. Chunk construction is local only;
    every still-unsubmitted source is checked again before every paid boundary.
    """
    recovery.assert_decision(ids, token)
    recovery.validate_current_scope()
    accepted_hashes = accepted_request_hashes(recovery.state)
    if any(recovery.data["records"][cid]["hash"] not in accepted_hashes for cid in ids):
        # Once for this explicitly confirmed scope, not between its own chunks.
        # Entirely reused aliases make no new purchase and need no new reserve.
        require_accounted_batch_costs(recovery.state)
    if not recovery.data["preflight_complete"] or (recovery.unresolved() and not allow_partial):
        raise RecoveryError("Waiting for whole-run source recovery; no request sent.")
    for scope in recovery.data["scopes"]:
        if scope["status"] in {"submission_started", "ambiguous"}:
            raise RecoveryError("An earlier submission is ambiguous; automatic retry is blocked.")
    marker = recovery.state.data.get("primary_submission") or {}
    if marker.get("status") in {"submission_started", "ambiguous"}:
        raise RecoveryError("An earlier primary submission is ambiguous; reconcile it first.")
    for cid in ids:
        recovery.evidence(cid)
    remaining = list(ids)
    submitted = []
    while remaining:
        stop()
        recovery.validate_current_scope()
        accepted = accepted_request_hashes(recovery.state)
        reused = []
        for cid in remaining:
            recovery.evidence(cid)
            rid = accepted.get(recovery.data["records"][cid]["hash"])
            if rid:
                reuse_accepted(recovery, cid, rid)
                reused.append(cid)
        if reused:
            recovery.state.data["primary_submission_complete"] = True
            recovery.state.data["phase"] = "primary_pending"
            recovery.checkpoint()
            remaining = [cid for cid in remaining if cid not in reused]
        if not remaining:
            break
        chunk, chunk_ids, size, hashes = [], [], 32, set()
        aliases = {}
        for cid in remaining:
            r = recovery.data["records"][cid]
            if r["hash"] in hashes:
                aliases[cid] = next(i for i in chunk_ids if recovery.data["records"][i]["hash"] == r["hash"])
                continue
            e = recovery.evidence(cid)
            system, blocks, mt = api.classify_payload(vocabulary, e["images"], e["text"],
                page_idxs=e["indices"], total_pages=e["pages"], segment=e["segment"])
            request = api.build_batch_request(cid, system, blocks, mt)
            request_size = len(json.dumps(request).encode()) + 2
            if request_size + 32 > max_bytes:
                raise RecoveryError("One prepared document exceeds the upload limit; source retained.")
            if chunk and (size + request_size > max_bytes or len(chunk) >= max_requests):
                break
            chunk.append(request)
            chunk_ids.append(cid)
            hashes.add(r["hash"])
            size += request_size
        # Revalidate the complete remaining source scope at the paid boundary.
        for cid in remaining:
            recovery.evidence(cid)
        stop()
        recovery.validate_current_scope()
        scope = recovery.scope(chunk_ids, recovery.token(chunk_ids), allow_partial=allow_partial)
        scope["parent_confirmation"] = token
        scope["aliases"] = aliases
        scope.update(status="submission_started", started_ts=now())
        state = recovery.state
        for cid in chunk_ids + list(aliases):
            if cid in chunk_ids:
                state.data.setdefault("requests", {})[cid] = copy.deepcopy(state.data["primary_inventory"][cid])
            r = recovery.data["records"][cid]
            r.update(state="supplemental_submission_started", request_id=aliases.get(cid, cid), scope_id=scope["id"])
        state.data["primary_submission"] = {"status": "submission_started",
            "request_identities": chunk_ids, "attempt_id": scope["id"], "started_ts": scope["started_ts"]}
        state.data["phase"] = "primary_submission_started"
        recovery.event(chunk_ids[0], "submission_started", scope_id=scope["id"])
        recovery.checkpoint()
        try:
            created = api.submit_batch(chunk)
            bid = created.get("id")
            if not isinstance(bid, str) or not bid:
                raise RecoveryError("Provider returned no batch identity.")
        except Exception:
            scope["status"] = "ambiguous"
            state.data["primary_submission"]["status"] = "ambiguous"
            recovery.checkpoint()
            raise RecoveryError("Submission outcome is uncertain. Retained request scope must be reconciled; do not resubmit.") from None
        scope.update(status="accepted", batch_id=bid, accepted_ts=now())
        state.add_batch(bid, len(chunk), created.get("processing_status", ""), request_ids=chunk_ids)
        state.data["primary_submission"].update(status="accepted", batch_id=bid)
        state.data.setdefault("primary_chunks", []).append(copy.deepcopy(state.data["primary_submission"]))
        for cid in chunk_ids + list(aliases):
            r = recovery.data["records"][cid]
            r.update(state="submitted", request_id=aliases.get(cid, cid), scope_id=scope["id"])
            recovery.sync_exception(cid)
            worker = state.data.get("workers", {}).get(str(Path(r["worker_dir"]).resolve()).casefold())
            if worker and not worker.get("completed"):
                # Re-apply only this worker; earlier accepted files replay from
                # the manifest and existing finishing operations retain lineage.
                worker["classification_status"] = "pending"
            recovery.event(cid, "submission_accepted", request_id=aliases.get(cid, cid), scope_id=scope["id"])
        state.data["phase"] = "primary_pending"
        # Unresolved sources remain durable but do not block independent paid
        # results/finishing. Exact per-worker validation still prevents movement.
        state.data["primary_submission_complete"] = True
        recovery.checkpoint()
        submitted.extend(chunk_ids)
        remaining = [cid for cid in remaining if cid not in chunk_ids and cid not in aliases]
    return submitted
