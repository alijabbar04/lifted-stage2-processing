"""Synthetic PDF edge cases and application-checkpoint crash matrix.

The matrix injects process-like failures immediately before and after each
verified Recovery.checkpoint in five successful single-source workflows. It
does not claim to emulate disk-controller failure or every byte-level OS I/O
window; atomic-copy/publication tests cover additional file-operation windows.
"""
import json
from pathlib import Path
import random
from unittest.mock import Mock

import fitz
import pytest

from test_source_recovery import app, pdf, recovery, reload, render, setup


PASSWORD = "matrix-only-password"


def test_real_21mib_aes256_pdf_is_identified_before_any_page_access(tmp_path, monkeypatch):
    path = tmp_path / "real-large-locked.pdf"
    payload = random.Random(1729).randbytes(21 * 1024 * 1024)
    with fitz.open() as doc:
        doc.new_page().insert_text((50, 60), "Synthetic encrypted document")
        doc.embfile_add("synthetic-random.bin", payload)
        doc.save(path, encryption=fitz.PDF_ENCRYPT_AES_256,
                 owner_pw="synthetic-owner", user_pw=PASSWORD)
    assert 20 * 1024 * 1024 < path.stat().st_size < 23 * 1024 * 1024
    actual_open = fitz.open
    with actual_open(path) as actual:
        assert actual.needs_pass
        assert actual.authenticate(PASSWORD)

    class EncryptionFirst:
        def __init__(self, actual):
            self.actual = actual

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.actual.close()

        @property
        def needs_pass(self):
            return self.actual.needs_pass

        def __len__(self):
            raise AssertionError("Encrypted document page count must not be queried")

        def __iter__(self):
            raise AssertionError("Encrypted pages must not be accessed")

        def __getattr__(self, name):
            raise AssertionError("No other encrypted-document access allowed: " + name)

    monkeypatch.setattr(recovery.fitz, "open", lambda *args, **kwargs:
                        EncryptionFirst(actual_open(*args, **kwargs)))
    inspected = recovery.inspect_source(path)
    assert inspected["cause"] == "password_protected_pdf"
    assert inspected["pages"] is None
    assert inspected["structural_validation"] == "requires_decryption"


