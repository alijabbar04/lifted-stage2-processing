# Stage 2 - Processing

## User guide | v1.4.1

Obsidian Compact edition. A practical guide to processing documents, checking the result, and turning verified corrections into better software.

Stage 1 gathers the documents. Stage 2 classifies, names and organises them. Stage 3 uploads the prepared folders. Completing Stage 2 does not itself upload anything.

> The important distinction: processing prepares documents; the post-run accuracy audit makes suggestions; an AI review checks those suggestions against the documents; a separate learning review decides whether the software should change.

### Your usual route

1. Choose the care home's **[Files]** folder and confirm the **[Processed]** destination.
2. Check Settings, start processing, and review the run estimate.
3. Let processing and any pending batch follow-up finish.
4. Let the optional post-run accuracy audit finish, then open **Reports**.
5. Use **Review audit...** to have Luna or Opus check the real documents and record decisions.
6. Use **Learn from corrections...** to have Fable or Sol assess justified software improvements.

### Find the right page

| Topic | Page |
| --- | --- |
| The new workspace and its controls | 2 |
| Folder setup and your first run | 3 |
| Live processing, batches and recovery | 4 |
| Reading progress and the post-run audit | 5 |
| Reports: audit versus review ledger | 6 |
| Launching the document review | 7 |
| Naming collisions, ranking and review quality | 8 |
| Learning from corrections and changing code | 9 |
| Settings and API usage | 10 |
| Discord and Telegram updates | 11 |
| Troubleshooting and completion checklist | 12 |

Edition: 7 September 2026. Examples are generic. This guide contains no account credentials or worker documents.

<!-- pagebreak -->

## 1. The Obsidian Compact workspace

The main screen keeps the essentials visible: the selected care home, the current phase, work completed, the current document, recent activity and three headline figures. Detailed information is still available when you need it.

| Control | What you use it for |
| --- | --- |
| Choose care-home folder | Select the folder containing worker subfolders, normally the care home's [Files] folder. |
| Start processing | Scan and confirm a new processing run. Read the estimate before submitting. |
| Check batch status | Check saved provider jobs and apply ready results. A saved batch may have more than one phase. |
| Stop | Request a safe stop. It is not a pause button or a promise of instant interruption. |
| Preview / Hide preview | Show or hide the current document without crowding the main work area. |
| Details & full log | Open the full activity log, detailed counters, and Folder & utilities. |
| Review audit... | Prepare an account-selected Luna or Opus document review. |
| Learn from corrections... | Prepare a Fable or Sol review of accumulated correction evidence. |

### The top navigation stays available

- **Jobs:** find saved jobs and their status, then check the relevant care-home run.
- **Reports:** choose the automatic audit or the AI review/correction ledger.
- **API Usage:** inspect Stage 2 and its workflow tools' recorded API usage.
- **Tools:** open the additional processing utilities available in this installation.
- **Guide:** open this guide.
- **Settings:** change processing options, limits and notification preferences.

### Fewer headline numbers, not less information

**Workers complete** counts finished worker folders. **Needs review** counts audit flags, not proven naming errors. **Estimated run cost** is an estimate based on recorded usage and the configured exchange rate, not an account balance.

The app icon appears beside Stage 2. The top ribbon is black. On supported Windows versions, the system caption bar containing minimise, maximise and close is black too; older Windows versions may use the operating system's dark fallback.

> Processing uses the Anthropic API key in Settings. The two external AI-review workflows use the Claude or Codex account you select. These are separate billing and usage contexts.

<!-- pagebreak -->

## 2. Folder setup and your first run

Choose the care home's **[Files]** folder, not a worker folder or a parent containing several care homes. Download cloud-only files first.

```text
Care Home/
  Care Home [Files]/
    Worker A/
    Worker B/
  Care Home [Processed]/
```

### Before starting

1. In **Settings**, confirm the Anthropic API key, classification model, worker/file/spend limits and maximum file size.
2. For normal handover, enable **Move processed workers to a destination folder** under File movement and select the separate **[Processed]** destination.
3. Enable PDF conversion as needed. LibreOffice supports Office-to-PDF conversion; without it, Word may become text-only and other Office files may remain unconverted. Neither route guarantees fidelity.
4. Enable **Accuracy audit after processing** under Post-run checks if wanted. Its additional API usage must fit the budget.
5. Choose **[Files]**, wait for the scan, then **Start processing**. Before confirming, check the mode, scope, warnings and estimate, including skipped or deferred files.

