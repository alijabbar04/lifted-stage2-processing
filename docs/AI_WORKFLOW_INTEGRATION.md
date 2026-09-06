# Stage 2 AI handoff integration

Stage 2 can prepare either of two deliberately separate roles. The same role
uses the same rule snapshots and durable file-based context across providers;
it does not pretend that Claude and Codex share a native conversation history.

| Role | Claude Code | Codex | Default effort |
|---|---|---|---|
| Review a completed filename audit | Opus (`opus`) | Luna (`gpt-5.6-luna`) | high |
| Review correction evidence and improve code | Fable (`fable`) | Sol (`gpt-5.6-sol`) | xhigh |

These are explicit model choices, never automatic fallbacks. The installed
Claude CLI advertises Opus/Fable aliases; the selected Codex account's live
`model/list` response must advertise the exact selected model and effort.
Claude checks actual model entitlement when a run starts; local alias support
is not a claim that every Claude subscription has access. If it refuses, retain
the prepared request and choose a valid account/model explicitly.

## Account selection

`stage2_ai_workflows.discover_accounts()` reads the AI Account Manager's
non-secret `%APPDATA%\ClaudeAccountManager\profiles.json` registry. Claude
profiles are launched with a **process-local** `CLAUDE_CONFIG_DIR`; the default
`%USERPROFILE%\.claude` profile correctly leaves that variable unset. No login
tokens are copied, and no global default account changes are made.

This installed manager version's GPT panel uses the current Codex login, not
an additional Codex-account registry. Stage 2 lists that existing `CODEX_HOME`
and can also accept explicitly registered existing Codex homes. Registration
is metadata only: sign in to each home through the provider first. Stage 2
does not hunt for tokens or clone another account's authentication.

Before preparing and immediately before launching, verify the selected identity
using `claude auth status --json` or the Codex `account/read` API. A changed or
unavailable identity blocks launch. A name such as “Personal” is not sufficient
proof that a requested email address is signed in.

The launch terminal starts in the role's workspace with the exact request
prefilled. Claude receives `/stage2-review-audit "<REQUEST.md>"` or
`/stage2-review-learning "<REQUEST.md>"`. Codex receives the equivalent explicit
request-file prompt. The different slash name intentionally avoids silently
invoking an older globally installed `stage2-audit-review` skill.

An existing VS Code process may retain a different account environment when
another window opens. Therefore Stage 2 opens an account-isolated terminal;
it does not claim that the VS Code extension has switched accounts or received
the prompt. You can use the same request file from a correctly signed-in IDE.

## Stable integration API

The implementation is `src/stage2_ai_workflows.py` and has no Tk dependency.
Keep identity/model preflight off the Tk main thread.

```python
accounts = discover_accounts(manager_dir=None, codex_homes=[])
# Account: id, provider, name, config_dir, email, source; .label is for display.

prepared = prepare_workflow(
    "audit-review", account, "luna",
    audit_report=exact_completed_report,
    document_root=processed_documents,
    processing_root=original_files_folder,
    care_home=display_name,
    source_root=stage2_git_checkout,
    assets_root=bundled_ai_review_docs,
    completed_audit=True,
    allow_document_changes=True,
    review_all_flags=False,
)
# Preparing writes context/request artifacts but does not call an AI model.
process = launch_workflow(prepared)
```

The learning equivalent uses role `code-learning`, model `fable` or `sol`,
`allow_code_changes=True`, and the exact source checkout plus existing ledgers.
It rejects an installed-app folder in place of a Git checkout. An audit role
cannot authorize code changes; a learning role cannot authorize document
renames. Neither role is granted publishing or installation by these buttons.

The desktop persists Stage 2 settings in a namespaced dictionary:

```json
{
  "ai_workflows": {
    "workspace_root": "<optional existing/default workspace location>",
    "source_root": "<Stage 2 Git source checkout>",
    "ledger_path": "<existing Master_Filename_Review_Ledger.xlsx>",
    "codex_homes": [
      {"path": "<already signed-in isolated CODEX_HOME>", "name": "Personal", "email": "<expected email>"}
    ],
    "audit-review_model": "luna",
    "code-learning_model": "fable",
    "audit-review_account": "<last explicitly selected account id>",
    "code-learning_account": "<last explicitly selected account id>",
    "audit-review_expected_email": "<last verified audit-review email>",
    "code-learning_expected_email": "<last verified learning-review email>"
  }
}
```

Do not store access tokens or passwords in these settings. Remembered choices
are convenience defaults, not permission to skip the account/model check.

## Context, reports, and completion

Default workspaces:

```text
%LOCALAPPDATA%\Lifted\Stage2\ai-workflows\
  audit-review\
    AGENTS.md / CLAUDE.md / MEMORY.md
    requests\<UTC-time-and-id>\REQUEST.md / manifest.json / context\...
  code-learning\
    AGENTS.md / CLAUDE.md / MEMORY.md
    requests\<UTC-time-and-id>\REQUEST.md / manifest.json / context\...
```

Each request gets its own immutable initial rule snapshot and audit hash; older
requests are not overwritten. `MEMORY.md` is preserved, and both providers are
instructed to record only verified transferable lessons there. Native provider
chat persistence is additional history, not the cross-provider source of truth.

The Reports chooser should distinguish the **audit** (Stage 2's machine output)
from the **AI review ledger** (human/AI-reviewed decisions and improvement
history). The existing `C:\Lifted\Stage2 Audit Review\Master_Filename_Review_Ledger.xlsx`
and its sibling journal remain the continuity source where installed. The
`Misnaming Record.xlsx` correction index remains separately accessible. Never
create a blank master merely because a different provider was selected.

An unattended controller may explicitly call
`launch_headless(prepared, authorized_unattended=True)` and poll/wait its returned
`HeadlessRun`. This writes durable provider event/error logs and
`runner-status.json` in the request folder. It does not bypass all tool
permissions. CLI exit 0 means **outputs awaiting verification**, not proof that
the audit was checked or the software improved. Verify the transaction helper's
results, ledger reconciliation, and regression evidence before claiming success.

See `docs/ai-review/WORKFLOW_GUIDE.md` for the end-user process and the exact
review/collision/ranking/learning rules. The direct helper/API retains the original
confidence-greater-than-80 default. The desktop dialog explicitly includes all
flagged/error rows and passes `review_all_flags=True`; it does not silently claim
the lower-confidence cases were checked by a legacy high-confidence-only review.