def test_real_structurally_valid_zero_page_pdf_has_precise_cause(tmp_path):
    path = tmp_path / "zero-pages.pdf"
    data = bytearray(b"%PDF-1.4\n")
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [] /Count 0 >>"]
    offsets = [0]
    for index, value in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{index} 0 obj\n".encode() + value + b"\nendobj\n")
    xref = len(data)
    data.extend(b"xref\n0 3\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size 3 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(data)
    with fitz.open(path) as doc:
        assert len(doc) == 0 and not doc.is_repaired
    result = recovery.inspect_source(path)
    assert result["cause"] == "zero_page_pdf"
    assert result["pages"] == 0


@pytest.mark.parametrize("cause", ["corrupt_or_truncated_pdf", "no_renderable_or_extractable_content"])
def test_authenticated_copy_validation_failure_retains_encrypted_original(tmp_path, monkeypatch, cause):
    # Authentication and decryption are real. A validation failure is injected
    # because creating stable, cross-MuPDF malformed encrypted fixtures is not
    # reliable. This exercises the post-authentication failure branch honestly.
    c, paths = setup(tmp_path, [PASSWORD])
    c.preflight(render)
    original = paths[0].read_bytes()
    original_inspect = recovery.inspect_source
    examined = []

    def fail_decrypted_candidate(path, *args, **kwargs):
        result = original_inspect(path, *args, **kwargs)
        if Path(path).resolve().is_relative_to(c.root.resolve()):
            with fitz.open(path) as decrypted:
                assert not decrypted.needs_pass and not decrypted.is_encrypted
            examined.append(Path(path))
            result["cause"] = cause
        return result

    monkeypatch.setattr(recovery, "inspect_source", fail_decrypted_candidate)
    result = c.unlock(["0"], PASSWORD, c.token(["0"]), render)
    assert examined and result["unreadable"] == 1 and result["unlocked"] == 0
    assert c.data["records"]["0"]["state"] == "decrypted_but_unreadable"
    assert c.data["records"]["0"]["cause"] == cause
    assert paths[0].read_bytes() == original
    with fitz.open(paths[0]) as doc:
        assert doc.needs_pass
    assert not c.state.data["requests"]
    assert PASSWORD not in json.dumps(c.state.data)


class SimulatedCrash(BaseException):
    """Escape normal exception handling as a terminated process would."""


# These are exact counts for the successful single-source action below. The
# count contract deliberately fails if a new durable transition is added, so
# the matrix must then be extended rather than silently missing that boundary.
CHECKPOINT_COUNTS = {"preflight": 5, "unlock": 5, "replace": 3,
                     "quarantine": 2, "reinstate": 3}
MATRIX = [(action, index, timing)
          for action, count in CHECKPOINT_COUNTS.items()
          for index in range(1, count + 1) for timing in ("before", "after")]


def fixture_for(root, action):
    c, paths = setup(root, [None if action == "preflight" else PASSWORD])
    original = paths[0].read_bytes()
    replacement = pdf(root / "replacement.pdf", text="Synthetic selected replacement") if action == "replace" else None
    if action != "preflight":
        c.preflight(render)
    if action == "reinstate":
        c.quarantine(["0"], c.token(["0"]))
    return c, paths[0], original, replacement


def perform(c, action, replacement):
    if action == "preflight":
        c.preflight(render)
    elif action == "unlock":
        c.unlock(["0"], PASSWORD, c.token(["0"]), render)
    elif action == "replace":
        c.replace("0", replacement, c.token(["0"]), render)
    elif action == "quarantine":
        c.quarantine(["0"], c.token(["0"]))
    elif action == "reinstate":
        c.reinstate(["0"], c.token(["0"]))


@pytest.mark.parametrize("action,expected", CHECKPOINT_COUNTS.items())
def test_matrix_includes_every_current_successful_checkpoint(tmp_path, action, expected):
    c, _source, _original, replacement = fixture_for(tmp_path, action)
    actual = c.checkpoint
    c.checkpoint = Mock(side_effect=actual)
    perform(c, action, replacement)
    assert c.checkpoint.call_count == expected


@pytest.mark.parametrize("action,index,timing", MATRIX,
                         ids=[f"{a}-checkpoint-{i}-{t}" for a, i, t in MATRIX])
def test_each_local_checkpoint_recovers_without_lost_original_or_paid_request(tmp_path, action, index, timing):
    c, source, original, replacement = fixture_for(tmp_path, action)
    actual_checkpoint = c.checkpoint
    calls = 0

    def crash_at_boundary():
        nonlocal calls
        calls += 1
        if calls == index and timing == "before":
            raise SimulatedCrash()
        actual_checkpoint()
        if calls == index and timing == "after":
            raise SimulatedCrash()

    c.checkpoint = crash_at_boundary
    with pytest.raises(SimulatedCrash):
        perform(c, action, replacement)
    assert calls == index

    # Re-open using the actual recovery-window sequence: resume durable file
    # intents, then finish local inspection/evidence preparation. If the
    # operator's decision was never committed, confirm that local action again.
    restarted = reload(c)
    restarted.resume(render)
    restarted.preflight(render)
    if action == "preflight":
        assert restarted.data["preflight_complete"] is True
    elif action == "unlock" and restarted.data["records"]["0"]["state"] in recovery.LOCKED:
        perform(restarted, action, replacement)
    elif action == "replace" and recovery.digest(source) != recovery.digest(replacement):
        perform(restarted, action, replacement)
    elif action == "quarantine" and restarted.data["records"]["0"]["state"] != "excluded_quarantined":
        perform(restarted, action, replacement)
    elif action == "reinstate" and restarted.data["records"]["0"]["state"] == "excluded_quarantined":
        perform(restarted, action, replacement)

    final = reload(restarted)
    final.verify_ledger()
    record = final.data["records"]["0"]
    if action in ("unlock", "replace"):
        assert record["state"] == "unlocked_validated"
        assert Path(record["archive_path"]).read_bytes() == original
        assert source.is_file()
        assert not record.get("pending_resolution")
    elif action == "quarantine":
        assert record["state"] == "excluded_quarantined"
        assert not source.exists()
        assert Path(record["quarantine_path"]).read_bytes() == original
        assert not record.get("quarantine_intent")
    else:
        assert source.read_bytes() == original
        assert not record.get("reinstate_intent")
    assert not final.state.data["requests"]
    assert not final.data["scopes"]
    assert PASSWORD not in json.dumps(final.state.data)
