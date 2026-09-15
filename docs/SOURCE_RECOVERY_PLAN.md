# Source preflight and delayed recovery

Canonical baseline: main 665b254dc8530ebf8d80c234ec3e5ecd71635f46,
Stage 2 1.5.9, build 2026.09.15-evidence1. Existing untracked tmp/ is preserved
in the original checkout; implementation uses a separate Git worktree.

## Current flow

- Engine.run_batch_submit prepares/converts/flattens workers, discovers a frozen
  primary_inventory and submitted_worker_scope, then renders and sends chunks
  in one loop. Later locked/unreadable sources can follow earlier paid work.
- _record_primary_source_exception records provider-free hash-bound exceptions;
  _primary_excluded_ids prevents overlap with accepted/ambiguous requests.
- _resume_primary_inventory and recover_primary_submission already bind each
  POST to a durable submission_started marker, but render the tail lazily.
- run_batch_apply replays accepted results, manifests and finishing operations;
  complete workers are skipped. Partial worker records correctly fail finishing.
- assess_unresolved_finishing and _finishing_operation already retain bounded,
  explicit retry tokens, costs and attempts. Share-code output validates types
  but currently only asks for JSON in the prompt.
- BatchState supports version 5, single-writer Engine operations and terminal
  receipts. Source attention currently provides a shortened informational dialog.

## Implementation sequence

1. Add an independent local inspection/recovery module: full-page PDF validation,
   explicit causes, persistent hash-bound records, validated staging/archive,
   an append-only hash-chained ledger, checkpoint verification and restart checks.
2. Add masked-password/replacement/quarantine/reinstatement operations, preserving
   originals and refusing stale confirmations, accepted or completed sources.
3. Integrate whole-scope prepared evidence and a default wait gate into initial
   and resumed batch submission. Persist explicit ready-subset/supplemental
   scopes and submission markers; reuse accepted results and existing apply.
4. Add a recovery centre with full export, cause/worker grouping, local actions,
   cost/scope confirmation, delayed resume and independent finishing recovery.
5. Make partial completion, audit scope and terminal receipts truthful; preserve
   version-5 compatibility and reject unknown newer state schemas. Add structured
   share-code output and retain explicit bounded retry behavior.
6. Exercise synthetic encrypted/corrupt/missing/changed/blank fixtures, failure
   transitions, restarts, partial scopes, billing idempotence and normal flows.
   Inspect the diff for privacy, rollback and completion semantics.
7. After tests and review, bump version/build, build and verify executable and
   installer, publish the GitHub update/release and update the desktop app under
   the user's explicit deployment authorization in this task.

## Product choices

- Wait is the default. A complete inspection report can authorize a specific
  ready subset; unresolved sources remain visible and block full completion.
- No permanent-deletion feature is introduced. Quarantine is reversible and
  keeps exact source identities and hashes in the retained audit trail.
- Passwords stay only in local call memory, never in arguments or saved state.
- Development and tests use synthetic fixtures only; no live care-home data.
