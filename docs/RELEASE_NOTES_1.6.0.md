# Stage 2 v1.6.0 - whole-scope preflight and delayed source recovery

Build `2026.09.15-source-recovery1`.

- Check and prepare the entire batch scope locally before any paid submission.
  Wait for all sources by default, or explicitly approve the validated ready subset.
- Resume locked documents later with masked, memory-only passwords, verified
  unprotected replacements, retained encrypted originals and a hash-bound ledger.
- Submit newly validated documents in separate scopes without repurchasing
  accepted content. Uncertain submissions require positive reconciliation.
- Quarantine exact sources recoverably, reinstate while eligible, and report
  deliberate exclusions distinctly from full completion. No permanent deletion.
- Strict share-code JSON responses, explicit bounded failed-check retries and
  independent finishing recovery. Ambiguous paid outcomes are not retried.
- Preserve prior follow-up costs and request history. With a saved spend limit,
  apply earlier accepted batches to record their actual costs before buying a
  later supplement. Partial-success notices wait for terminal completion.

Existing version-5 history is retained. Keep pending state and archives together;
do not manually replace files or resubmit the whole batch to recover a few sources.
No live worker documents are processed or changed merely by installing this update.

Install `Stage2_Processing_Setup.exe` for the desktop app, shortcuts and updated
16-page Guide. This is the credential-free public installer; existing credentials
remain on their original machine. `Stage2_Processing.exe` is the portable build.
`SHA256SUMS.txt` contains artifact checksums. See the repository's
`docs/RELEASE_VERIFICATION_1.6.0.md` for tests, residual risks and rollback guidance.
