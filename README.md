# Stage 2 — Processing

## v1.5.2 Obsidian / Jade ranking recovery — build 2026.09.08-ranking1

The current UI direction is **Graphite + Deep Jade**: a near-black/graphite
surface with restrained jade accents, real filled/outlined buttons, a clean
idle screen and progressive panels while work is active. The existing
**Jobs, Reports, API Usage, Tools and Guide** navigation remains available.

The three post-processing actions have separate meanings:

- **Accuracy Audit** finds possible filename or evidence problems and writes a
  report. It does not rename documents.
- **AI Document Review** checks that report against the actual document pages,
  records Keep/Rename/Defer decisions, and applies supported filename
  corrections only when explicitly enabled.
- **Improve Stage 2** studies the review ledger and evidence to decide whether
  a general software change is justified. It is separate from document review
  and never receives code-change authority merely because corrections are on.

For AI review, choose **Model → Account → Effort**. The provider is inferred
from the model and shown read-only (Sol/Terra/Luna/Astra use Codex; Fable/Opus
use Claude). Changing model refreshes compatible accounts and efforts; an
incompatible previous choice must be selected again. The default document
review setup is **Sol / High**, with corrections and automatic review enabled
only when the completed Accuracy Audit permits it. The app snapshots the exact
model, account identity, effort, scope and authority at Start; later global
changes affect the next run only.

Automatic review runs only after processing, follow-up work and a complete,
durable audit receipt. It must not select the newest report by filename, launch
from a partial/pending/error audit, submit twice, or silently substitute an
account/model. A visible desktop terminal or log viewer shows the existing
supervised CLI stream (assistant messages, tool activity, results and attention
requests where available); it is not promised to expose private reasoning and
must not be labelled an interactive chat when it is only live output. **View AI
session** reopens that existing stream and does not create a second job.

The processing API key/billing and the subscribed Codex or Claude CLI account
are separate. Sign-in may require the user; there is no automatic OAuth or
silent personal-account fallback. VS Code is optional and is not the runner.
The shipped CLI minimum is Fable 5.1 version 2.1.251; the currently updated
CLI is 2.1.263. Fable 5.1 / High is a code-learning recommendation, while
Astra / Medium is a provisional complex-case alternative—not a benchmark
claim—and filename corrections must never trigger automatic code changes.

### Windows batch-folder lock cleanup

Stage 2 keeps the same `.docreview_batch_writer.lock` path while batch work is
active. On Windows, after unlocking and closing the writer, it makes a
best-effort removal attempt. Another standard Python holder can keep the file
open, leaving a harmless remnant for the next successful use. POSIX and
request/ledger locks remain permanent by design; never delete locks manually.

## Earlier compact baseline (v1.4.1)

The compact near-black UI keeps the three key run measures visible and moves
the detailed counters/log into **Details**. The real app icon and black Windows
caption match the design. Jobs, Reports, API Usage, Tools, Guide and Settings
remain available. The audit shows checked-document progress, its current
operation and an explicitly incomplete state when interrupted.

**Reports** offers the filename audit or the AI correction ledger.
**AI Document Review** prepares the selected model-led handoff;
**Improve Stage 2** prepares the separate software-learning handoff. Each role
shares durable rules and file-based memory, with verified account selection.
Document corrections use hash-checked transactions and full-category ranking;
code learning requires evidence and regression tests. Both are explicit actions,
not hidden extra API work after processing.

**Settings → Notifications…** configures Discord, Telegram and/or Slack lifecycle
updates. Credentials stay local; public builds contain no bot tokens.
See the [complete user guide](docs/USER_GUIDE.md),
[AI workflow details](docs/AI_WORKFLOW_INTEGRATION.md) and
[notification setup](docs/NOTIFICATIONS.md).

### Included pipeline changes

New audit/override reports default to CSV. Audit, Summary and optional
Orientation tables stay separate, linked by a `- Tables.csv` manifest; the
report browser archives these companions together. Existing Excel reports
and the internal Filename Identification Record workbook remain supported.

