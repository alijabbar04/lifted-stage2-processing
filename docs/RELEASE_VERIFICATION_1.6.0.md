# Stage 2 v1.6.0 verification and recovery handoff

Build: `2026.09.15-source-recovery1`.
Canonical baseline: `main` (full commit identity below),
version 1.5.9 / `2026.09.15-evidence1`, not the older installed-source snapshot.
Exact baseline: `665b254dc8530ebf8d80c234ec3e5ecd71635f46`.
Implementation branch: `feat/source-recovery-preflight`, isolated worktree.
The canonical checkout's unrelated untracked `tmp/` was preserved.

## Root cause and changed paths

The old primary loop rendered and submitted chunks together. A later locked
source could therefore be discovered after earlier documents were purchased.
The preserved incomplete-worker check was correct; discovery and operator
recovery needed to happen earlier.

| Area | Change |
| --- | --- |
| `Engine.run_batch_submit`, `_resume_primary_inventory` | Whole selected scope is discovered, fingerprinted, inspected and prepared locally; no automatic POST follows preflight. Explicit ready-subset confirmation is separate. |
| `stage2_source_recovery.py` | Encryption-first inspection; per-source causes; restricted staging; verified original archives; masked-password library integration; exact replacement/quarantine/reinstatement; immutable hash-chained ledger and scope tokens. |
| `stage2_source_recovery_ui.py`, compact dashboard | Complete grouped queue, delayed password sessions, costs, explicit partial choices, exports, independent finishing access and positive-match reconciliation. |
| `submit_ready`, `_submit_followup_batch`, `run_batch_apply` | Exact saved scopes and pre-POST markers; accepted-content reuse; renamed alias replay; multiple follow-up sessions; prior actual cost accounting before the next budget decision. |
| `_source_completion_policy`, `BatchState.finalize_applied`, audit | Distinct `completed_with_exclusions`, exact retained receipt/report, archive verification, affected workers excluded from audit and automatic review blocked for partial scope. |
| `ClaudeAPI.share_code_check`, `_finishing_operation`, `batch_finishing_bridge.py` | Schema-constrained JSON and strict types/date validation; safe diagnostics; only explicit, bounded, hash-bound failed retries; ambiguous attempts never automatically repeated. |
| Progress and notifications | Local inspection is not processing; completed-worker counts remain separate; partial completion is not a normal success notice. |