### What happens to the documents

Stage 2 converts, classifies, checks and organises documents using its controlled naming/routing rules. Finished folders normally contain **Overwrite Documents** and **Bulk**, with numbered upload batches.

Move mode moves a worker only after finalisation. An existing destination worker is treated as already done, not silently replaced. With Move mode off, the selected source folder is changed in place.

### Conversion is not a fidelity check

Preserve originals and compare important source pages/sheets with their PDFs. Conversion can lose ink/signatures, images, cells or fields, or misalign answers and labels. Fixed-height source rows may already hide content; splitting PDFs can lose form-widget appearances. Successful conversion/naming does not prove completeness. Repairing one file does not prove the general converter is fixed.

### Flattening is a utility, not a prerequisite

**Details & full log > Folder & utilities > Flatten folders only** moves nested files into the worker folder without AI calls. It changes organisation. Normal processing prepares its own files; do not flatten a pending run or repeat this utility just because it is waiting.

> Keep a recoverable copy of your original source set. Do not manually move, rename or replace files belonging to a pending batch or an active review.

<!-- pagebreak -->

## 3. Live processing, batches and recovery

### Live mode

The app processes documents while it remains open and online. You can follow the current document, preview and activity. Depending on Settings, an unknown document may prompt you to define it or be filed as a descriptive **Other** item for later checking.

Read an unknown document before defining it. A controlled compliance type belongs in the controlled vocabulary; miscellaneous material should not be turned into a new compliance type merely to avoid an unknown result.

### Overnight Batch mode

Batch mode submits work to the provider for asynchronous processing. The confirmation screen separates primary classification, follow-up allowance, required finishing work and the optional audit. Use that estimate rather than assuming every part of a batch run has the same price.

1. Wait for submission to finish and the app to report a saved pending batch.
2. When ready to continue, select the same care-home folder and press **Check batch status**.
3. Stage 2 checks the saved provider jobs. If results are ready, it applies them and continues the run.
4. Some results need a follow-up batch. Follow-up work can be split into several bounded requests; a large follow-up is not automatically an error.
5. Continue checking until the run's finishing work and enabled audit have reached a terminal result. A submitted batch is not a completed worker folder.

Only close the app when it has finished local work and says the batch is safely pending. Keep it open while it applies results, organises workers or runs the audit. Provider turnaround is variable; a quiet period alone does not mean failure.

### If a submission is ambiguous

An internet interruption can happen after the provider accepted a batch but before Stage 2 received its acknowledgement. Stage 2 then cannot safely assume whether that submission exists. The saved planned request and attempted submission are retained so the provider can be reconciled with the local state.

- Use **Check batch status** and follow the recovery explanation.
- Do not delete the hidden batch-state file or force a fresh live run to bypass the warning.
- Do not repeatedly submit the same work. Recovery must first identify any already accepted requests.
- If files were moved or modified since submission, restore the expected source or investigate the mismatch before resuming.

### Cache and stop behaviour

Content-hash records help avoid repeating settled work. They do not mean every finishing check, audit or intentionally forced reprocess is free. **Stop** preserves known completed work where supported; a request already in flight may finish first and can still be billed.

<!-- pagebreak -->

## 4. Reading progress and the post-run audit

The progress display describes the **current phase**. Processing, waiting for a provider batch and checking filenames are different phases; do not interpret one phase's percentage as completion of the whole pipeline.

### What the post-run accuracy audit does

When enabled and affordable within the remaining run budget, it re-examines the final eligible documents and compares their existing names with the classification evidence. Concerned cases can receive an additional opinion. It writes an audit report; it does not rename documents simply because it suggests a different name.