Completed worker moves update the shared `<Care Home> roster.csv`, preserving
approved Lifted profile IDs for Stage 3. Classification models, pricing,
vocabulary and orientation policy are unchanged by this pipeline patch.
The earlier pipeline and restart-safe batch recovery work is included in this release.

### Ranking needs attention and targeted recovery

**Ranking needs attention** means a required date, signature or quality check
did not provide usable evidence. It is not a zero score and is not permission
to guess, mark a contract unsigned, or promote a copy because a request failed.
Keep saved results and source files in place. Completed workers may already be
in **Processed**; that does not mean the captured run scope is complete.

For a batch, use **Check batch status**. It reuses saved answers and explains
unresolved checks. If a confirmed failed finishing check is eligible for
another attempt, Stage 2 asks separately before sending new paid finishing work
and shows an estimate. Declining preserves unfinished work. An uncertain
provider outcome must be reconciled first; it is not eligible for a blind
retry. Keep the same files and settings: added, removed, moved or changed
inputs invalidate the old retry confirmation.

Targeted audit-row recovery is separate from the standard review scope. It
retains the original audit/report identity, current document evidence and prior
history; it does not silently turn every earlier **Correct** row into an
AI-reviewed row. Normal review still includes flagged and error rows. A
signature sample covering the last three pages of a long contract is not proof
about every page. Offline review or regression results are not measured live
accuracy improvement.

**AI-powered document classification and renaming for UK care-worker compliance
documents.**

Stage 2 takes the folders of downloaded, merged worker documents that Stage 1
produced and, worker by worker, converts supported formats to PDF, sends the
configured page evidence to the Claude API to identify the document, names it
from a fixed **controlled vocabulary** (`BRP`, `DBS Document`, `Certificate of
Sponsorship`, …), removes duplicates, ranks the remaining copies so the best one
is obvious, and files everything into the two folder shapes Stage 3 needs to
upload. You select a care-home folder and the app works through it, reporting
skipped, unresolved and failed cases. Conversion completion and a naming audit
do not prove that every source element survived; preserve originals and review
uncertain or unreadable documents before uploading.

```
Stage 1 — Download & Merger  →  Stage 2 — Processing  →  Stage 3 — Uploading
   pulls documents from            classifies, renames,      uploads to the
   the Lifted Talent portal        dedupes, organises        worker's profile
   and merges per worker                                     (app.lifted-talent.com)
```

