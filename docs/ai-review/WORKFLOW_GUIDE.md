# Stage 2: from an audit to better naming

The audit identifies possible mistakes. A document reviewer checks the actual pages and corrects supported mistakes. A separate software reviewer studies those decisions and changes the general naming rules only when the evidence justifies it.

## The four steps

1. **Finish processing and the accuracy audit.** Use the completed audit CSV or XLSX in Reports. A flag is a suggestion, not proof; a 100% progress bar means the selected work finished, not that every filename is correct.
2. **Review the documents.** Choose the review provider/account/model and the document-review action. Claude receives the new project command `/stage2-review-audit`; Codex receives an equivalent ordinary prompt. Both read the same `REVIEW_RULES.md`, `NAMING_RULES.md`, request, and role memory. They inspect the documents, explain Keep/Rename/Defer decisions, and prepare a checked repair plan. If a ranked category changes, every affected peer is assessed and the whole category is reranked. Authorized plans make backed-up, collision-safe moves.
3. **Keep the evidence.** Synchronize every decision and affected peer into the existing master review ledger and JSONL journal. Actual corrections and unresolved problems also go into the existing Misnaming Record. The review summary separates corrected files, dependent ranking changes, kept names, unresolved cases, and errors.
4. **Learn from the records.** Choose the software-learning action. Claude receives `/stage2-review-learning`; Codex receives an equivalent prompt. Both read `LEARNING_RULES.md`. They critically reassess the records, reproduce a general failure, add positive and negative regression tests, and propose or implement an authorized source change. A correct Keep may identify a false-positive audit problem. A mistaken human/AI correction is not a rule to copy.

## What is shared, and what is not

The document-review and code-learning roles have separate stable workspaces and memories. Within a role, changing provider or account keeps that role's workspace, rules, ledger references, and prior notes. Every launch has a new request directory; an in-flight request is never overwritten. Its manifest records the exact report, folder roots, rules hashes, permissions, provider, model, effort, and expected account. Memory helps continuity but does not override the current request or document evidence.

The launcher opens a process-local terminal session with the requested identity. It does not guarantee that an already-running editor extension has switched accounts. Check the provider's signed-in identity and selected model before continuing. If either differs, stop instead of silently substituting. The exact account, model, sequence and authority belong in each launch request, not these shared rules. Preparing this feature does not itself authorize spending or launching a review.

## Scope and practical limits

- The manual helper defaults to the established **confidence strictly greater than 80** rule. The desktop handoff requests **all flags**, including lower-confidence/error rows, and records that expanded scope explicitly. Always follow the current request. Neither selection is a claim to inspect every file.
- A completed report is needed. Do not review while processing, applying, or auditing is changing those folders. The helper also takes the same writer locks as Stage 2.
- Review inputs may be CSV or modern Excel XLSX. Legacy XLS is not supported by this helper; convert a copy first.
- Document inspection requires the review model's available PDF/image/document tools. The helper hashes and moves files; it does not read their meaning or verify whether the reviewer really looked at a page.
- Repairs require a valid configured source checkout, its `src/ai_review.py`, `src/Stage2_Processing.pyw`, and Python with openpyxl. The installed desktop app launches this workflow; it does not pretend the source helper is a standalone portable document-understanding service.
- If sources, audit, or worker files change after preparation, the plan is stale and must be regenerated. Never bypass that check.
- No paid evaluations, uploads, account substitutions, publishing, building, or desktop installation are implied by a document-review request. A learning request may authorize source changes and offline tests, but release/install needs its own recorded scope.

For exact review commands and evidence format, read `REVIEW_RULES.md`. For filenames, ranking, and folders, read `NAMING_RULES.md`. For existing ledger compatibility and learning statuses, read `REVIEW_RECORDS.md`.