| Display | How to read it |
| --- | --- |
| 384 of 600 documents checked | The audit has recorded 384 outcomes, which can include errors. This is an illustrative count, not proof that every file was readable. |
| 64% | Work completed in that audit, not 64% naming accuracy. |
| Current document / current check | The document and step being worked on, including rendering or waiting for an AI response. |
| Elapsed waiting time | Time spent in the current wait. It explains a stationary bar without inventing progress. |
| About ... remaining - estimate | A timing estimate once enough useful progress exists. It can change. |
| Time remaining unavailable | There is not enough reliable evidence for an estimate, or the current wait makes one misleading. |
| Needs review | Flagged results to inspect; not a count of confirmed mistakes. |

### Completion, skipping and interruption

**Audit complete** means the audit finished its attempts and wrote the report. It can still contain errors, unreadable inputs or incorrect AI judgements. Open **Reports** and check statuses and Notes; do not treat completion as a passed accuracy test. The naming audit examines resulting documents, not a complete source-to-output fidelity comparison.

**Audit skipped** means it did not run, for example because it was disabled or the remaining budget was insufficient. Completed processing is not undone. Do not describe a skipped audit as passed.

**Stopped or failed** means the audit is incomplete. Do not submit a partial report as if every document was checked. Restarting the audit checks documents again and can consume additional API usage; the Stop button is not a pause/resume mechanism.

### If the bar stops moving

Check the current step, elapsed wait, recent activity and **Details & full log**. A slow model response is different from a recorded error. Do not repeatedly click Start, cancel and retry, or launch an external correction review while the app is still processing the same documents.

<!-- pagebreak -->

## 5. Reports: two different kinds of evidence

Press **Reports** to answer **Which report would you like?** The automatic audit and the review ledger are intentionally separate.

### 1 - Post-run filename audit

Choose **Browse audit reports**, select the right care home and run, then open its report. Stage 2's current output is **Filename_Audit_Report.csv**, with a number added when needed to avoid overwriting an older report.

Read the current filename, suggested filename, full file path, review status, confidence, reason and evidence together. A confidence score is the AI's assessment, not proof. Even a high-confidence suggestion must be checked against the actual document before a correction.

CSV output can have companion **Summary**, **Orientation** and **Tables** files. Keep the companions with the main report; they replace the separate tabs of the older Excel output. Use the main audit CSV, not a summary or tables manifest, when starting an AI review. Existing Excel **.xlsx** audits remain supported by the review workflow.

### 2 - AI review / correction ledger

Choose **Open AI review ledger** to open **Master_Filename_Review_Ledger.xlsx**. Luna or Opus records what it actually checked, what it kept or changed, what remains uncertain, and the evidence needed for later improvement work.

The ledger is shared across accounts, providers and runs. On an existing setup it remains in the configured Stage 2 Audit Review folder, beside **review_records.jsonl**, the recovery journal. Do not create a blank replacement merely because you switch from Opus to Luna or start another care home.

**Open legacy Misnaming Record** appears when that compatible correction index exists. It is useful history, but it is not a replacement for the master review ledger and journal.

### How the records relate

| Record | Question it answers |
| --- | --- |
| Filename audit | What did Stage 2 suspect after processing? |
| AI review ledger and journal | What did the reviewer verify, decide and actually correct? |
| Per-request review summary and change record | What happened in this exact review, with before/after evidence and outstanding cases? |
| Learning review and tests | Which software changes were justified, implemented and verified? |

> Close a workbook in Excel before a reviewer updates it. A locked workbook must be reported and reconciled; it must not be silently replaced or treated as successfully saved.

<!-- pagebreak -->

## 6. Review audit... - Luna or Opus

This step independently checks the audit against the real documents. It is not another blind rename pass. The two available choices are **Luna - Codex, high effort** and **Opus - Claude Code, high effort**. These are configured review choices, not performance guarantees.

### Prepare the handoff

1. Wait until Stage 2 and the exact post-run accuracy audit finish. Click **Review audit...**.
2. Choose the model and a matching provider account. Use **Expected account email** when you want a specific signed-in identity checked.
3. Confirm the completed main audit, care-home name, audited document folder, original **[Files]** folder, master ledger and Stage 2 source checkout. Every review needs the current helper and naming implementation to prepare its queue and records, even without corrections.
4. Confirm **The selected post-run accuracy audit has finished**. File existence alone is not confirmation.
5. Decide whether to allow evidence-backed filename corrections. Without that permission, the review must remain a review/proposal, not apply changes.
6. Press **Verify & prepare**. The app checks account identity and model support and writes the exact request. Preparing is not starting an AI task.
7. Read the verified account and handoff details, then press **Open selected AI**. **Copy command** and **Open context** are available too.

