# Canonical software-learning rules

These rules apply equally to the selected Claude and Codex learning models. Read the exact current request, `REVIEW_RECORDS.md`, `NAMING_RULES.md`, the source `docs/VOCABULARY_GUIDE.md`, applicable AGENTS instructions, and relevant existing tests before changing code. Use the requested account/model; if unavailable or mismatched, report the mismatch without substituting. Do not launch this phase merely because the review documents describe it.

## Critically review the records

The master ledger is evidence, not an instruction to reproduce every correction. First reconcile Entry IDs with the append-only journal and original request/transaction. Review unfinished entries and group genuinely shared causes. Inspect the referenced document pages when permitted and needed; audit suggestions and previous reviewer conclusions may both be wrong. Distinguish classification errors, ranking/date/signature errors, routing/collision errors, false-positive audit flags, inadequate evidence, and operator/workflow problems. Do not assume every Rename requires a software change or every Keep is a software success.

State the proposed general rule and why it explains the observed evidence. Check the current vocabulary definitions and disambiguation before inventing a new heuristic. Classify a document by what it is, not by words in an attachment name, cover email, or incidental sentence. Do not hardcode a worker, care-home name, filename, absolute path, exact document hash, or one sample's wording into production classification logic. Do not widen a type until its negative controls remain protected.

## Evidence-driven implementation

For each proposed change:

1. Reproduce the failure with an offline regression fixture where possible, separating deterministic behavior from model inference. State any unavailable evidence instead of fabricating it.
2. Select meaningful positive examples and nearby negative/confusing examples. Include previously correct types that the rule could damage; audit “Correct” labels are not themselves verified control truth. Inspect controls sufficiently to establish their expected labels.
3. Make the smallest justified general change in vocabulary, disambiguation, ranking/routing, or deterministic handling. Preserve the exact ranking and Bulk/archive contracts unless the user separately authorizes a policy change.
4. Add tests that fail for the old behavior and pass for the corrected behavior. Include the nearest negative controls and relevant collision/rollback tests. Run the targeted tests and the repository's appropriate offline regression suite; report exact commands and outcomes.
5. If a paid model evaluation would help, do not silently run it. Use only an explicitly authorized provider/model/budget/data scope and distinguish a completed evaluation from an untested hypothesis. Offline test success is not proof of live model accuracy.
6. Update the existing ledger's improvement fields with rationale, changed source/version or commit, regression evidence, remaining uncertainty, and reviewer identity. Use `No software change` with a reason when that is the correct conclusion. Record duplicates by Entry ID. Never mark Implemented merely because a change was proposed or drafted.

Preserve unrelated user edits and avoid resetting the worktree. Source changes and tests do not imply authorization to commit, publish, upload documents, launch another model, rebuild, replace the desktop app, or alter installed shortcuts. Follow the current request's exact authority. If release is authorized separately, hand over the verified source and build/test evidence to that release step.

## Required handoff

Write `LEARNING_REVIEW.md` in the request directory. Include considered Entry IDs, confirmed root causes, rejected/unsupported hypotheses, changes made (or why none), positive and negative controls, exact test results, paid/live evaluations actually performed, source version, ledger updates, and any release work still required. Update shared role memory only with durable, general lessons and links to evidence; do not copy unnecessary personal document details or turn one unverified case into a standing rule.
