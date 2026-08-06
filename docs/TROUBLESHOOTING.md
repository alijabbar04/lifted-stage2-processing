# Troubleshooting

Failure modes below are taken from the app's own error handling. Each entry says
what you will actually see, why, and what to do.

---

## API key and billing

### "authentication failed - check the API key" (HTTP 401)

The key is missing, wrong, or has been revoked.

1. Open **Settings (⚙) → API key** and check what it says the key source is. The
   app looks in three places, in this order:
   `keyring` (Windows Credential Manager) → `ANTHROPIC_API_KEY` environment
   variable → a permission-restricted local file, as a last resort.
2. Re-paste the key. An Anthropic key starts `sk-ant-`.
3. If it worked yesterday and not today, the key was probably **rotated or
   revoked**. Ask Ali Jabbar for the current one — do not go hunting for an old
   copy on disk.

### The run stopped and said the credit balance is too low

Not an error in Stage 2 — the Anthropic account has run out of credit. The run
stops cleanly rather than burning through retries, and records a row in the
**Run Status** tab of `Filename Identification Record.xlsx` with the timestamp,
care home, and the worker it stopped on, so you can resume from the right place.

Top the account up at <https://console.anthropic.com>, then re-run the same
folder. Already-processed files are skipped (see *Will re-running charge me
twice?* below), so you only pay for what is left.

### "rate limited - too many requests, slow down" (HTTP 429)

Transient. The app already retries automatically (twice, for 429 and 5xx). If it
still fails, the account is hitting its rate limit — wait a few minutes and
re-run, or switch to **Overnight Batch** mode, which is not subject to the same
live-traffic limits and costs half as much.

### "Anthropic temporarily overloaded" (HTTP 529)

Anthropic's side, not yours. Retried automatically; re-run later if it persists.

### Will re-running charge me twice?

No. Stage 2 keeps a content-hash manifest (`.docreview_manifest.json`, hidden,
in the care-home folder) of every file it has successfully processed with a given
model and settings. Unchanged files are skipped on a re-run. The manifest is only
updated when a result is actually **applied**, so an interrupted run does not
mark work as done that it never finished.

Editing the vocabulary does **not** invalidate the manifest — if you want a
folder re-classified after a vocabulary fix, delete
`.docreview_manifest.json` from the care-home folder (it is a hidden file; turn
on hidden files in Explorer, or delete it from PowerShell).

### The cost estimate looks wrong

It is computed from the real token counts the API returns, multiplied by the
price table and the USD→GBP rate in Settings. Two things to check: the **FX
rate** (default 0.79) and the **price table** — if Anthropic changes prices, the
numbers in `MODELS & PRICING` near the top of `src/Stage2_Processing.pyw` are the
single place to edit.

---

## Documents that will not process

### Files are silently skipped and the log mentions cloud-only files

The commonest confusing failure. **OneDrive "online-only" placeholder files have
no bytes on disk.** Stage 2 detects them (`FILE_ATTRIBUTE_OFFLINE` /
`RECALL_ON_OPEN` / `RECALL_ON_DATA_ACCESS`) and deliberately skips them rather
than forcing a download mid-run — the alternative is a run that stalls
unpredictably on network speed.

**Fix:** hydrate the files first. In Explorer, right-click the care-home folder →
**Always keep on this device**, wait for the green tick on every file, then
re-run. Best practice is to keep `Documents\Lifted` permanently pinned to the
device.

### Word / Excel / PowerPoint files came out as plain text

LibreOffice is not installed, so conversion fell back to text-only. The
classifier reads pages as **images**, so a text-only conversion classifies
poorly.

Install LibreOffice from <https://www.libreoffice.org/download> and re-run — it
is detected automatically. You will also need to delete
`.docreview_manifest.json` first, or the badly-converted files will be skipped
as already done.

### A PDF is corrupt or will not render

Individually logged and skipped; the run continues. Try the **🧰 Tools → PDF
Rotator / PDF Splitter / Convert to PDF** helpers to re-save it, or open and
re-export it from any PDF viewer. If a file has no readable pages at all there is
nothing the classifier can act on — handle it by hand.

### Sideways or upside-down scans

Handled automatically, three ways: a free text-layer rotation check
(`detect_pdf_text_rotation`), a rotation retry that re-sends page 1 in all four
orientations, and — when `auto_rotate` is on (the default) and confidence is
≥ 60 — physically rewriting the page upright so Stage 3 uploads it the right way
round.

If a scan is still misread, it is usually so skewed that no orientation reads
cleanly. Straighten it with **🧰 Tools → PDF Rotator** and re-run.

### One PDF contains several different documents

Also automatic. A file flagged as a bundle by the classifier, or any PDF of 4+
pages, gets a full page-by-page scan (`detect_bundle_starts`) and is split into
`<name> [doc N].pdf` parts, each classified separately. 2–3 page PDFs are only
scanned when they look bundle-prone — e.g. classified as an inherently one-page
ID type, which is the stacked-passport/licence/BRP case.

If a bundle slips through, split it with **🧰 Tools → AI Document Splitter** and
re-run that worker.

---

