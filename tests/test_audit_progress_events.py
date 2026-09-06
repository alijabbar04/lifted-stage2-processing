"""Audit UI progress must describe completed evidence, not scheduled requests."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from _load_app import load_app

app = load_app()


def run_audit(tmp_path, events, *, writer_error=False, stop=False, observer=None):
    worker = tmp_path / "Worker"
    worker.mkdir()
    for name in ("DBS Document.pdf", "Jane personal notes.pdf", "Passport.pdf"):
        (worker / name).write_bytes(b"synthetic fixture - never sent to a model")
    kb = SimpleNamespace(vocabulary_block=lambda: "test vocabulary",
                         canonical_name=lambda name: next((item for item in ("DBS Document", "Passport", "BRP") if item.casefold() == name.casefold()), None),
                         group_of=lambda name: "Crucial")
    def classify(api, vocab, path, **kwargs):
        if path.name == "Passport.pdf":
            raise RuntimeError("synthetic request failed")
        return {"result": {"features": "synthetic evidence"}, "used_imgs": [1, 2]}
    def check_stop():
        if stop:
            raise app.StopRequested()
    def writer(rows, path, **kwargs):
        assert events[-1]["state"] == "writing_report"
        assert not any(e["state"] == "complete" for e in events)
        if writer_error:
            raise OSError("synthetic disk full")
        path.write_text("synthetic report", encoding="utf-8")
    with patch.object(app, "classify_document_core", side_effect=classify), \
         patch.object(app, "resolve_auto_review", return_value=("BRP", "Crucial")), \
         patch.object(app, "audit_adjudicate", return_value={"verdict": "another_type", "confidence": 90, "reason": "synthetic", "correct_name": "BRP", "other_label": ""}), \
         patch.object(app, "processing_reports_dir", return_value=tmp_path), \
         patch.object(app, "record_processing_report"), \
         patch.object(app, "_write_audit_workbook", side_effect=writer):
        return app.run_accuracy_audit(None, None, kb, [worker], tmp_path,
                                     resolution=1, check_stop=check_stop,
                                     on_progress=observer or events.append)


def test_audit_counts_custom_names_errors_and_adjudication(tmp_path):
    events = []
    rows, report = run_audit(tmp_path, events)
    assert report.is_file()
    assert [e["completed"] for e in events if e["state"] == "document_done"] == [1, 2, 3]
    adjudication = next(e for e in events if e["state"] == "adjudicating")
    assert adjudication["completed"] == 0
    assert events[-1]["state"] == "complete"
    assert events[-1]["completed"] == events[-1]["total"] == 3
    assert events[-1]["needs_review"] == 2
    assert events[-1]["errors"] == 1
    assert len(rows) == 3


def test_failed_report_write_never_emits_complete(tmp_path):
    events = []
    with pytest.raises(OSError):
        run_audit(tmp_path, events, writer_error=True)
    assert events[-1]["state"] == "writing_report"
    assert not any(e["state"] == "complete" for e in events)


def test_stop_never_emits_complete_or_counts_unchecked_document(tmp_path):
    events = []
    with pytest.raises(app.StopRequested):
        run_audit(tmp_path, events, stop=True)
    assert [e["state"] for e in events] == ["started"]
    assert events[-1]["completed"] == 0


def test_broken_progress_observer_does_not_break_audit(tmp_path):
    events = []
    def broken(event):
        events.append(event)
        raise RuntimeError("synthetic UI observer failure")
    rows, report = run_audit(tmp_path, events, observer=broken)
    assert len(rows) == 3 and report.is_file()