- **Stage 1:** [`lifted-stage1-download-merger`](https://github.com/alijabbar04/lifted-stage1-download-merger)
- **Stage 2:** this repository
- Older, superseded code: [`lifted-compliance-automation`](https://github.com/alijabbar04/lifted-compliance-automation)

---

## ⚠️ Fixing misclassifications — read this first

> ### The golden rule: never hardcode a filename.
>
> When Stage 2 names a document wrongly, the fix goes into the **controlled
> vocabulary** or the **disambiguation rules** — never into a filename check, a
> known-bad list, or a special case for one worker or care home.
>
> **→ [docs/VOCABULARY_GUIDE.md](docs/VOCABULARY_GUIDE.md)** — how to do it
> properly, with a worked example.

Stage 2 never shows the model the filename a document arrives with: incoming
filenames are the *previous* run's wrong answers, so feeding them back in only
launders the mistake. The vocabulary and rules genuinely are the classifier's
logic — there is nowhere else for a fix to live.

The vocabulary is 87 document types across three tiers plus a ~17,000-character
rules block, held as constants in
[`src/Stage2_Processing.pyw`](src/Stage2_Processing.pyw). A flat, reviewable
mirror is generated into [`vocabulary/`](vocabulary/).

---

## Install the app (start here)

This gives you the app with Desktop and Start Menu shortcuts, plus the user
guide wired up to the in-app **Guide** button. No admin rights and no Python
needed.

**You need:** a Windows 10/11 PC. The normal public setup does not require a
GitHub account, repository invitation, GitHub CLI or Python.

1. Open the public [Releases page](https://github.com/alijabbar04/lifted-stage2-processing/releases)
   and select the intended release.
2. Download **Stage2_Processing_Setup.exe** and **SHA256SUMS.txt** from that same
   release. Compare the setup file's SHA-256 with its exact manifest entry:

   ```powershell
   Get-FileHash -Algorithm SHA256 .\Stage2_Processing_Setup.exe
   Get-Content .\SHA256SUMS.txt
   ```

3. If the hashes match, run the setup wizard. It installs the app, Desktop and
   Start Menu shortcuts, and the guide. Do not run a mismatched download.

| Installed | Where |
|---|---|
| App + Desktop/Start Menu shortcuts | `%LOCALAPPDATA%\Programs\Stage 2 - Processing\` |
| User guide (in-app **Guide** button) | `%LOCALAPPDATA%\Lifted\Guides\` |

The public setup also installs guide copies in the app's `Guides` directory and
`Documents\Lifted\Guides`. It contains no preconfigured API key or bot tokens
and does not silently install LibreOffice.

For a portable option, download **Stage2_Processing.exe** and verify its own
manifest entry. It includes the guide and AI workflow rules, but does not create
shortcuts. The older optional `install.ps1` bootstrap uses the GitHub CLI and
currently asks for GitHub sign-in; that is a script requirement, not a condition
of downloading the public setup. Developer checkout instructions are below.

### First run

1. **Get an Anthropic API key from Ali Jabbar.** It is never stored in this
   repository. Every document you process bills that key's account.
2. Launch **Stage 2 - Processing**, then **cog (⚙) → API key** → paste → save.
   It goes into the **Windows Credential Manager**, not a file, and you only do
   this once.
3. Install LibreOffice for supported Office-to-PDF conversion. Without it,
   some formats use a text-only fallback or remain unconverted. Even with it,
   verify important source content such as signatures and spreadsheet cells:
   ```powershell
   winget install --id TheDocumentFoundation.LibreOffice -e
   ```
4. Point **Folder to process** at the `[Files]` folder Stage 1 produced —
   typically `Documents\Lifted\<Care Home>\<Care Home> [Files]`, the folder
   holding one sub-folder per worker.
5. Pick **Live** (results as it goes) or **Overnight Batch**. Batch primary
   classification is discounted, while follow-up, finishing and optional audit
   work have separate estimates; provider turnaround is asynchronous and
   variable. Start it only after reviewing the estimate.

In v1.3.1, Overnight Batch settles confident controlled matches and descriptive
Other results from the primary batch. Only genuinely unresolved documents are
sent once in a second discounted batch; with second opinion enabled, that small
batch uses Sonnet directly. Keep using **Check batch status** through both
phases. Worker folders are not finalised or moved while a follow-up is pending.

If a **primary submission needs recovery**, select the same folder and use
**Check batch status**. Stage 2 checks the saved requests against Anthropic's
batch records first, then shows a recovery plan before asking to submit any
verified remaining requests. Start processing and Flatten stay unavailable
while saved batch work remains. Keep the hidden batch-state file in place.

> Legacy tooling can produce `Stage2_Processing_Preconfigured_Setup.exe` with
> a preconfigured API key. That private artifact is not the public setup and
> must not be published. Normal installation uses the public setup above.

Full walkthrough: **[docs/USER_GUIDE.pdf](docs/USER_GUIDE.pdf)**
(also on the **Guide** button inside the app). Step-by-step install for a
non-technical colleague: **[docs/INSTALL.md](docs/INSTALL.md)**.

### What the output looks like

Each finished worker folder ends up as exactly two sub-folders:

```
Jane Doe\
├── Overwrite Documents\        the 16 types Stage 3 uploads one at a time
│   ├── BRP.pdf                 (BRP, Share Code Document, CoS, driving
│   ├── Employment Contract.pdf
│   └── Certificate of Sponsorship - (15-02-2025).pdf    licence, …)
└── Bulk\
    ├── Batch 01\               everything else, 30 files per batch
    │   ├── DBS Document.pdf            ← worst copy
    │   ├── DBS Document (01).pdf
    │   └── DBS Document (02).pdf       ← best copy
    └── Batch 02\
```

Filenames are controlled-vocabulary names. Where duplicates exist, a **higher
number is the better document** (scored on newest + clearest + most relevant).
Two types keep their date: `Certificate of Sponsorship` and
`Share Code Check Result`.

With **Move mode** on, completed workers are moved to a destination folder
(normally `<Care Home> [Processed]`) as each finishes; otherwise everything is
organised in place.

---

## API cost and safeguards

Stage 2 defaults to **Haiku**, the cheapest model, which is also the most
accurate choice for this task per pound — Opus is 15× Haiku's input price and
rarely better at classification, so it is hidden behind an "advanced models"
setting plus a confirmation.

Costs are small but real. One measured data point: a full verification sweep of
**217 documents cost £6.38** — and that was on *Sonnet*, one call per document.
Haiku is a third of Sonnet's input price, and **Overnight Batch mode halves it
again**. Reckon on single-digit pounds for a typical care home, not tens.

The confirmation screen shows primary classification, required live finishing,
the optional follow-up reserve and the optional accuracy audit separately, then
applies the configured ceiling to their cumulative total. Before an enabled
audit begins, Stage 2 reports its own expected cost; processing remains complete
and the audit is skipped if the remaining budget cannot cover it.

Long PDFs retain the safe bounded view (first two pages plus the last page). They
are never automatically scanned page-by-page or split from sampled evidence.
Explicit sampled signs of a bundle create a review flag while leaving the
original intact. Short PDFs with at most seven useful pages retain the single-
classification bundle split and archive their originals.

Built-in safeguards, all editable in Settings:

| Guard | Default | Effect |
|---|---|---|
| `max_budget_gbp` | **£35** | one cumulative ceiling across batch, follow-up, live finishing and audit |
| `CONFIRM_COST_THRESHOLD_GBP` | £1 | pre-flight estimate above this needs confirmation |
| `max_workers` | 100 | stops after this many worker folders |
| `max_files` | 2000 | stops after this many files sent |
| `max_file_mb` | 25 MB | skips any single file larger than this |
| content-hash manifest | on | already-processed files are never re-billed |
| prompt caching | on | the vocabulary + rules are cached at ~10% of input price |

The live cost meter uses the **real token counts the API returns**, not
estimates. Prices live in one place — the `MODELS & PRICING` block at the top of
`src/Stage2_Processing.pyw` (last verified against Anthropic's public pricing
2026-09-02). Classification traffic goes only to the Anthropic API. Page
orientation uses the bundled ONNX model through CPU-only ONNX Runtime; pages,
thumbnails and orientation metadata never leave the laptop.

### Local page orientation

Settings offers **Off**, **Audit only / shadow mode** and **Automatic
high-confidence correction**. Audit only is the v1.3.1 default. Every PDF page
is inspected locally in small batches, separately from the bounded paid
classification sample. Automatic mode additionally requires both the configured
confidence and margin thresholds and vetoes blank, sparse, photographic and
conflicting evidence. The model is shipped in the executable, never downloaded
at runtime, and a missing/failed runtime leaves documents unchanged with a clear
audit warning—there is no paid or network orientation fallback.

---

## Setup (developers)

```powershell
git clone https://github.com/alijabbar04/lifted-stage2-processing.git
cd lifted-stage2-processing
.\setup.ps1                      # deps + optional API-key setup; -SkipKey to skip
python src\Stage2_Processing.pyw  # run from source
.\build\build.ps1                 # rebuild the exe into dist\
```

Needs **Python 3.13** (3.11+ works) with *"Add python.exe to PATH"* ticked.
Dependencies include `pymupdf`, `pillow`, `onnxruntime`, `openpyxl`, `keyring`
and Windows-only `pywin32`; use the pinned
[requirements.txt](requirements.txt). There is **no Anthropic SDK**. Most API
traffic uses `urllib`; Windows batch submissions use native WinHTTP with no
automatic POST retry or transport fallback, preserving ambiguous-submission
recovery and avoiding duplicate billing.

### Repository layout

| Path | What |
|---|---|
| [`src/Stage2_Processing.pyw`](src/Stage2_Processing.pyw) | the whole app — GUI, engine, vocabulary and rules |
| [`src/api_usage.py`](src/api_usage.py) | shared cross-app API usage ledger + analytics page |
| [`src/local_orientation.py`](src/local_orientation.py) | CPU-only ONNX page-orientation inference and safety gates |
| [`assets/orientation/`](assets/orientation/) | pinned official 7 MB model, licence and provenance |
| [`tools/orientation_benchmark.py`](tools/orientation_benchmark.py) | explicit-folder, offline orientation benchmark |
| [`src/eval_classifier.py`](src/eval_classifier.py) | regression harness over the ground-truth set |
| [`src/Stage2_Recheck_Unknowns.pyw`](src/Stage2_Recheck_Unknowns.pyw) | standalone "re-check leftover unknowns" tool |
| [`src/misname_log.py`](src/misname_log.py) | appends to the Misnaming Record workbook |
| [`vocabulary/`](vocabulary/) | **generated** flat mirror of the vocabulary + rules, and the extractor |
| [`build/stage2.spec`](build/stage2.spec), [`build/build.ps1`](build/build.ps1) | PyInstaller build of the app |
| [`build/installer/`](build/installer/) | Inno Setup installer tooling (see below) |
| [`docs/`](docs/) | user guide PDF, install, troubleshooting, vocabulary guide, deferred work |
| [`CHANGELOG.md`](CHANGELOG.md) | what changed in each version, with the harness numbers |
| [`install.ps1`](install.ps1) | end-user bootstrap: pulls the exe + guide from the Release and makes shortcuts |
| [`setup.ps1`](setup.ps1) | developer setup: deps, LibreOffice check, optional API-key storage |

`src/Stage2_Processing.pyw` and the adjacent workflow/UI modules are the source for the current v1.5.2 refinement; the earlier compact baseline is retained above for context. `build\build.ps1`
reproduces the application executable and `build\build_public_installer.ps1`
builds the credential-free public installer.

### A few things worth knowing before you edit

- **The vocabulary lives in two places at runtime**, but the source wins. The
  `SEED_*` constants create `%APPDATA%\DocReviewAIStation\Filename
  Identification Record.xlsx`, and every later load **re-seeds** it: an edited
  seed description overwrites the workbook's copy on the next run, so a fix
  reaches machines that have run before without anyone touching Excel. Only a
  rename/removal needs a second edit (`RETIRED_NAMES`). See
  [VOCABULARY_GUIDE.md](docs/VOCABULARY_GUIDE.md#seeds-vs-workbook-how-an-edit-reaches-a-machine-that-already-ran).
- **`OVERWRITE_TYPES` is mirrored in Stage 3.** Change one side only and uploads
  break silently.
- **Model IDs are pinned deliberately** (`claude-haiku-4-5`,
  `claude-sonnet-4-6`, `claude-opus-4-8`). Do not "upgrade" them without asking.
- **Local orientation is separate from paid classification.** It examines every
  processed PDF page locally with the bundled CPU-only ONNX model. v1.3.1
  defaults to audit/shadow mode; automatic high-confidence correction must stay
  opt-in until a representative, manually reviewed local benchmark supports the
  configured thresholds.
- **`classify_document_core` and `validate_result` are module-level on purpose**
  so the engine, the regression harness and the re-check tool all share the exact
  same classification path and cannot drift.
- **Writing a hidden file on Windows raises `PermissionError`** — use the
  `_write_hidden_json` helper for the `.docreview_*.json` state files.
- Stage 2 keeps its data in `%APPDATA%\DocReviewAIStation\`. Those files contain
  **real worker data** and are git-ignored. Never commit or attach them.

### Making changes

1. **Branch:** `git checkout -b fix/salary-letter-misclassification`
2. **Edit.** For classification behaviour, follow
   [docs/VOCABULARY_GUIDE.md](docs/VOCABULARY_GUIDE.md) — vocabulary and rules
   only, never a filename.
3. **Regenerate the vocabulary mirror** so the diff is reviewable:
   `python vocabulary\export_vocabulary.py`
4. **Verify offline:** run the appropriate `tests/` regression suite and
   `python tests\run_gui_isolated.py` from the repo root. The GUI runner gives
   each case a fresh process; skips, timeouts or shared-interpreter Tk failures
   are not passing runtime checks. Record the source commit and actual results.
   For classification changes, separately consider the regression harness
   (`python src\eval_classifier.py --tag my-fix` — needs a local ground-truth
   copy via `STAGE2_GT_ROOT`; costs real API money and asks first).
5. **Refresh the guide before freezing:** update `docs\USER_GUIDE.md`, then run
   `python tools\build_user_guide.py --render-dir <guide-QA-folder>` with a
   working Poppler executable (use `--pdftoppm <path>` if needed). Inspect every
   rendered page, contents-page references and page breaks. The page count is
   not fixed; update pagebreak markers/contents when content changes.
6. **Rebuild the exe:** `.\build\build.ps1`. It bundles the existing PDF; it
   does not regenerate the guide or establish Markdown/PDF freshness.
7. **Build the public installer and final checksum manifest:**
   `.\build\build_public_installer.ps1`. This writes `dist\SHA256SUMS.txt`
   after all four inputs exist: application, public setup, guide PDF and ONNX
   model. Check the four entries against the final artifacts.
8. **Check the frozen UI and deployment without processing worker documents.**
   Exercise Reports, Guide, API Usage and the review/notification dialogs, not
   just startup. When no active work will be disrupted, verify installed app and
   guide hashes plus Desktop/Start Menu shortcut targets. Publish only the
   reviewed final source/assets and compare the downloaded release hashes.
9. **Open a PR.** Explain the change, tests, guide review and any unrun paid
   evaluation. Do not reuse test counts from an earlier source state.

If a colleague runs the packaged app, remember the exe/installer must be
**rebuilt and redeployed** for them to get your change — editing the source does
not update anyone's installed copy.

### The end-user installer

The normal release uses `build\build_public_installer.ps1` and
`build\installer\Stage2_Public_Installer.iss`. It produces
**Stage2_Processing_Setup.exe**, containing the app, icon, shortcuts and guide.
This credential-free artifact is intended for public Releases; users configure
their own processing key and optional notification credentials locally.

The same tooling directory also contains a **legacy private** preconfigured
installer route. It reads a key at build time and can bundle that key into its
output. Such credential-bearing artifacts must never be published. Do not
confuse that private output with the normal public setup or its checksums.

---

## Security

- **No API key anywhere in this repository.** The app resolves credentials via
  `keyring` (Windows Credential Manager) → `ANTHROPIC_API_KEY` → a
  permission-restricted local file as a last resort. `config.json` actively
  strips any `api_key` field, and migrates legacy plaintext keys into keyring.
- **No care-worker personal data.** The only sample PDFs are synthetic,
  non-sensitive orientation fixtures; there are no production documents, logs,
  caches or runtime workbooks. The regression harness's ground-truth set is real worker
  documents and is intentionally absent — supply it locally via
  `STAGE2_GT_ROOT`.
- The controlled vocabulary contains document *category* names
  ("DBS Certificate", "Share Code") — categories, not people.
- The repository is **public**. Never commit API keys, worker documents,
  generated reports, browser profiles, or credential-bearing installers.

## Licence

No open-source application licence has been selected. The owner authorized this
release; publication does not add reuse permissions. The bundled orientation
model's Apache-2.0 terms remain separate. See [docs/LICENSING.md](docs/LICENSING.md).