## Classification problems

### Everything landed as "Other - Unknown"

Means the model would not confidently match the documents to the vocabulary. Most
often the input is bad rather than the vocabulary: text-only Office conversions
(install LibreOffice) or unhydrated OneDrive placeholders produce blank-looking
pages.

If the documents are genuinely fine, run the **🧰 Tools → Re-check Unknowns**
tool, which re-classifies leftover unknowns individually at full page resolution
with a rotation retry and a stronger-model second opinion.

### A document was renamed to the wrong type

Do **not** fix this by hardcoding the filename. Read
**[VOCABULARY_GUIDE.md](VOCABULARY_GUIDE.md)** — fixes go into the controlled
vocabulary or the disambiguation rules, and there is a worked example.

Also add a row to `%APPDATA%\DocReviewAIStation\Misnaming Record.xlsx` (standing
instruction: log every misnamed document found, resolved or not).

### The run paused and asked me to define a document

Working as designed. The API could not match it to the controlled list, so it is
asking you to file it as **Relevant** or **Other**. Your answer is written into
the vocabulary workbook and is remembered from then on.

Names are rejected if longer than 80 characters — it wants a *document type*, not
a description of the document.

### After a vocabulary edit, nothing changed

Expected, and it catches everyone once. The `SEED_*` constants are only used to
**create** the workbook on first run; after that
`%APPDATA%\DocReviewAIStation\Filename Identification Record.xlsx` is what the
app reads. Editing a seed does not touch an existing workbook.

Delete the affected row from the workbook (it re-seeds from source), or edit the
description cell in Excel, or add the old name to `RETIRED_NAMES` to prune it
everywhere automatically. Full explanation in
[VOCABULARY_GUIDE.md](VOCABULARY_GUIDE.md#the-two-place-problem-seeds-vs-workbook).

---

## Runs that stop early

### "budget limit reached"

The live cost estimate hit the ceiling — **£25** by default. Raise
`max_budget_gbp` in Settings if the run is legitimately that large, then re-run;
completed files are skipped.

### The run stopped after 100 workers or 2000 files

Deliberate guard rails (`max_workers`, `max_files` in Settings) so a
mis-selected folder cannot spend unbounded money. Raise them or process in
chunks.

### Overnight Batch results never arrived

Batch jobs normally finish in about an hour and are guaranteed within 24. State
lives in `.docreview_batch_state.json` (hidden) in the care-home folder — leave
it alone, it is how the app resumes and collects results. Re-open the app and the
same folder to check on and apply a submitted batch.

Do not delete that file while a batch is outstanding, or the app loses track of
work you have already paid for.

---

## Tools, guides and the GUI

### The Guide button says "Guide not found"

The guide PDF is not on the machine. Stage 2 looks in `%LOCALAPPDATA%\Lifted\Guides`,
beside the exe, and `Documents\Lifted\Guides`.

Copy `docs/USER_GUIDE.pdf` from this repository into
`%LOCALAPPDATA%\Lifted\Guides\` and rename it to start with `Stage 2` — for
example `Stage 2 Guide - AI Processing.pdf` (matching is by filename prefix).
The full installer places it there automatically.

### Tools panel entries show "(NOT FOUND)"

Those helper tools (Convert to PDF, PDF Rotator, PDF Splitter, AI Document
Splitter, Re-check Unknowns) are separately built exes that the full installer
places in `%LOCALAPPDATA%\Lifted\Tools`. They are not part of this repository, so
a source checkout or the bare exe will not have them.

Copy the exes into `%LOCALAPPDATA%\Lifted\Tools\`, or use the full installer.

### PermissionError writing a hidden file

A fixed bug worth knowing if you touch that code: on Windows, writing to a file
that already has the *hidden* attribute raises `PermissionError`. Writes must
clear the attribute first — use the `_write_hidden_json` helper rather than
`write_text` for `.docreview_*.json`.

### "Could not find Stage2_Processing.pyw"

From `Stage2_Recheck_Unknowns.pyw` or `eval_classifier.py`: both load the main
app as a module and expect it in the same folder. Keep them together in `src/`,
or run from the repository root.

### The app will not start from source

Run `.\setup.ps1` — a missing `pymupdf`, `pillow` or `openpyxl` stops it
immediately. `keyring` is the exception: without it the app still runs and falls
back to the `ANTHROPIC_API_KEY` environment variable.

---

## Regression harness

### `eval_classifier.py` cannot find the ground truth

The ground-truth documents are real care-worker files and are **not in this
repository**. Point the harness at your local copy:

```powershell
$env:STAGE2_GT_ROOT = "C:\path\to\folder"   # holds 'Misnamed Files\' + 'misnamed files record.xlsx'
python src\eval_classifier.py --tag my-fix
```

Without it, the historic locations under your Desktop are probed. The harness
costs real API money and asks for confirmation before sending anything.

---

## Getting help

Include: what you were doing, the exact error text, the model and mode
(Live/Batch), and whether the files were OneDrive placeholders.

**Never attach real worker documents, or any file from
`%APPDATA%\DocReviewAIStation\`** — they contain personal data.
