"""Partial scope must be conspicuous in real generated audit CSV outputs."""
import csv
from types import SimpleNamespace
from unittest.mock import Mock

from test_source_recovery import app, pdf


def test_partial_audit_reports_never_claim_excluded_evidence_was_checked(tmp_path, monkeypatch):
    worker = tmp_path / "Included worker"
    pdf(worker / "Passport.pdf")
    excluded = tmp_path / "Excluded worker"
    pdf(excluded / "locked.pdf", "synthetic")
    monkeypatch.setattr(app, "processing_reports_dir", lambda name: tmp_path)
    monkeypatch.setattr(app, "record_processing_report", Mock())
    classify = Mock(return_value={"result": {"features": "synthetic"}, "used_imgs": [1]})
    monkeypatch.setattr(app, "classify_document_core", classify)
    monkeypatch.setattr(app, "resolve_auto_review", lambda *a, **k: ("Passport", "Crucial"))
    kb = SimpleNamespace(vocabulary_block=lambda: "Synthetic", canonical_name=lambda name: "Passport",
                         group_of=lambda name: "Crucial")
    rows, report = app.run_accuracy_audit(None, None, kb, [worker], tmp_path,
        resolution=1, source_exclusions={"count": 1, "workers": ["Excluded worker"],
            "statement": "1 source documents deliberately excluded and not processed"})
    assert len(rows) == 1 and classify.call_count == 1
    assert rows[0]["Notes"].startswith("PARTIAL RUN:")
    assert "excluded workers are outside this audit" in report.read_text(encoding="utf-8-sig")
    with report.with_name(report.stem + " - Summary.csv").open(encoding="utf-8-sig", newline="") as stream:
        summary = list(csv.DictReader(stream))
    assert summary[0] == {"metric": "RUN OUTCOME", "value": "COMPLETED WITH EXCLUSIONS"}
    assert {"metric": "Full-scope accuracy claim", "value": "NOT PERMITTED"} in summary
    assert not any("locked.pdf" in str(row) for row in rows)