### What opens

An account-isolated terminal opens in the persistent audit-review workspace. Claude receives the prefilled **/stage2-review-audit** command and the exact request-file path. Codex receives the equivalent request-file prompt. Both are directed to the same role rules, naming rules, records and shared file-based memory.

The launcher does not claim to inject text into an already open VS Code extension. Existing IDE windows can retain a different account environment. The isolated terminal avoids silently using that other account; you may open the same request from a correctly signed-in IDE yourself.

### What the reviewer must do

The desktop handoff includes all flagged/error rows, including lower-confidence cases. The reviewer must inspect the actual evidence, keep correct names, correct only justified cases when allowed, and defer unresolved cases honestly. Encrypted, blank or incomplete evidence must not receive invented scores, dates, signature claims or a successful-review label. A file's name or a confident earlier judgement is not proof it was readable. Use the supplied helper to plan/apply changes, maintain the ledger/journal and reconcile outputs.

The older manual command may use a confidence-greater-than-80 queue. Follow the scope written in this exact request, not an assumption from an older session. Neither a launch notification nor a terminal opening proves the review finished.

<!-- pagebreak -->

## 7. Naming collisions and a trustworthy review

Official naming and routing rules come from the Stage 2 implementation and the supplied naming-rule snapshot. The reviewer should not invent a near-match, shorten a type arbitrarily or change rules to fit one example.

### Ranked families: check every peer

Some document families use their numbered suffix to express order. In those families, **the higher number is the preferred copy**. Simply adding the next number would incorrectly promote a corrected document without comparing its quality with the existing copies.

When a correction enters or changes a ranked family, the helper requires evidence and scores for **all peers in that worker's affected family**, not only the colliding file. It then plans the entire order using the actual Stage 2 rules. Readability, completeness, signing and actual document dates can matter according to the family.

```text
Illustrative ranked order:
  Document Type.pdf       lower-ranked copy
  Document Type (01).pdf  next copy
  Document Type (02).pdf  preferred copy
```

This illustration explains suffix order, not a universal instruction to create three files or to rank every document type.

### Unranked families: avoid collisions without implying quality

For an unranked family, a collision may use the next available number. The reviewer must still preserve the correct controlled name, extension and routing. It must not overwrite an existing document to make its proposed name fit.

### Dates, source bytes and evidence

- Use the date evidenced by the document where the naming rule calls for it, not the file's modified date or today's date.
- Do not assume a signature is present just because a filename says signed.
- The transaction helper checks the saved inventory and hashes, locks the relevant folders, creates backups and plans collision-safe changes.
- If a source changed, a peer is missing, a folder is busy or the evidence is incomplete, stop and resolve the mismatch. Do not bypass the helper with ad-hoc filesystem commands.
- A filename correction does not authorize deleting, rewriting, merging, splitting or rotating the underlying document.

### What a completed review should leave

Look for a review summary, recorded decisions, applied-change evidence where applicable, a reconciled ledger/journal and a clear list of unresolved cases. “Nothing needed changing” is valid if supported. “The model exited successfully” is not sufficient verification.

<!-- pagebreak -->

## 8. Learn from corrections... - Fable or Sol

This is a separate reviewer with a different job: critically inspect the accumulated review evidence and decide whether a general software change is warranted. It may find that a previous correction was wrong, too specific to generalise, or not caused by a code defect.

The available choices are **Fable - Claude Code, xhigh effort** and **Sol - Codex, xhigh effort**. The launcher checks the chosen account and supported configuration. It never silently replaces an unavailable model or account.

### Launch the learning review

