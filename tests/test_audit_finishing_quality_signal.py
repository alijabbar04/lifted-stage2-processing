"""Regression: blind audit agreement must not hide contrary finishing evidence.

The 2026-09-10 five-worker review found nine legible, complete single-subject
policies left in Employee Handbook families. Their already-paid finishing
scores were 15-20, while the genuine handbook controls scored 85-95. The
audit independently repeated the existing type for the missed peers and
therefore marked them Correct without adjudication.

These tests keep the finishing score separate from classification confidence:
the narrow conflict becomes Unable To Determine, with no suggested rename and
no second paid call. High-scoring handbooks, low-scoring other families,
incomplete copies, invalid state, and conflicting evidence remain controls.
"""
from unittest.mock import Mock, patch

from _load_app import load_app


app = load_app()


class KB:
    def vocabulary_block(self):
        return "Employee Handbook\nDBS Document"

    def canonical_name(self, name):
        return name if name in ("Employee Handbook", "DBS Document") else None

    def group_of(self, _name):
        return "Important"


def audit_one(tmp_path, filename, *, predicted, quality_hint):
    worker = tmp_path / "Worker"
    worker.mkdir()
    document = worker / filename
    document.write_bytes(b"synthetic evidence; never sent to a provider")
    digest = app.file_hash(document)
    adjudicator = Mock()
    with patch.object(app, "classify_document_core", return_value={
            "result": {"features": "synthetic classification evidence"},
            "used_imgs": [object()]}), \
         patch.object(app, "resolve_auto_review",
                      return_value=(predicted, "Important")), \
         patch.object(app, "audit_adjudicate", adjudicator), \
         patch.object(app, "processing_reports_dir", return_value=tmp_path), \
         patch.object(app, "record_processing_report"), \
         patch.object(app, "_write_audit_workbook",
                      side_effect=lambda rows, path, **_kwargs:
                      path.write_bytes(b"synthetic workbook")):
        rows, _report = app.run_accuracy_audit(
            None, None, KB(), [worker], tmp_path, resolution=1,
            finishing_quality_hints=({digest: quality_hint}
                                     if quality_hint is not None else {}))
    return rows[0], adjudicator


def test_blind_handbook_agreement_with_low_relevance_is_human_review(tmp_path):
    baseline = tmp_path / "without-finishing-evidence"
    baseline.mkdir()
    old_row, old_adjudicator = audit_one(
        baseline, "Employee Handbook.pdf", predicted="Employee Handbook",
        quality_hint=None)
    assert old_row["Review Status"] == "Correct"
    old_adjudicator.assert_not_called()

    corrected = tmp_path / "with-finishing-evidence"
    corrected.mkdir()
    row, adjudicator = audit_one(
        corrected, "Employee Handbook.pdf", predicted="Employee Handbook",
        quality_hint={"score": 20, "legible": True, "complete": True})

    assert row["Review Status"] == "Unable To Determine"
    assert row["Confidence Score"] == 0
    assert row["Suggested Filename"] == ""
    assert "20/100" in row["Reason For Concern"]
    assert "not classification confidence" in row["Reason For Concern"]
    adjudicator.assert_not_called()


def test_nearby_controls_remain_correct(tmp_path):
    cases = [
        ("Employee Handbook.pdf", "Employee Handbook",
         {"score": 85, "legible": True, "complete": True}),
        ("Employee Handbook.pdf", "Employee Handbook",
         {"score": 21, "legible": True, "complete": True}),
        ("Employee Handbook.pdf", "Employee Handbook",
         {"score": 20, "legible": True, "complete": False}),
        ("DBS Document.pdf", "DBS Document",
         {"score": 20, "legible": True, "complete": True}),
    ]
    for index, (filename, predicted, hint) in enumerate(cases):
        case = tmp_path / str(index)
        case.mkdir()
        row, adjudicator = audit_one(
            case, filename, predicted=predicted, quality_hint=hint)
        assert row["Review Status"] == "Correct"
        assert row["Confidence Score"] == 0
        adjudicator.assert_not_called()


def test_audit_quality_boundary_and_boolean_flags_fail_closed():
    assert app.audit_finishing_quality_concern(
        "Employee Handbook", {"score": 19, "legible": True, "complete": True})
    assert app.audit_finishing_quality_concern(
        "Employee Handbook", {"score": 20, "legible": True, "complete": True})
    assert not app.audit_finishing_quality_concern(
        "Employee Handbook", {"score": 21, "legible": True, "complete": True})
    assert not app.audit_finishing_quality_concern(
        "Employee Handbook", {"score": 20, "legible": "false", "complete": "false"})
    for score in ("Infinity", "-Infinity", "1e999"):
        assert not app.audit_finishing_quality_concern(
            "Employee Handbook", {"score": score, "legible": True, "complete": True})


def test_only_completed_byte_bound_nonconflicting_state_becomes_a_hint():
    digest = "a" * 64
    operation_id = f"quality:Employee Handbook:{digest}"
    quality = {"score": 15, "legible": True, "complete": True}

    def state(*, family_status="complete", operation_status="complete",
              result=quality, member_hash=digest):
        return {"workers": {"worker": {
            "ranking_families": {"Employee Handbook": {
                "status": family_status, "members": [{"hash": member_hash}]}},
            "finishing_operations": {operation_id: {
                "status": operation_status, "result": result}},
        }}}

    assert app.audit_finishing_quality_hints(state()) == {digest: {
        "score": 15, "legible": True, "complete": True,
        "date": "", "note": ""}}
    assert app.audit_finishing_quality_hints(
        state(family_status="deferred")) == {}
    assert app.audit_finishing_quality_hints(
        state(operation_status="failed")) == {}
    assert app.audit_finishing_quality_hints(
        state(result={"legible": True})) == {}
    assert app.audit_finishing_quality_hints(
        state(member_hash="not-a-sha256")) == {}

    malformed = state()
    malformed["workers"]["worker"]["finishing_operations"][operation_id] = "bad"
    assert app.audit_finishing_quality_hints(malformed) == {}

    malformed_flags = state(result={"score": 20, "legible": "false", "complete": "false"})
    assert app.audit_finishing_quality_hints(malformed_flags) == {}

    malformed_family = {"workers": {"worker": {
        "ranking_families": {"Employee Handbook": {
            "status": "complete", "members": "not-a-list"}},
        "finishing_operations": {}}}}
    assert app.audit_finishing_quality_hints(malformed_family) == {}

    conflicting = state()
    conflicting["workers"]["other"] = {
        "ranking_families": {"Employee Handbook": {
            "status": "complete", "members": [{"hash": digest}]}},
        "finishing_operations": {operation_id: {
            "status": "complete", "result": {
                "score": 90, "legible": True, "complete": True}}},
    }
    assert app.audit_finishing_quality_hints(conflicting) == {}
