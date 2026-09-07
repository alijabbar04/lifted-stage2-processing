# Stage 2 AI workflow integration

This document describes the implemented handoff and automatic-review APIs.
The end-user roles are **AI Document Review** (`audit-review`) and **Improve
Stage 2** (`code-learning`). They share provider/account selection mechanics,
but keep separate workspaces, permissions, ledgers and request histories.

## Model catalog and selection

`src/stage2_ai_workflows.py` exposes `MODEL_CATALOG`, `model_keys_for_role()`,
`model_choice()` and `effort_choices()`. The catalog is shared: models are not
locked to one role. The current entries are:

| Model key | Provider / ID | Primary recommendation |
|---|---|---|
| `sol` | Codex / `gpt-5.6-sol` | Document review High; code learning High |
| `opus` | Claude / `claude-opus-5` | Document review High |
| `terra` | Codex / `gpt-5.6-terra` | Document review High |
| `astra` | Codex / `gpt-6-astra` | Document review High; code learning Medium |
| `fable` | Claude / `claude-fable-5-1` | Code learning High |
| `luna` | Codex / `gpt-5.6-luna` | Supervised/routine review High |

The provider is inferred from the model and displayed read-only. The UI uses
**Model → Account → Effort**: changing model refreshes compatible accounts and
advertised effort choices, while an incompatible prior selection is rejected.
`validate_selection()` rechecks the explicit provider model ID, account
identity and effort immediately before launch. No unavailable model, account
or effort is silently substituted. These rankings are practical starting
recommendations, not a Stage 2 benchmark.

The automatic document-review defaults are Sol / High, enabled after a
successful Accuracy Audit, with supported document corrections enabled. The
code-learning default is Fable 5.1 / High. The UI may save a different
explicit choice for the next run; changing global settings never mutates a
committed run snapshot.

## Account discovery and handoff

`discover_accounts()` reads non-secret Claude profile metadata and the current
Codex login, plus only Codex homes explicitly registered in Stage 2 settings.
It does not copy tokens, search for credentials, switch a global login or
perform automatic OAuth. `validate_selection()` uses provider auth/model
preflight and an optional expected identity; a failed or changed identity
blocks launch. Displayed account labels are hints until this live check passes.

`prepare_workflow(role, account, model_key, ...)` validates role authority,
source/assets paths, the exact completed report and selected scope, then writes
an immutable request directory containing the request, manifest, rule
snapshots and context. Preparation does not call a provider. A document-review
request cannot authorize code changes; a learning request cannot authorize
document renames. Publishing and installation are outside both roles.

```python
accounts = discover_accounts(codex_homes=registered_homes)
choice = model_choice("sol", effort="high", role="audit-review")
verified = validate_selection(account, "sol", expected_email=expected_identity,
                              effort="high", role="audit-review")
prepared = prepare_workflow(
    "audit-review", account, "sol", effort="high",
    audit_report=exact_completed_report,
    document_root=processed_documents,
    processing_root=original_files_folder,
    care_home=display_name,
    source_root=stage2_git_checkout,
    assets_root=bundled_ai_review_docs,
    completed_audit=True,
    allow_document_changes=True,
    review_all_flags=True,
)
```

`launch_workflow(prepared, interactive=True)` is the explicit/manual path.
`launch_headless(prepared, authorized_unattended=True)` is used by the
automatic controller. Both launch the selected CLI with a process-local
provider environment; VS Code is not the runner.

## Automatic review controller

`Stage2AutoReviewController` in `src/stage2_autoreview.py` owns the durable
state for live and batch processing. Its sequence is:

1. `capture_snapshot()` validates the run ID, roots, selected account/model/
   effort, source and rules, and captures exact rule bytes and hashes.
2. `commit_run()` persists the immutable snapshot. A later Settings change
   affects only a future run; resume uses the saved selection.
3. Record processing completion and the exact Accuracy Audit receipt with its
   report path/hash and final worker scope. Only an exactly complete matching
   audit arms automatic review; pending, stopped, failed or incomplete audits
   remain unlaunchable.
4. `maybe_launch()` performs pre-launch identity/model/effort revalidation,
   claims the run once, prepares the exact candidate queue and calls
   `launch_headless()`. No newest-file discovery or silent fallback is used.
5. `poll()` reconciles provider status, review outputs and ledger records.
   Completion means verified output and ledger reconciliation, not CLI exit
   alone. Ambiguous launch claims are retained and not automatically retried.

The controller distinguishes disabled, waiting, ready, running, completed,
completed-with-unresolved, failed, needs-attention, no-candidates and
cancelled states. `cancel_queued()` can cancel an unclaimed queued review.
`view_existing()` opens the request already bound to the run and never submits
another request.

## Visible live output

`src/stage2_live_output.py` provides `open_live_output(request_dir, ...)` for
the automatic run's existing event stream. The read-only viewer renders
assistant messages, tool/command activity, results, errors and attention state
from `provider-events.jsonl`, `provider-stderr.log` and `runner-status.json`.
It never submits a prompt, resumes an agent or cancels the supervised process;
closing the window leaves the run running. The window is live output, not an
interactive chat, and does not expose private internal reasoning. Its caption
identifies the role, model, effort and verified account identity without
storing credentials.

## Durable context and authority

Default context is under `%LOCALAPPDATA%\Lifted\Stage2\ai-workflows\`, with
separate `audit-review` and `code-learning` folders. Each request has its own
immutable manifest and rule snapshot; shared role memory is continuity only,
not authority over current evidence. The master review ledger and JSONL journal
remain the record of decisions. Hash-checked transactions and the existing
ranking rules govern document changes; learning changes require evidence,
regression tests and explicit code-change permission.

The processing API key/billing and subscribed Claude/Codex CLI account are
separate usage contexts. Account names or remembered settings do not prove
identity, entitlement or completion. See `docs/ai-review/WORKFLOW_GUIDE.md` for
the end-user evidence, naming, collision and learning rules.