1. Finish and reconcile the document review first. Do not send an unfinished spreadsheet as settled evidence.
2. Click **Learn from corrections...** and choose the model and provider account.
3. Confirm the existing master ledger and the actual Stage 2 **Git source checkout**, not the installed application folder.
4. Keep the same persistent shared-context root unless you deliberately want a new context location.
5. Leave code-change permission unticked for a recommendation-only review, or allow verified source-code changes with regression tests and quality checks.
6. Press **Verify & prepare**, check the handoff, then **Open selected AI**.

### The AI's required sequence

- Reconcile the ledger with its journal and read prior verified lessons and unresolved cases.
- Review the evidence behind the previous reviewer's decisions; do not blindly trust every renamed file.
- Find a reproducible cause in naming, classification, ranking, routing or another in-scope function.
- Consider unaffected document types and difficult counterexamples before broadening a rule.
- Add a regression test for the defect and run relevant neighbouring tests.
- Make the smallest justified general change only when code changes are allowed.
- Record exactly what changed, what tests passed, what remains uncertain and whether further evaluation is needed. Do not mark evidence resolved just because a patch exists.

### Context is shared deliberately

Opus and Luna share the **audit-review** role folder. Fable and Sol share a separate **code-learning** role folder. Each role has **MEMORY.md** plus an exact request folder containing rule snapshots and manifests. Both providers read those files; their native chat histories remain separate.

Changing accounts does not erase the ledger or role memory. Changing roles does not give the learning reviewer permission to rename care-home documents. The button does not authorize publishing a GitHub release or installing a new desktop build; those are separate, explicitly managed release steps.

<!-- pagebreak -->

## 9. Settings and API Usage

Processing settings affect the main document-processing engine. Choosing Luna, Opus, Fable or Sol in an external-review dialog does not change the engine's classification model.

| Setting | Practical effect |
| --- | --- |
| Anthropic API key | Supplies processing API access. The normal installation uses the operating system credential store; leaving a replacement field blank retains the existing key. |
| Model / advanced models | Changes the classification model used by the processing engine. Keep an intentional, tested setting; this guide does not replace the in-app model/cost information. |
| USD to GBP rate | Converts recorded usage into the displayed GBP estimate. |
| Worker, file and spend limits | Bound a run. Review which limit was reached before changing it or continuing. |
| Skip files larger than (MB) | Leaves over-limit files unsent and reports them. It is separate from provider batch-payload chunking. |
| Image resolution / adaptive pages | Changes what page evidence is sent and when. Lower cost is not automatically equivalent evidence quality. |
| Skip API when clearly identified | Uses a shortcut when filename/text appears sufficient. Understand the accuracy tradeoff before enabling it. |
| Auto-file unrecognised as Other | Avoids live definition prompts; unresolved documents still need attention. |
| Local page orientation | Off, audit-only/shadow mode, or optional high-confidence correction. An orientation flag is not itself a proven rotation error. |
| PDF conversion | Converts supported formats; LibreOffice enables supported full Office conversion, but does not guarantee content/layout fidelity. |
| File movement | Moves finished workers to the chosen destination, or processes in place when disabled. |
| Accuracy audit after processing | Runs the optional report-only check, subject to remaining budget and successful completion. |
| Notifications... | Opens Discord and Telegram delivery settings. |

### Understanding API Usage

Open **API Usage** from the top navigation. The combined view includes Stage 2 processing and its integrated workflow tools, with scope controls for the included tools. Other pipeline applications have their own usage pages.

Use the tracker to inspect recorded usage over time and by the available scopes/models. The main screen's run estimate is a convenient summary, not a provider invoice, prepaid balance or proof that every external subscription session was captured.

Claude Code and Codex review sessions use the selected subscription/account. Their account quotas and session limits are separate from the processing API key. Check AI Account Manager or the provider's own account view for those limits; do not assume the Stage 2 API cost figure includes them.

<!-- pagebreak -->

## 10. Discord and Telegram updates

Open **Settings > Notifications...**. Enable Discord, Telegram, both or neither. Notifications report meaningful lifecycle events; they do not upload reports, attach documents or start an AI review automatically.

### Configure and test

1. Enable the channel you want and enter its exact destination: a Discord channel ID or a Telegram chat ID/channel.
2. Reference an existing local credential file and its token variable, or enter a new bot token for storage in the operating system credential store.
3. Leave **New bot token** blank to keep the existing credential. A saved OS credential takes precedence over environment/file references.
4. Choose whether to include aggregate progress updates and how long a wait should be before its first update.
5. Press **Save settings**, then **Test enabled channels**. Wait for the actual delivery result; “queued” is not “delivered”.

