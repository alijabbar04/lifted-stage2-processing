"""Regression: blind audit agreement must not hide contrary finishing evidence.

Single-subject policies keep being left in Employee Handbook ranking families.
Batch finishing has already paid for a quality assessment of every member, and
the retained 2026-09 Chiltern batch states (195 distinct handbook-family
members across 40 workers) separate the two populations completely: every
genuine handbook scored 72-92 with legible=true and complete=true, while every
misfiled policy scored 15-35 with legible=true and complete=FALSE - the
finishing prompt judges completeness against the claimed family, so a wrong
type is "incomplete" by construction. The first shipped signal required
complete=true and a score of 20 or below, and therefore fired for none of
them; five misfiled policies in the 2026-09-10 cohort stayed "Correct".

These tests keep the finishing score separate from classification confidence:
the narrow conflict (Employee Handbook, legible, score <= 40) becomes Unable
To Determine, with no suggested rename and no second paid call. Genuine
handbooks at their lowest observed score, scores just above the threshold,
illegible copies, other families, invalid state, and conflicting evidence
remain controls.
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


def audit_many(tmp_path, documents):
    """Audit several files of one worker in a single run.

    `documents` maps filename -> (predicted type, finishing hint or None).
    Returns ({filename: row}, adjudicator mock)."""
    worker = tmp_path / "Worker"
    worker.mkdir()
    hints, predicted = {}, {}
    for filename, (pred, hint) in documents.items():
        document = worker / filename
        document.write_bytes(
            f"synthetic evidence for {filename}; never sent to a provider".encode())
        predicted[filename] = pred
        if hint is not None:
            hints[app.file_hash(document)] = hint
    adjudicator = Mock()

    def resolve(_kb, result):
        return predicted[result["features"]], "Important"

    def classify(_api, _vocab, path, **_kwargs):
        return {"result": {"features": path.name}, "used_imgs": [object()]}

    with patch.object(app, "classify_document_core", side_effect=classify), \
         patch.object(app, "resolve_auto_review", side_effect=resolve), \
         patch.object(app, "audit_adjudicate", adjudicator), \
         patch.object(app, "processing_reports_dir", return_value=tmp_path), \
         patch.object(app, "record_processing_report"), \
         patch.object(app, "_write_audit_workbook",
                      side_effect=lambda rows, path, **_kwargs:
                      path.write_bytes(b"synthetic workbook")):
        rows, _report = app.run_accuracy_audit(
            None, None, KB(), [worker], tmp_path, resolution=1,
            finishing_quality_hints=hints)
    return {row["Current Filename"]: row for row in rows}, adjudicator


def audit_one(tmp_path, filename, *, predicted, quality_hint):
    rows, adjudicator = audit_many(tmp_path, {filename: (predicted, quality_hint)})
    return rows[filename], adjudicator


WRONG_TYPE_HINT = {"score": 15, "legible": True, "complete": False,
                   "note": "Policy document, not employee handbook"}


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
        quality_hint=WRONG_TYPE_HINT)

    assert row["Review Status"] == "Unable To Determine"
    assert row["Confidence Score"] == 0
    assert row["Suggested Filename"] == ""
    assert "15/100" in row["Reason For Concern"]
    assert "Policy document, not employee handbook" in row["Reason For Concern"]
    assert "not classification confidence" in row["Reason For Concern"]
    adjudicator.assert_not_called()


def test_incomplete_flag_and_scores_up_to_the_threshold_are_still_surfaced(tmp_path):
    # the observed misfiled-policy population: 15, 25, 28 and 35, all legible
    # and all reported incomplete for the claimed handbook family
    for index, score in enumerate((15, 25, 28, 35, 40)):
        case = tmp_path / str(index)
        case.mkdir()
        row, adjudicator = audit_one(
            case, "Employee Handbook.pdf", predicted="Employee Handbook",
            quality_hint={"score": score, "legible": True, "complete": False,
                          "note": ""})
        assert row["Review Status"] == "Unable To Determine", score
        assert row["Suggested Filename"] == ""
        assert f"{score}/100" in row["Reason For Concern"]
        assert "finishing note" not in row["Reason For Concern"]
        adjudicator.assert_not_called()


def test_family_replay_flags_only_the_wrong_type_member(tmp_path):
    # the shape of a real family: a current handbook, an older edition and a
    # single-subject policy ranked into the same family
    rows, adjudicator = audit_many(tmp_path, {
        "Employee Handbook (02).pdf": ("Employee Handbook", {
            "score": 92, "legible": True, "complete": True,
            "note": "Current, clear, comprehensive handbook"}),
        "Employee Handbook (01).pdf": ("Employee Handbook", {
            "score": 72, "legible": True, "complete": True,
            "note": "Clear handbook, February 2021 edition"}),
        "Employee Handbook.pdf": ("Employee Handbook", {
            "score": 15, "legible": True, "complete": False,
            "note": "Health & Safety Policy, not Employee Handbook"}),
    })
    assert rows["Employee Handbook (02).pdf"]["Review Status"] == "Correct"
    assert rows["Employee Handbook (01).pdf"]["Review Status"] == "Correct"
    assert rows["Employee Handbook.pdf"]["Review Status"] == "Unable To Determine"
    assert rows["Employee Handbook.pdf"]["Suggested Filename"] == ""
    assert all(row["Confidence Score"] == 0 for row in rows.values())
    adjudicator.assert_not_called()


def test_nearby_controls_remain_correct(tmp_path):
    cases = [
        # lowest genuine handbook score observed across the retained states
        ("Employee Handbook.pdf", "Employee Handbook",
         {"score": 72, "legible": True, "complete": True}),
        ("Employee Handbook.pdf", "Employee Handbook",
         {"score": 85, "legible": True, "complete": True}),
        # just above the threshold, even when reported incomplete
        ("Employee Handbook.pdf", "Employee Handbook",
         {"score": 41, "legible": True, "complete": False}),
        # a low score explained by legibility is a scan problem, not a conflict
        ("Employee Handbook.pdf", "Employee Handbook",
         {"score": 15, "legible": False, "complete": False}),
        # other families mix wrong-type, expired and poor-scan causes at the
        # same scores, so the signal stays Handbook-only
        ("DBS Document.pdf", "DBS Document",
         {"score": 15, "legible": True, "complete": False}),
    ]
    for index, (filename, predicted, hint) in enumerate(cases):
        case = tmp_path / str(index)
        case.mkdir()
        row, adjudicator = audit_one(
            case, filename, predicted=predicted, quality_hint=hint)
        assert row["Review Status"] == "Correct", (index, hint)
        assert row["Confidence Score"] == 0
        adjudicator.assert_not_called()


def test_audit_quality_boundary_and_boolean_flags_fail_closed():
    assert app.AUDIT_HANDBOOK_RELEVANCE_REVIEW_MAX == 40
    assert app.audit_finishing_quality_concern(
        "Employee Handbook", {"score": 15, "legible": True, "complete": False})
    assert app.audit_finishing_quality_concern(
        "Employee Handbook", {"score": 35, "legible": True, "complete": False})
    assert app.audit_finishing_quality_concern(
        "Employee Handbook", {"score": 40, "legible": True, "complete": True})
    assert not app.audit_finishing_quality_concern(
        "Employee Handbook", {"score": 41, "legible": True, "complete": False})
    assert not app.audit_finishing_quality_concern(
        "Employee Handbook", {"score": 72, "legible": True, "complete": True})
    assert not app.audit_finishing_quality_concern(
        "Employee Handbook", {"score": 15, "legible": False, "complete": False})
    assert not app.audit_finishing_quality_concern(
        "DBS Document", {"score": 15, "legible": True, "complete": False})
    # malformed typed flags are invalid evidence, never coerced
    assert not app.audit_finishing_quality_concern(
        "Employee Handbook", {"score": 15, "legible": "true", "complete": False})
    assert not app.audit_finishing_quality_concern(
        "Employee Handbook", {"score": 20, "legible": "false", "complete": "false"})
    for score in ("Infinity", "-Infinity", "1e999"):
        assert not app.audit_finishing_quality_concern(
            "Employee Handbook", {"score": score, "legible": True, "complete": True})


def test_only_completed_byte_bound_nonconflicting_state_becomes_a_hint():
    digest = "a" * 64
    operation_id = f"quality:Employee Handbook:{digest}"
    quality = {"score": 15, "legible": True, "complete": False,
               "note": "Policy document, not employee handbook"}

    def state(*, family_status="complete", operation_status="complete",
              result=quality, member_hash=digest):
        return {"workers": {"worker": {
            "ranking_families": {"Employee Handbook": {
                "status": family_status, "members": [{"hash": member_hash}]}},
            "finishing_operations": {operation_id: {
                "status": operation_status, "result": result}},
        }}}

    assert app.audit_finishing_quality_hints(state()) == {digest: {
        "score": 15, "legible": True, "complete": False,
        "date": "", "note": "Policy document, not employee handbook"}}
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
