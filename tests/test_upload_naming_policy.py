import hashlib
import json
from pathlib import Path

from _load_app import load_app

app = load_app()
import upload_naming_policy as policy


INCIDENT_LABELS = [
    "Other - Medication Administration Compliance Policy Acknowledgement",
    "Other - Recruitment of Ex-Offenders Policy acknowledgement",
    "Other - Ecctis qualification and English proficiency statement",
]


def test_incident_labels_are_semantic_word_complete_and_portal_safe():
    shortened = [policy.portal_safe_stem(label) for label in INCIDENT_LABELS]
    assert all(policy.normalized_length(label) <= 50 for label in shortened)
    assert all(not label.endswith((" Administr", " acknowledg", " proficien"))
               for label in shortened)
    assert "Medication" in shortened[0] and "Acknowledgement" in shortened[0]
    assert "Recruitment" in shortened[1] and "Ex-Offenders" in shortened[1]
    assert "Ecctis" in shortened[2] and "proficiency" in shortened[2]


def test_suffix_budget_is_reserved_before_emitting_final_name(tmp_path):
    stem = "Other - exceptionally detailed professional qualification evidence statement"
    first = app.unique_path(tmp_path, stem, ".pdf")
    first.write_bytes(b"one")
    second = app.unique_path(tmp_path, stem, ".pdf")
    assert second.name.endswith("(2).pdf")
    assert policy.normalized_length(first.name) <= 50
    assert policy.normalized_length(second.name) <= 50
    assert "~" in first.stem  # stable distinction only after lossy shortening


def test_other_name_never_slices_through_a_word_and_is_deterministic():
    source = ("Detailed medication administration compliance acknowledgement "
              "for temporary healthcare professionals")
    first = app.other_name(source)
    assert first == app.other_name(source)
    assert policy.normalized_length(first) <= 50
    assert all(piece in source.casefold() or piece.startswith("~")
               for piece in first.casefold().replace("other - ", "").split())


def test_manual_dialog_counter_includes_other_prefix_and_collision_reserve():
    label = "Recruitment of Ex-Offenders Policy acknowledgement"
    relevant_used, relevant_remaining = app.UnknownDialog.choice_budget(
        "Relevant", label)
    other_used, other_remaining = app.UnknownDialog.choice_budget("Other", label)
    assert other_used > relevant_used
    assert other_remaining < 0
    assert relevant_remaining >= 0


def test_controlled_vocabulary_audit_and_handoff_manifest(tmp_path):
    assert policy.validate_controlled_names(
        [name for name, _ in app.SEED_CRUCIAL + app.SEED_IMPORTANT + app.SEED_OTHER]) == []
    assert policy.validate_controlled_names(
        ["A deliberately overlong controlled vocabulary name " * 3])
    manifest = app.ProcessedManifest(tmp_path)
    digest = hashlib.sha256(b"synthetic handoff").hexdigest()
    short = policy.portal_safe_stem(INCIDENT_LABELS[2])
    manifest.record(digest, "test-model", 1.5, short, "Other",
                    short + ".pdf", full_description=INCIDENT_LABELS[2])
    manifest.save()
    handoff = json.loads((tmp_path / app.HANDOFF_MANIFEST_NAME).read_text(
        encoding="utf-8"))
    assert handoff["naming_policy_version"] == policy.NAMING_POLICY_VERSION
    assert handoff["documents"][0]["full_document_description"] == INCIDENT_LABELS[2]
    assert handoff["documents"][0]["short_upload_label"] == short
    assert policy.normalized_length(handoff["documents"][0]["final_filename"]) <= 50


def test_main_recheck_and_ai_review_paths_use_shared_policy():
    recheck_source = Path(app.__file__).with_name("Stage2_Recheck_Unknowns.pyw").read_text(
        encoding="utf-8")
    review_source = Path(app.__file__).with_name("ai_review.py").read_text(
        encoding="utf-8")
    assert "s2.unique_path" in recheck_source and "s2.safe_stem" in recheck_source
    assert "upload_naming_policy.final_filename" in review_source