An existing Claudius Messenger configuration can be referenced locally. Bot tokens must not go into source code, screenshots, public installers or this guide. Telegram requires an explicit destination; the app does not guess who should receive the messages.

### What messages mean

| Event | When it is reported |
| --- | --- |
| Run/phase starts | Preparation, processing, organising, audit and other evidenced phase changes. |
| Batch or follow-up submitted | Request/batch counts, with a reminder that submission is not completion. |
| Worker completion | Aggregate completed-worker count, without names. |
| Progress | Optional 25%, 50%, 75% and 100% milestones, throttled to avoid a stream of tiny updates. |
| Extended wait | After the configured interval, then at most every 30 minutes; waiting alone is not labelled a failure. |
| Attention needed / failure | A brief controlled reason; open the app for the full details. |
| Audit finished | Checked and flagged counts, with a reminder to inspect Reports. |
| AI review launched | The selected review phase started; it does not claim the external AI has finished. |
| Run finished | A summary that distinguishes completed, skipped, disabled or failed audit status. |

Messages use aggregate information, not worker names, document text, filenames, full paths, prompts or credentials. Duplicate events are suppressed where possible.

Delivery is best-effort. A channel failure does not stop document processing or the other channel. Check the in-app delivery result when a test fails. Unsent updates can be lost when the app closes; provider retries can occasionally duplicate a message. The application records and reports remain the authoritative run evidence.

<!-- pagebreak -->

## 11. Troubleshooting and completion checks

| What you see | What to do |
| --- | --- |
| A pending batch on this folder | Check batch status for this saved run; do not submit it again. |
| Ambiguous batch submission | Reconcile saved state with provider jobs through recovery. Keep the state files and original inputs intact. |
| A limit or insufficient remaining budget | Read the estimate/status and decide whether to change the relevant limit. A skipped audit is not a passed audit. |
| Cloud-only or missing files | Make the files available locally, then retry the appropriate unfinished work. |
| A file was changed or moved after submission | Restore/reconcile it before recovery or review. Do not force a stale plan to apply. |
| Review cannot open while Stage 2 is busy | Wait for processing, recovery and scanning to finish. The current run has not been interrupted. |
| Wrong account, unavailable model or unsupported effort | Open the intended account in AI Account Manager/provider, verify sign-in and select explicitly. No fallback is chosen for you. |
| Review requires a source checkout | Select current Stage 2 source: the transaction helper and naming code. |
| Workbook is open / records could not save | Close the workbook, then reconcile the retained journal and outputs. Do not overwrite the ledger. |
| No notification arrives | Check enabled channels, destination, credential reference and the actual test result. Processing may still be succeeding. |

### Before Stage 3 uploads

- Confirm all intended worker folders completed, including any follow-up work.
- Review skipped, unknown, unconverted and error cases in **Details & full log**.
- Compare key source/output content; preserve originals and escalate loss. Renaming cannot fix conversion damage.
- Confirm whether the optional accuracy audit completed; do not infer it from a full processing bar.
- Check and reconcile the external document-review result if you launched one.
- Confirm Stage 3's processed folder and roster; resolve roster-refresh warnings before assuming upload readiness.

### Before calling a software improvement finished

Check the learning report, evidence, tests and uncertainties. Code edits or terminal success do not update the desktop app: verify the build, installer, installed executable, shortcut and GitHub release separately.

### Further reference

In the source checkout, see **docs/ai-review/WORKFLOW_GUIDE.md**, its review/naming/learning rules, **docs/TROUBLESHOOTING.md**, **docs/NOTIFICATIONS.md**, **docs/AI_WORKFLOW_INTEGRATION.md** and, for vocabulary changes, **docs/VOCABULARY_GUIDE.md**.

Update **docs/USER_GUIDE.md**; run **tools/build_user_guide.py** and check pages/contents. Builds only bundle the PDF: release both. **docs/INSTALL.md** lists tests and four checksum artifacts.
