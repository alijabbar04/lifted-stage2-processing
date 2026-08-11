# Stage 2 — Processing

**AI-powered document classification and renaming for UK care-worker compliance
documents.**

Stage 2 takes the folders of downloaded, merged worker documents that Stage 1
produced and, worker by worker, converts everything to PDF, sends each page to
the Claude API to work out *what the document actually is*, renames it to a name
from a fixed **controlled vocabulary** (`BRP`, `DBS Document`, `Certificate of
Sponsorship`, …), removes duplicates, ranks the remaining copies so the best one
is obvious, and files everything into the two folder shapes Stage 3 needs to
upload. It is fully automatic — you point it at a care-home folder and it
processes every document in it.

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

**You need:** a Windows 10/11 PC and a GitHub account that has been **invited to
this repository** (it's private — ask the maintainer for an invite and accept the
email first).

Open **PowerShell** (press the Windows key, type `powershell`, press Enter) and
run these steps:

**Step 1 — install the GitHub CLI** (one time; it's how the download
authenticates against this private repo):

```powershell
winget install --id GitHub.cli -e
```

Then **close PowerShell and open a new window** so the `gh` command is found.

**Step 2 — sign in to GitHub** (one time; a browser window opens — use the
invited account):

```powershell
gh auth login --web
```

**Step 3 — download and run the installer:**

```powershell
gh release download v1.0.0 --repo alijabbar04/lifted-stage2-processing --pattern install.ps1 --dir $env:TEMP --clobber; & $env:TEMP\install.ps1
```

That downloads the app (~57 MB), installs it, creates the shortcuts, and
verifies what landed:

| Installed | Where |
|---|---|
| App + Desktop/Start Menu shortcuts | `%LOCALAPPDATA%\Programs\Stage 2 - Processing\` |
| User guide (in-app **Guide** button) | `%LOCALAPPDATA%\Lifted\Guides\` |

> Prefer to grab the file by hand? Download `Stage2_Processing.exe` from the
> [Releases](https://github.com/alijabbar04/lifted-stage2-processing/releases)
> page — it's a single self-contained exe and runs from anywhere. You just don't
> get the shortcuts or the in-app guide.

### First run

1. **Get an Anthropic API key from Ali Jabbar.** It is never stored in this
   repository. Every document you process bills that key's account.
2. Launch **Stage 2 - Processing**, then **cog (⚙) → API key** → paste → save.
   It goes into the **Windows Credential Manager**, not a file, and you only do
   this once.
3. Install LibreOffice if `install.ps1` said it was missing — without it,
   Word/Excel/PowerPoint files convert as text only, which classifies badly:
   ```powershell
   winget install --id TheDocumentFoundation.LibreOffice -e
   ```
4. Point **Folder to process** at the `[Files]` folder Stage 1 produced —
   typically `Documents\Lifted\<Care Home>\<Care Home> [Files]`, the folder
   holding one sub-folder per worker.
5. Pick **Live** (results as it goes) or **Overnight Batch** (half price, ready
   within ~1 hour, guaranteed within 24). Start it.

> There is also a full installer that pre-configures the API key for you, so
> there is nothing to paste. It is deliberately **not** published here because
> the key is compiled into it — ask Ali for that one directly if you would rather
> not handle a key.

Full walkthrough with screenshots: **[docs/USER_GUIDE.pdf](docs/USER_GUIDE.pdf)**
(also on the **Guide** button inside the app). Step-by-step install for a
non-technical colleague: **[docs/INSTALL.md](docs/INSTALL.md)**.

### What the output looks like

Each finished worker folder ends up as exactly two sub-folders:

```
Jane Doe\
├── Overwrite Documents\        the 15 types Stage 3 uploads one at a time
│   ├── BRP.pdf                 (BRP, Share Code Document, CoS, driving
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

Built-in safeguards, all editable in Settings:

| Guard | Default | Effect |
|---|---|---|
| `max_budget_gbp` | **£25** | run stops as soon as the live estimate hits it |
| `CONFIRM_COST_THRESHOLD_GBP` | £1 | pre-flight estimate above this needs confirmation |
| `max_workers` | 100 | stops after this many worker folders |
| `max_files` | 2000 | stops after this many files sent |
| `max_file_mb` | 25 MB | skips any single file larger than this |
| content-hash manifest | on | already-processed files are never re-billed |
| prompt caching | on | the vocabulary + rules are cached at ~10% of input price |

The live cost meter uses the **real token counts the API returns**, not
estimates. Prices live in one place — the `MODELS & PRICING` block at the top of
`src/Stage2_Processing.pyw` (last verified against Anthropic's public pricing
2026-07-06). All traffic goes to the Anthropic API and nowhere else.

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
Dependencies are four packages — `pymupdf`, `pillow`, `openpyxl`, `keyring`
([requirements.txt](requirements.txt)). There is **no Anthropic SDK**: the app
calls the Messages API and the Message Batches API directly over `urllib`.

### Repository layout

| Path | What |
|---|---|
| [`src/Stage2_Processing.pyw`](src/Stage2_Processing.pyw) | the whole app — GUI, engine, vocabulary, rules (9,530 lines) |
| [`src/api_usage.py`](src/api_usage.py) | shared cross-app API usage ledger + analytics page |
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

`src/Stage2_Processing.pyw` is the exact source that produced the shipped v1.0.0
exe, so `build\build.ps1` reproduces it. The only edit is one code comment whose
example filename used a real worker's name, replaced with a synthetic one — no
functional change.

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
4. **Test locally:** `python src\Stage2_Processing.pyw` on a small folder, and
   for classification changes run the regression harness
   (`python src\eval_classifier.py --tag my-fix` — needs a local ground-truth
   copy via `STAGE2_GT_ROOT`; costs real API money and asks first).
5. **Rebuild the exe:** `.\build\build.ps1`
6. **Open a PR.** Say what changed in the vocabulary, and either paste the
   harness result or say why it was not run.

If a colleague runs the packaged app, remember the exe/installer must be
**rebuilt and redeployed** for them to get your change — editing the source does
not update anyone's installed copy.

### The end-user installer

[`build/installer/`](build/installer/) holds the Inno Setup project that produces
the colleague-facing setup (app + API key pre-configured into the Credential
Manager + silent LibreOffice download + the guides/tools payload).

**Its output is deliberately never published to this repository or its Releases,
because the API key is compiled into it.** The key is read at build time from
`ANTHROPIC_API_KEY` or a git-ignored `.installer_secrets` file — never from
anything tracked here. Treat any built installer as a secret.

---

## Security

- **No API key anywhere in this repository.** The app resolves credentials via
  `keyring` (Windows Credential Manager) → `ANTHROPIC_API_KEY` → a
  permission-restricted local file as a last resort. `config.json` actively
  strips any `api_key` field, and migrates legacy plaintext keys into keyring.
- **No care-worker personal data.** No sample documents, no logs, no caches, no
  runtime workbooks. The regression harness's ground-truth set is real worker
  documents and is intentionally absent — supply it locally via
  `STAGE2_GT_ROOT`.
- The controlled vocabulary contains document *category* names
  ("DBS Certificate", "Share Code") — categories, not people.
- Repository is **private**. Keep it that way: the classification prompts encode
  internal compliance process.
