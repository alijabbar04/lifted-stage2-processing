"""Vocabulary contracts for single-subject policy packs and worker records.

The 2026-09-07 five-worker trial filed 191 single-subject policy and
procedure packs (each ending in an e-signature certificate, some carrying
blank appendix forms) as 'Employee Handbook', a spot-checks policy and
shadowing/competency records as 'Spot Check', investigation minutes as
'Supervision', and a warning letter as 'Probation Review'. The definitions
had no exclusion and, for the policies, no destination. These tests pin the
offline half of the fix: the wording the model receives, its propagation to
an existing vocabulary workbook, and the acceptance of the descriptive
destinations.

The 2026-09-08 independent QA added two boundary controls the wording must
honour: a full policy that starts a file keeps its identity even when a
FILLED-IN appendix follows it (rule 18, first complete document wins), and a
genuine worker record keeps its identity when it is partly filled or
unsigned (completeness and signatures are quality, not identity).

These are wording and acceptance checks. Live classification accuracy at
these boundaries still needs a measured batch.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _load_app import load_app


app = load_app()
IMPORTANT = dict(app.SEED_IMPORTANT)
ALL_SEEDS = dict(app.SEED_CRUCIAL + app.SEED_IMPORTANT + app.SEED_OTHER)


class TestPolicyPackDefinitions(unittest.TestCase):

    def test_handbook_excludes_single_subject_policies_with_a_destination(self):
        desc = IMPORTANT["Employee Handbook"]
        self.assertIn("SINGLE-SUBJECT policy and procedure", desc)
        self.assertIn("'Other - <their title>'", desc)
        self.assertIn("signing certificate or a blank form does not turn one policy into the handbook", desc)
        # the positive definition and the existing narrower destinations survive
        self.assertIn("STAFF/CAREGIVER HANDBOOK booklet itself", desc)
        self.assertIn("'Employee Handbook Receipt'", desc)
        self.assertIn("Employee Handbook Receipt", ALL_SEEDS)

    def test_spot_check_is_a_worker_record_not_gated_on_completion(self):
        desc = IMPORTANT["Spot Check"]
        self.assertIn("spot-check record OF THIS WORKER", desc)
        # completeness and signatures rank the record; they do not define it
        self.assertNotIn("COMPLETED", desc)
        self.assertIn("usually but not necessarily signed or fully completed", desc)
        self.assertIn("partly filled or unsigned spot check of this worker is STILL a 'Spot Check'", desc)
        for destination in ("'Other - Spot Checks Policy and Procedure'",
                            "'Other - Shadowing Checklist'",
                            "'Other - Medication Administration Competency Assessment'"):
            self.assertIn(destination, desc)

    def test_supervision_excludes_investigation_minutes(self):
        desc = IMPORTANT["Supervision"]
        self.assertIn("supervision meeting RECORD", desc)
        self.assertIn("'Other - Investigation Meeting Minutes'", desc)
        self.assertIn("even when supervision is mentioned", desc)

    def test_partial_or_unsigned_worker_records_keep_their_identity(self):
        # the QA's second control: a genuine but incomplete worker record is
        # still the controlled type; it only ranks lower for completeness
        self.assertIn("unsigned or partly completed supervision record of this worker is STILL 'Supervision'",
                      IMPORTANT["Supervision"])
        self.assertIn("it ranks lower for completeness", IMPORTANT["Supervision"])
        self.assertIn("it ranks lower for completeness", IMPORTANT["Spot Check"])
        rules = app.DISAMBIGUATION_RULES
        self.assertIn("even when some fields are empty or a signature is missing", rules)
        self.assertIn("completeness and signatures affect its quality rank, not what it is", rules)
        # nothing in the record side demands a completed, dated, signed form
        self.assertNotIn("COMPLETED worker form", rules)

    def test_probation_review_keeps_content_over_heading(self):
        desc = IMPORTANT["Probation Review"]
        self.assertIn("'Other - Disciplinary Warning Letter'", desc)
        self.assertIn("'Other - Shadowing Checklist'", desc)
        # negative control from the trial: a 'Performance Management' heading
        # does not rule out a form that explicitly records the probation review
        self.assertIn("'Performance Management' whose content explicitly records the probation review", desc)
        self.assertIn("EXPLICIT probation context", desc)

    def test_rule_22_separates_policy_packs_from_handbooks_and_records(self):
        rules = app.DISAMBIGUATION_RULES
        self.assertIn("22. A POLICY PACK IS NEITHER A HANDBOOK NOR A WORKER RECORD", rules)
        self.assertIn("e-signature / signing-certificate page does not make it a 'Spot Check'", rules)
        # the record side is defined by standing alone or starting the file
        self.assertIn("a WORKER RECORD is a form about THIS worker that stands alone or STARTS the file", rules)
        # rule I (classify by what it IS) remains the overriding rule
        self.assertIn("I. CLASSIFY A DOCUMENT BY WHAT IT *IS*", rules)

    def test_rule_22_defers_to_rule_18_for_a_policy_with_a_filled_appendix(self):
        # the QA's first control: a complete policy that starts the file is
        # not overridden by a filled-in appendix form (first complete
        # document wins), and the converse order keeps the record
        rules = app.DISAMBIGUATION_RULES
        self.assertIn("rule 18(b) decides", rules)
        self.assertIn("a full policy followed by its filled-in appendix stays 'Other - <policy title>'", rules)
        self.assertIn("a filled-in form followed by policy extracts stays the worker record", rules)
        self.assertIn("THE FIRST COMPLETE DOCUMENT WINS", rules)
        self.assertIn("filled in where the policy text starts the file - rule 18", IMPORTANT["Employee Handbook"])
        self.assertIn("blank or filled-in spot-check form when the policy starts the file - rule 18",
                      IMPORTANT["Spot Check"])
        # no blanket converse survives
        self.assertNotIn("never the policy", rules)

    def test_no_new_controlled_type_and_no_filename_special_case(self):
        names = {n for n, _ in app.SEED_CRUCIAL + app.SEED_IMPORTANT + app.SEED_OTHER}
        for invented in ("Policy and Procedure", "Policy Document", "Shadowing Checklist",
                         "Investigation Meeting Minutes", "Disciplinary Warning Letter"):
            self.assertNotIn(invented, names)
        self.assertIn("Employee Handbook", names)
        self.assertIn("Spot Check", names)
        self.assertIn("Supervision", names)
        self.assertIn("Probation Review", names)


class TestDestinationsAreAccepted(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        appdata = Path(self.temp.name)
        self.patches = [patch.object(app, "APP_DIR", appdata),
                        patch.object(app, "RECORD_XLSX", appdata / "record.xlsx"),
                        patch.object(app, "CONFIG_PATH", appdata / "config.json")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.temp.cleanup()

    def test_existing_workbook_is_reseeded_with_the_new_wording(self):
        from openpyxl import Workbook
        book = Workbook()
        book.remove(book.active)
        for tab, rows in (("Crucial", app.SEED_CRUCIAL), ("Important", app.SEED_IMPORTANT),
                          ("Other", app.SEED_OTHER)):
            sheet = book.create_sheet(tab)
            sheet.append(["Filename", "Description / Identification Features"])
            for name, desc in rows:
                if name in ("Employee Handbook", "Spot Check", "Supervision", "Probation Review"):
                    desc = "stale description from an earlier install"
                sheet.append([name, desc])
        book.save(app.RECORD_XLSX)
        kb = app.KnowledgeBase()
        block = kb.vocabulary_block()
        self.assertIn("SINGLE-SUBJECT policy and procedure", block)
        self.assertIn("spot-check record OF THIS WORKER", block)
        self.assertIn("partly filled or unsigned spot check", block)
        self.assertIn("'Other - Investigation Meeting Minutes'", block)
        self.assertNotIn("stale description", block)
        self.assertEqual(kb.group_of("Employee Handbook"), "Important")

    def test_policy_description_files_as_descriptive_other_not_handbook(self):
        kb = app.KnowledgeBase()
        result = {"match": False, "name": "", "confidence": 88,
                  "other_label": "Training Policy and Procedure"}
        self.assertEqual(app.resolve_auto_review(kb, result),
                         ("Other - Training Policy and Procedure", "Other"))
        matched, name, group, _conf, _features, label = app.validate_result(kb, result)
        self.assertFalse(matched)
        self.assertEqual(label, "Training Policy and Procedure")
        self.assertEqual(app.other_name(label), "Other - Training Policy and Procedure")

    def test_genuine_handbook_and_worker_records_still_resolve(self):
        kb = app.KnowledgeBase()
        for name, group in (("Employee Handbook", "Important"), ("Spot Check", "Important"),
                            ("Supervision", "Important"), ("Probation Review", "Important")):
            result = {"match": True, "name": name.lower(), "confidence": 90}
            self.assertEqual(app.resolve_auto_review(kb, result), (name, group))
        # the narrower receipt page keeps its own (Other-tier) controlled name
        receipt = {"match": True, "name": "employee handbook receipt", "confidence": 90}
        self.assertEqual(app.resolve_auto_review(kb, receipt),
                         ("Other - Employee Handbook Receipt", "Other"))


if __name__ == "__main__":
    unittest.main()
