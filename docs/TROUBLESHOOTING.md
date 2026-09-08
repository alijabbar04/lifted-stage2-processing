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

Every PDF page can be checked by the bundled CPU-only ONNX model. v1.3.1 starts
in **Audit only / shadow mode**, which reports uncertain and apparently rotated
pages but never changes them. **Automatic high-confidence correction** is
opt-in: it also requires the confidence-margin gate and refuses blank, sparse,
photograph-only or locally conflicting pages. Accepted corrections are saved
atomically before paid classification. No orientation data leaves the laptop
and there is no API fallback.

If a scan is still misread, it is usually so skewed that no orientation reads
cleanly. Straighten it with **🧰 Tools → PDF Rotator** and re-run.

### One PDF contains several different documents

Short PDFs that fit in one request bypass page-one triage and are shown in full,
so a confident Passport on page 1 cannot hide a visa or BRP later in the file.
The same response supplies the guarded split plan; it does not trigger extra
bundle API calls. Long PDFs retain the bounded first-two-plus-last paid sample,
are never split from sampled evidence, and are only flagged when that sample
explicitly suggests a bundle. For a confirmed short split, the original is
archived outside the worker tree and every page is retained.

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

The cumulative cost estimate hit the ceiling — **£35** by default. Raise
`max_budget_gbp` in Settings if the run is legitimately that large, then re-run;
completed files are skipped.

### The run stopped after 100 workers or 2000 files

Deliberate guard rails (`max_workers`, `max_files` in Settings) so a
mis-selected folder cannot spend unbounded money. Raise them or process in
chunks.

### Overnight Batch results never arrived

Provider turnaround is asynchronous and variable; a quiet period alone does
not mean failure. State lives in `.docreview_batch_state.json` (hidden) in the
care-home folder — leave it alone, it is how the app resumes and collects
results. Re-open the app and the same folder to check on and apply a submitted
batch.

v1.3.1 can show a second **follow-up** phase for only the genuinely unresolved
documents. This is also a discounted Message Batch, not a live Haiku-to-Sonnet
retry chain. Keep using **Check batch status**; workers are not moved until it
finishes. An unexpectedly large follow-up requires confirmation before it is
submitted. If Stage 2 reports an ambiguous submission state, it deliberately
will not resubmit automatically because doing so could bill the same documents
twice.

Do not delete that file while a batch is outstanding, or the app loses track of
work you have already paid for.

### A `.docreview_batch_writer.lock` file remains after Windows work

The batch writer deliberately keeps this same path while it is working. On
Windows, Stage 2 unlocks and closes the file before making a best-effort
unlink. Another standard Python holder or contender may still have it open, so
Windows can refuse deletion; that remnant is harmless and is retired on a
later successful use. POSIX locks and request/ledger locks remain permanent by
design. Do not delete a lock manually or use its presence alone to diagnose a
failed batch; check the saved batch state and current operation instead.

While that state is pending or ambiguous, Stage 2 blocks all live processing
for the same folder. This is deliberate: a live fallback could duplicate paid
work. Use **Check batch status** to retrieve/complete it or resolve the retained
attempt record safely.

### Primary batch submission needs recovery

This means the first submission phase was interrupted, or the app could not
confirm a submission response. It is separate from the later follow-up phase.
Select the same care-home folder and press **Check batch status**. Stage 2
first performs a read-only comparison of its saved request list and
Anthropic's batch records. It presents the verified recovery plan and remaining
primary estimate before asking to resume. Accepted requests are kept; only
requests verified as unsubmitted can be sent by recovery.

If reconciliation is blocked, the message explains what could not be verified.
Resolve that condition and check again. Leave `.docreview_batch_state.json`
in place. Do not use a new run or flatten the folder to work around the
unfinished batch; those actions remain disabled while it is pending.

After recovery, the main header updates from saved state. Use **Check batch
status** again to collect and apply the results when the batches finish.
A stopped, failed or pending operation no longer fills the progress bar just
because its background operation returned.

### Ranking needs attention

**Ranking needs attention** means a required date, signature or quality check
did not provide usable evidence. It is not permission to guess a score, mark a
contract unsigned or promote a copy because a request failed. Use **Check batch
status** and read the retained finishing details. If a confirmed failed
finishing operation is eligible for another attempt, Stage 2 asks separately,
shows an estimate and sends only that new paid finishing work after explicit
confirmation. It never authorizes a blind repeat of primary classification.