Provider JSON schema wiring follows the official
[Claude structured-output documentation](https://platform.claude.com/docs/en/build-with-claude/structured-outputs).
Local validation remains necessary; no live provider call was used in development.

## State, scope and privacy

Native batch schema remains **5**. Source recovery adds a version-1 subrecord,
complete source records, explicit scopes and an append-only hash-chained ledger.
Initialization retains a pre-recovery backup. Unknown newer native/recovery
schemas and unreadable state are rejected. A legacy run without a saved complete
inventory cannot silently become an empty approved recovery scope.

Checkpoints are reopened and compared with expected content; a writer's return
value is not evidence of persistence. Completed workers and accepted/uncertain
request identities remain immutable. A submitted-but-unacknowledged scope needs
an exact positive provider match, not an empty provider listing, before adoption.
For budget-limited runs, a later source supplement waits until earlier accepted
primary and follow-up batches have verified actual-cost accounting markers.
Apply accepted results first; a legacy run can populate those markers on resume.
Pending batch costs cannot silently be treated as zero for another purchase.

Passwords are passed directly to PyMuPDF, not to a subprocess, provider or logger.
UI fields are masked and cleared; credentials are not in state, exports or release
assets. Password-free evidence is deliberately retained locally in a restricted
per-run directory. Python cannot guarantee physical RAM zeroization or protect
against an administrator/OS memory dump: this release does not claim otherwise.

No permanent-delete control was added. Quarantining requires a verified retained
copy and an exact confirmation. A missing original can be replaced, but is
explicitly reported as unavailable rather than falsely claimed to be archived.
Signed PDFs are withheld for separate review because decryption can invalidate
signatures. Local filesystem/hash checks fail closed on unexpected source drift.

## Verification

All fixtures are synthetic. No live care-home documents, rosters, source states
or provider batches were modified. No live processing was run.

Executed commands (from repository root; Python 3.13 on this machine):

```powershell
python -m pytest -q -rs --tb=short --basetemp .test-v160-final -p no:cacheprovider
python -m compileall -q src
git diff --check
python tools/build_user_guide.py --render-dir .test-guide-final --pdftoppm <Poppler-pdftoppm.exe>
.\build\build.ps1
.\build\build_public_installer.ps1
.\tools\smoke_test_frozen_app.ps1 -ExePath '<installed executable>'
```

Final test/build/install results are recorded below after release verification.
Focused verification after the final receipt guard: 80 tests passed in 51.57s.
An earlier full run loaded the source before the last pre-POST boundary change:
754 passed, 1 skipped and 201 subtests passed; its new payload-mutation regression
failed on that stale imported module. The boundary suite passed after the change,
and a fresh full-suite process was required for final verification.
Final review also added a guard against partial-success UI/notifications before
the audit/terminal boundary and a finite-budget settlement gate for supplements.
The next full process passed 757 tests, skipped 1 and passed 201 subtests in
263.80s. Final budget/notice/original-archive guards received separate focused
coverage before the final release suite. The budget group passed 68 tests and
17 subtests; related recovery UI passed 18 tests and 9 subtests. Partial-notice
and engine tests passed 36 tests and 31 subtests. The archive/fault matrix passed
51 tests after extending the out-of-scope spy to allow verified in-scope reads.
The 16-page guide was rendered with Poppler and all pages visually inspected.
Poppler's missing optional Symbol/ArialUnicode font warnings did not cause
visible clipping or missing text in the rendered guide.

Focused coverage includes 98 encrypted PDFs / 40 workers with zero POSTs;
correct, wrong, blank and mixed-password groups; delayed sessions and reopening;
49 completed worker records and contents unchanged; exact accepted-hash reuse;
explicit subset, duplicate aliases and `reprocess=True`; state/confirmation drift;
replacement archives and atomic activation; quarantine/reinstatement and corrupted
archive refusal; checkpoint interruption; schema-5 migration and newer rejection;
uncertain primary/supplemental/final-check requests; durable costs and repeat
follow-ups; malformed share-code replies and bounded retries; real partial audit
CSV summaries; terminal-receipt verification and hidden Tk queue/control tests.

The additional 45-test fault matrix includes a real approximately 21 MiB AES-256
PDF, a valid zero-page PDF, two explicitly injected post-decryption validation
failures, 36 before/after checkpoint interruptions across preflight, unlock,
replacement, quarantine and reinstatement, and five checkpoint-count contracts.
The damaged-after-decryption cases are controlled fault injection, not a claim
that all possible real PDF corruption patterns were reproduced.

## Manual QA and residual risks

- On a synthetic folder, start Overnight Batch: preflight must finish with zero
  POSTs and remain paused. Check the complete queue and cost estimate.
- Close/reopen with locked sources; try a wrong password then a mixed group.
  Only successful files should become newly validated, without storing passwords.
- Confirm a ready subset, apply accepted results, unlock a later subset and
  confirm only that supplement. Check request and cost history remains cumulative.
- Quarantine one unresolved file and inspect the exact partial statement in the
  UI, retained receipt, exported report and audit summary. Check no automatic
  review starts. Reinstate only while that worker remains incomplete.
- Simulate uncertain acknowledgement: no automatic retry. Reconcile only with
  exact provider evidence. Never resubmit the whole original batch.
- Failed final checks have their own estimated, explicit retry; ambiguous checks
  remain blocked. Completion must not imply document-content accuracy.

Tests exercise controlled filesystem failures, not every possible hardware,
cloud-sync or operating-system fault. Keep backups and avoid external edits to
pending sources. Provider behavior was mocked: account permissions, current
service availability and live schema acceptance remain operational checks.
No live document interpretation accuracy or end-to-end platform upload is claimed.
The recovery queue's GUI was tested at 1100x680 and 1100x760; unusual DPI/screen
configurations may require resizing. A live production recovery was deliberately
not bundled with this application update.

## Deployment and rollback

The user's direct request explicitly authorizes updating this desktop app and
GitHub after verification. Build from reviewed source with the public,
credential-free installer only; never use the private key-embedding builder.
Verify executable and guide SHA-256 after installation, actual window version,
desktop shortcut target and uploaded release asset checksums.

Preserve the previous executable before installing. Application-only rollback
can restore that exact backup while the app is closed, leaving credentials and
documents untouched. **Do not run an older app against a newly created recovery
state.** Preserve active state, terminal receipts and recovery archives together;
restoring a pre-recovery state after paid requests could lose acceptance history
and must not be used as a shortcut to resubmission. Prefer a forward fix for a
partially purchased recovery run. Other machines should install the v1.6.0 public
release rather than copying state or credentials from this machine.

## Final release results

- Final source suite: **773 passed, 1 skipped, 204 subtests passed** in 263.77s.
  The skipped pre-existing symlink test requires permissions unavailable to this
  account; the separate real Windows junction rejection test passed.
- `compileall` and `git diff --check`: passed.
- Guide: 16 pages, 89,291 bytes; final page 16 inspected after the budget note;
  rendered pages 1-15 were byte-identical to the already inspected images.
- Reviewed implementation commits: `fa0d1a4`, `0b112ba`, `e82fba9`.
- PyInstaller 6.22.1 / Python 3.13.14 executable build: passed; both new recovery
  modules confirmed in the archive. Built executable: 86,837,694 bytes.
- Credential-free Inno Setup 6.7.3 installer: passed, 87,952,679 bytes.
- Built and installed executable smoke tests: passed, actual window title
  `Stage 2 — Processing v1.6.0 | 2026.09.15-source-recovery1`; pinned per-user
  extraction also verified with Windows Temp supplied in the environment.
- Public installer exit code: 0. Installed executable SHA-256 matches the build.
  Desktop shortcut targets the standard per-user installed executable. All three
  installed guide copies match the packaged PDF. No existing app process was
  interrupted; existing settings/credential storage were not installer targets.
- Previous executable retained in the installation directory as
  `Stage 2 - Processing.exe.bak-v159-before-v160-20260915`, verified SHA-256
  `93250d8d92a5fc9c1eabef4bf8d249bdc0eb664b3db022677e2ef971fb1ff118`.
- Release destination: [v1.6.0](https://github.com/alijabbar04/lifted-stage2-processing/releases/tag/v1.6.0).

Final artifact SHA-256 identities (also supplied in `SHA256SUMS.txt`):

| Artifact | SHA-256 |
| --- | --- |
| `Stage2_Processing.exe` | `8f666c6dad4ee3ff93991eab1ed04e1faabee01a1218afce0984c7a508b543eb` |
| `Stage2_Processing_Setup.exe` | `d41c38067deff6df2577c2f2c8f837aa9928970a41436b29b27c9a441a6e1796` |
| `Stage2_Guide_AI_Processing.pdf` | `cb73947835f5a3e805de0847d111156c2fc34f1600f8ca4be5c7e127977a63b2` |
| `inference.onnx` | `af9a0a4f317ff0709ce752067807f819cb15d883f8ecad89f28df1c6ee2d9c92` |
