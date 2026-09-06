# Review records and learning handoff

Retain the existing canonical master and journal, rather than creating a competing ledger for each provider:

```text
C:\Lifted\Stage2 Audit Review\Master_Filename_Review_Ledger.xlsx
C:\Lifted\Stage2 Audit Review\review_records.jsonl
```

Use the paths recorded in the request if configured differently. An arbitrary master XLSX basename is supported through `--ledger-path`; `--ledger-root` defines the sibling `review_records.jsonl` and shared writer lock. The legacy secondary record is the configured `Misnaming Record.xlsx`, normally under `%APPDATA%\DocReviewAIStation`.

## What gets recorded

The canonical **Review Log** preserves the established 28 columns: identity/run/time/care-home, source audit and hash/sheet/row/confidence, original and suggested filename/path, decision and approved/applied filename/path, notes/evidence, before/after hashes, actual apply status/time/error, and the six improvement fields. **Run Log** retains its existing 15-column schema. **Instructions** explains the workflow. The helper validates headers, preserves historical rows and existing learning decisions, extends filters/tables, and saves a verified replacement workbook with a backup of the previous workbook.

Every audit candidate gets an outcome. Every additionally inspected affected ranking peer also gets an outcome, even when its final name does not change. Companion entries have `Audit Sheet = Peer reranking`, row 0, and explicit dependency evidence; they are not fabricated audit flags. The append-only JSONL records use the established `review_outcome` format and Entry IDs to make repeated synchronization idempotent. A newly prepared review has a distinct run and Entry IDs; its stable journal `case_id` links a later reassessment of the same audit row without overwriting an earlier Defer or Keep. `REVIEW_DECISIONS.json` is a request-local copy; `REVIEW_TRANSACTION.json` is the file-operation truth and includes every affected path, hash, retained backup, and completion or rollback status. A `review_only` transaction contains proposed operations, not applied ones: check Rename Applied and Apply Outcome before counting corrections.

The secondary **Misnaming Record** retains exactly these ten fields:

```text
date_identified, care_home, worker, file, wrong_name, correct_name,
found_by, status, date_resolved, notes
```

Actual corrections and unresolved/error cases are appended there with an Entry-ID marker. Only completed corrections are `Resolved`; unresolved records are `Open - needs human check`. Keep decisions remain in the master/journal, where they can reveal false-positive audit behavior without falsely claiming a filename was corrected.

## Learning statuses are a separate judgment

New outcomes start at **Pending software review**, including Keep decisions. The software reviewer must inspect the evidence before deciding:

- **In progress:** a documented investigation or implementation is underway.
- **Implemented:** a justified general source change exists, required regression tests passed, and the version/commit plus test evidence are recorded. This does not itself mean the desktop app was rebuilt or installed.
- **No software change:** explain why the current rule is correct, the evidence is insufficient, the case is operational, or a proposed change would harm valid controls.
- **Duplicate:** point to the canonical Entry ID with the same root cause. Do not collapse distinct evidence merely because filenames look alike.

Learning may update only the improvement fields: Improvement Status, Improvement Version Or Commit, Improvement Reviewed By, Improvement Reviewed At UTC, Implementation Notes, Duplicate Of Entry ID. Preserve the original review decision, evidence, and actual apply history. Use the helper below: it holds the ledger's `.review_records.writer.lock`, appends a separate `improvement_update` event to the journal, and backs up and atomically saves the master. Retrying after a workbook-write failure reconciles the same event rather than duplicating it. Manual changes that conflict with a recorded event block synchronization for explicit reconciliation. Do not rewrite or remove historical journal lines.

```text
python <source_root>/src/ai_review.py update-learning --updates <request>/learning_updates.json --ledger-root <ledger_directory> --ledger-path <master.xlsx> --authorized
```

The input contains the actual reviewer and one update per Entry ID. `expected_status` must match the inspected ledger. `Implemented` also requires `version_or_commit` and `test_evidence`. `Duplicate` requires a different known `duplicate_of_entry_id`.

```json
{
  "reviewer": "<provider / exact model / verified account>",
  "updates": [
    {
      "entry_id": "<existing Entry ID>",
      "expected_status": "Pending software review",
      "status": "Implemented",
      "version_or_commit": "<verified source version or commit>",
      "implementation_notes": "<general root cause, changes and tradeoffs>",
      "test_evidence": "<actual regression command, result, positive and negative controls>"
    }
  ]
}
```

Missing documents, a partial transaction, unsynchronized rows, or a locked workbook are visible unresolved states—not successful reviews. Do not repeat an applied rename to recover a ledger write. Retry synchronization from the retained request artifacts instead.