An uncertain provider outcome must be reconciled before any retry. Keep the
same files and saved settings; if a file was added, removed, moved or changed,
the old retry confirmation is invalid. Do not delete state or force a new run.
Incomplete scope remains incomplete even when some workers are already in
Processed. Targeted explicit audit-row recovery preserves the original audit
identity and review history; it does not mean every earlier **Correct** row was
AI-reviewed. A signature sample limited to the last three pages is not proof
about every page of a long contract.

### Local orientation warning / model unavailable

Page orientation uses the bundled CPU-only ONNX model and never falls back to a
paid API. If the model, checksum or ONNX Runtime is unavailable, Stage 2 leaves
every page unchanged, continues normal classification, and records the warning
in the Orientation sheet of `Filename_Audit_Report.xlsx`.

v1.3.1 defaults to **Audit only / shadow mode**. Apparently rotated and
uncertain pages are recorded but not changed. Automatic mode should remain
opt-in until `tools/orientation_benchmark.py` has been run on an explicitly
selected, representative and manually reviewed test folder.

Example (offline; the source folder is never modified):

```powershell
python tools\orientation_benchmark.py C:\path\to\reviewed-test-set `
  --output C:\Temp\orientation-results.xlsx
```

### Local orientation state could not save

Windows can temporarily refuse the atomic replacement of the hidden local
orientation-state file while another program is reading it. Stage 2 retries
only the relevant sharing/access-denied failures for a bounded period. The
previous valid state remains intact while it retries. If contention persists,
the save still fails visibly and only the temporary file owned by that save is
cleaned up.

Do not monitor a live run by repeatedly opening or reading its active hidden
JSON state. Use the progress shown in Stage 2. Close any editor, shell command,
previewer, scanner or synchronisation tool that may be holding the state file,
wait until the run is idle, and then retry the unfinished operation. A handle
that requests delete sharing can still block replacement on some Windows
systems, so it is not a reliable workaround. Permanent permission or storage
errors are not ignored.

---

## Tools, guides and the GUI

### The Guide button says "Guide not found"

The guide PDF is not on the machine. Stage 2 looks in `%LOCALAPPDATA%\Lifted\Guides`,
beside the exe, and `Documents\Lifted\Guides`.

`install.ps1` places it for you, so the quickest fix is to re-run the install
step from [INSTALL.md](INSTALL.md#1-install-it). To do it by hand, copy
`docs/USER_GUIDE.pdf` into `%LOCALAPPDATA%\Lifted\Guides\` and rename it so it
**starts with `Stage 2 `** — e.g. `Stage 2 Guide - AI Processing.pdf`. Matching is
a literal filename-prefix check (`name.lower().startswith("stage 2")`), so
`Stage2_Guide.pdf` will *not* be found — the space matters.

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

Run `.\setup.ps1` — a missing `pymupdf`, `pillow`, `onnxruntime` or `openpyxl` stops it
immediately. `keyring` is the exception: without it the app still runs and falls
back to the `ANTHROPIC_API_KEY` environment variable.

### The installed app says it cannot find a usable `init.tcl`

Use a v1.3.1 build produced after the frozen-startup correction. Earlier
v1.3.1 candidates allowed Windows to select `C:\Windows\Temp` for PyInstaller's
one-file extraction; Tcl/Tk could then fail before the Stage 2 window opened.
Corrected builds extract into the current user's
`%LOCALAPPDATA%\Lifted\Stage2Runtime` directory and are rejected by the build
script unless the real Stage 2 window opens in a launch test.

This error happens before any worker folder is selected. It does not process,
rename, move or upload documents, and it does not make an API call.

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

## Slack notifications need workspace approval

If Slack shows **Request to Add New Webhook**, ask an app administrator to approve
the notification app. A submitted request does not generate a webhook or send a
message. After approval, finish installation for the intended channel, save its
webhook in **Settings > Notifications > Slack**, enable Slack and test delivery.
The optional label in Stage 2 does not change Slack's channel. A saved credential
is not a confirmed connection; **queued** is not **delivered**. A notification
failure does not mean document processing failed. See [notification setup](NOTIFICATIONS.md).
