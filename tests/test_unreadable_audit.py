"""Unreadable input must fail loudly BEFORE any provider call, never classify.

An encrypted PDF really was classified from empty evidence, reported
'Pages Examined = 1' and hidden behind the audit's Custom Name suppression, so
the all-flags review queue omitted it. These tests pin the corrected contract:
no image + no text -> UnreadableDocumentError with zero provider calls, and an
explicit zero-page 'Unable To Determine' audit row that all-flags includes.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from _load_app import load_app
from _pdf_fixtures import corrupt_pdf_bytes, encrypted_pdf_bytes, pdf_bytes

app = load_app()


class RefusingAPI:
    """Fails the test if any provider request is ever built for empty evidence."""
    def __init__(self):
        self.calls = []

    def classify(self, vocab, imgs, text, **kwargs):
        assert imgs or (text or "").strip(), \
            "classification was requested with no images and no text"
        self.calls.append(("classify", len(imgs), bool((text or "").strip())))
        return {"match": True, "name": "BRP", "group": "Crucial",
                "confidence": 95, "features": "synthetic evidence"}

    def triage(self, vocab, img, text, total_pages):
        assert img or (text or "").strip(), \
            "triage was requested with no image and no text"
        self.calls.append(("triage",))
        return {"enough_from_page1": False, "confidence": 0, "match": False,
                "reason": "synthetic"}


def classify_core(path, api):
    return app.classify_document_core(api, "test vocabulary", path,
                                      resolution=1, adaptive_pages=False,
                                      escalation_api=None)


def test_encrypted_pdf_is_refused_before_any_provider_call(tmp_path):
    doc = tmp_path / "null.pdf"
    doc.write_bytes(encrypted_pdf_bytes())
    api = RefusingAPI()
    with pytest.raises(app.UnreadableDocumentError):
        classify_core(doc, api)
    assert api.calls == []


def test_corrupt_pdf_is_refused_before_any_provider_call(tmp_path):
    doc = tmp_path / "broken.pdf"
    doc.write_bytes(corrupt_pdf_bytes())
    api = RefusingAPI()
    with pytest.raises(app.UnreadableDocumentError):
        classify_core(doc, api)
    assert api.calls == []


def test_readable_pdf_and_text_only_inputs_still_classify(tmp_path):
    # positive controls: image-backed PDF pages and text-only documents must
    # keep working exactly as before the unreadable guard
    doc = tmp_path / "readable.pdf"
    doc.write_bytes(pdf_bytes("readable control"))
    api = RefusingAPI()
    assert classify_core(doc, api)["result"]["name"] == "BRP"
    note = tmp_path / "note.txt"
    note.write_text("a text-only document with real words", encoding="utf-8")
    assert classify_core(note, api)["result"]["name"] == "BRP"
    assert len(api.calls) == 2


def run_audit(tmp_path, files):
    worker = tmp_path / "Worker"
    worker.mkdir(exist_ok=True)
    for name, data in files.items():
        (worker / name).write_bytes(data)
    kb = SimpleNamespace(
        vocabulary_block=lambda: "test vocabulary",
        canonical_name=lambda name: next(
            (item for item in ("BRP", "Passport") if item.casefold() == str(name).casefold()), None),
        group_of=lambda name: "Crucial")
    api = RefusingAPI()
    with patch.object(app, "resolve_auto_review", return_value=("BRP", "Crucial")), \
         patch.object(app, "processing_reports_dir", return_value=tmp_path), \
         patch.object(app, "record_processing_report"):
        rows, report = app.run_accuracy_audit(api, api, kb, [worker], tmp_path,
                                              resolution=1)
    return {row["Current Filename"]: row for row in rows}, api


def test_audit_reports_unreadable_as_zero_page_error_not_custom_name(tmp_path):
    rows, api = run_audit(tmp_path, {
        # uncontrolled filename: the old code hid this as 'Custom Name' with
        # 'Pages Examined = 1' from a classification of literally nothing
        "null.pdf": encrypted_pdf_bytes(),
        "broken.pdf": corrupt_pdf_bytes(),
        # readable control with a custom human filename must STILL be
        # suppressed as Custom Name (that suppression is correct when the
        # document was actually read)
        "Jane personal notes.pdf": pdf_bytes("custom name control"),
        # readable control whose classification matches its controlled name
        "BRP.pdf": pdf_bytes("matching control"),
    })
    for name in ("null.pdf", "broken.pdf"):
        row = rows[name]
        assert row["Review Status"] == "Unable To Determine"
        assert row["Pages Examined"] == 0
        assert str(row["Notes"]).startswith("error:")
        # all-flags candidate selection includes every non-Correct/Custom row
        assert row["Review Status"] not in ("Correct", "Custom Name", "")
    assert rows["Jane personal notes.pdf"]["Review Status"] == "Custom Name"
    assert rows["BRP.pdf"]["Review Status"] == "Correct"
    # the two unreadable files never reached the provider
    assert all(call[0] == "classify" for call in api.calls)
    assert len(api.calls) == 2
