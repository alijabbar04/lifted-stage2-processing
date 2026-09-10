# Installing Stage 2 — Processing

This guide applies to the v1.5.5 Obsidian / Jade release (build
2026.09.10-session1).

This guide assumes no technical knowledge. Follow it top to bottom.

There are **two ways** to get Stage 2. Pick one.

---

# Option A — Just use the app (recommended)

You do not need Python, this repository, or any developer tools.

## 1. Install it

**You need** a Windows 10/11 PC. The public setup does not require a GitHub
account, invitation, GitHub CLI or Python.

1. Open the public [Releases page](https://github.com/alijabbar04/lifted-stage2-processing/releases)
   and select the release you intend to install.
2. Download **Stage2_Processing_Setup.exe** and **SHA256SUMS.txt** from the same
   release. Before running the setup, open PowerShell in your download folder:

   ```powershell
   Get-FileHash -Algorithm SHA256 .\Stage2_Processing_Setup.exe
   Get-Content .\SHA256SUMS.txt
   ```

3. Compare the printed hash with the exact `Stage2_Processing_Setup.exe` entry
   in the manifest. Do not run the file if they differ.
4. Run the setup wizard. It installs the app, Desktop and Start Menu shortcuts,
   and the user guide for the in-app **Guide** button.

| Installed | Where |
|---|---|
| App + shortcuts | `%LOCALAPPDATA%\Programs\Stage 2 - Processing\` |
| User guide | `%LOCALAPPDATA%\Lifted\Guides\` |

The setup runs per user without admin rights. It also places guide copies in
the app's `Guides` folder and `Documents\Lifted\Guides`. It does not configure
an API key or silently install LibreOffice.

### Other download routes

**Stage2_Processing.exe** is the portable app. Verify its own entry in the
same release's checksum file; it includes the guide/rules but creates no
shortcuts. **Stage2_Guide_AI_Processing.pdf** is the standalone guide download.

The older optional **install.ps1** bootstrap uses the GitHub CLI and currently
asks for GitHub sign-in. That is a requirement of the script, not the public
setup. A failed public download does not imply a missing repository invitation:
check the selected release, filename and connection. Developer checkout is
covered separately in Option B.

> Windows may warn that the file is "not commonly downloaded" because it is not
> code-signed. Choose **Keep**, then if SmartScreen appears, **More info → Run
> anyway**. This is expected for an internally-built tool.

## 2. Get an API key

Stage 2 reads documents using the Claude API, which needs an Anthropic API key.

**Ask Ali Jabbar for a key.** It is never stored in this repository and never
sent over email or chat where it can be forwarded. Every document you process
bills that key's Anthropic account, so treat it like a company credit card.

## 3. First run

Launch **Stage 2 - Processing** from the Desktop shortcut (or double-click
`Stage2_Processing.exe` if you downloaded it by hand). Then:

1. Click the **cog (⚙) button** to open Settings.
2. Paste the API key into the **API key** field and save.

The key is stored in the **Windows Credential Manager** — the operating
system's encrypted credential store — not in a text file. You only do this
once; it is remembered for every future run.

> If a colleague gave you the private
> **`Stage2_Processing_Preconfigured_Setup.exe`** instead of the public setup,
> the key was configured during install and you can skip this step. That private
> installer is never published to GitHub because it carries the key inside it.

## 4. Install LibreOffice (optional but recommended)

LibreOffice provides Office-to-PDF conversion for supported formats. Without
it, some formats fall back to text-only output and others can remain
unconverted. Text-only output cannot preserve all images, signatures or layout.

Even a completed full conversion can omit visible source elements or reproduce
clipping already in the source. Preserve original documents and check important
content before upload; the filename audit is not a source-to-output fidelity
comparison. See the user guide's conversion and audit limits.

Get it free from <https://www.libreoffice.org/download>. Stage 2 finds it
automatically afterwards; there is nothing to configure.

## 5. Where documents go in and out

Stage 2 works on the folders **Stage 1** produced. The normal layout is:

```
Documents\Lifted\
└── <Care Home>\
    ├── <Care Home> [Files]        ← INPUT: what Stage 1 downloaded and merged
    │   ├── Jane Doe\              ← one folder per worker, loose documents inside
    │   └── John Smith\
    └── <Care Home> [Processed]    ← OUTPUT: finished workers (if Move mode is on)
```

In the app:

- **Folder to process** → point it at the **`[Files]`** folder (the one holding
  the per-worker sub-folders, *not* an individual worker).
- Optionally turn on **Move mode** and set the destination to the
  **`[Processed]`** folder. Completed workers are then moved there as each one
  finishes, so you can see progress and Stage 3 has a clean folder to upload
  from. With Move mode off, Stage 2 renames and organises everything **in
  place** inside `[Files]`.

Inside each finished worker folder you get exactly two sub-folders:

```
Jane Doe\
├── Overwrite Documents\    BRP, Share Code Document, CoS, driving licence, ...
│                           (the 16 types Stage 3 uploads one at a time,
│                           including Employment Contract)
└── Bulk\
    ├── Batch 01\           everything else, 30 files per batch
    └── Batch 02\
```

Filenames are the controlled document types. Where there are duplicates, a
higher number is the **better** copy — `DBS Document (02)` beats
`DBS Document (01)` beats plain `DBS Document`.

## 6. Running a batch

1. Pick the `[Files]` folder.
2. Leave the model on **Haiku (cheapest)** unless you have a reason not to.
3. Choose **Live** (results as it goes) or **Overnight Batch**. Batch primary
   classification is discounted, while follow-up, finishing and optional audit
   work have separate estimates. Provider turnaround is asynchronous and
   variable; review the estimate and do not assume a fixed completion time.
4. Start it. The cost estimate in GBP updates live from the real token counts
   the API returns. The run uses one cumulative budget across primary
   classification, follow-up, finishing and the optional audit (**£35** by
   default, changeable in Settings).

For Overnight Batch, use **Check batch status** until all phases finish. A
confident controlled match or a specific descriptive Other result is settled by
the primary batch. Only generic, malformed or low-confidence results enter one
discounted follow-up batch. If second opinion is enabled, that small batch uses
the stronger model directly. No worker folder is finalised or moved while the
follow-up is pending.

The estimate screen lists primary batch classification, required live finishing,
the follow-up reserve and the optional audit separately. An enabled audit shows
its expected cost before it starts and is skipped—without undoing completed
processing—when the remaining cumulative budget is insufficient.

If the run reports **ranking needs attention**, a date, signature or quality
check lacked usable evidence. Use **Check batch status** to read the retained
finishing details. Only a confirmed eligible failed finishing operation may be
retried after its separate estimate and confirmation; reconcile an uncertain
provider outcome first, and never repeat primary classification blindly. Keep
the same files and settings: a changed, moved, added or removed input
invalidates the old retry confirmation. Some workers may already be in
**Processed** while the captured run is still incomplete.

Read **`docs/USER_GUIDE.pdf`** for the full walkthrough. It is
also available inside the app from the **Guide** button.

If something goes wrong, see **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)**.

---

# Option B — Run and modify the source

Do this if you need to change how Stage 2 classifies documents.

## 1. Install Python 3.13

Download from <https://www.python.org/downloads/>.

**During installation, tick "Add python.exe to PATH".** This is easy to miss and
everything else fails without it.

Python 3.11 or newer works; 3.13 is what the shipped build uses.

## 2. Install Git

Download from <https://git-scm.com/download/win> and accept the defaults.

## 3. Clone the repository

Open **PowerShell** and run:

```powershell
mkdir -Force ~\repos
cd ~\repos
git clone https://github.com/alijabbar04/lifted-stage2-processing.git
cd lifted-stage2-processing
```

The repository is public. Git may still ask you to sign in to GitHub, depending
on your local Git configuration.

## 4. Run setup

```powershell
.\setup.ps1
```

This checks your Python version and installs the dependencies in
`requirements.txt`: `pymupdf`, `pillow`, `onnxruntime`, `openpyxl`, `keyring`,
plus `pywin32==312` when `sys_platform == "win32"`. The Windows dependency
supports native WinHTTP batch transport. Setup also checks for LibreOffice and
offers to store your Anthropic API key in the Windows Credential Manager.

When it asks for the key, paste it and press Enter — the typing is hidden. Press
Enter on an empty prompt to skip and paste it into the app later instead. To
install the packages and nothing else:

```powershell
.\setup.ps1 -SkipKey
```

> If PowerShell refuses to run the script ("running scripts is disabled on this
> system"), allow local scripts for your account once:
>
> ```powershell
> Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
> ```

## 5. Run from source

```powershell
python src\Stage2_Processing.pyw
```

Use `pythonw` instead of `python` to run it without a console window behind it.

## 6. Verify and rebuild after a change

Run the appropriate offline regression suite under `tests/` and the isolated
GUI runner from the repository root:

```powershell
python tests\run_gui_isolated.py
./tools/test_install_bootstrap.ps1
```

Each GUI case runs in a fresh process. A skip, timeout or shared-interpreter Tk
failure is not a passing runtime check. Record the final source commit, actual
results and unverified cases. A startup smoke alone does not exercise dialogs.
Paid classification evaluation is separate and must be deliberately authorized.
The bootstrap check exercises only extracted checksum logic and synthetic files;
it never starts an installation, downloads assets or changes shortcuts.

Update `docs\USER_GUIDE.md`, then regenerate the guide before the application
is frozen. The guide builder needs ReportLab and pypdf in the chosen Python
environment and Poppler for visual QA:

```powershell
python tools\build_user_guide.py --render-dir <guide-QA-folder> --pdftoppm <pdftoppm-path>
```

Replace the angle-bracket paths with real local paths. Inspect every rendered
page for missing text, overflow and readability; recheck the contents page and
page-break markers. Page count may change. `build\build.ps1` bundles the
existing PDF and does not verify that it matches the Markdown.

Then build the app and credential-free public setup:

```powershell
.\build\build.ps1
.\build\build_public_installer.ps1
```

The app is written to `dist\Stage 2 - Processing.exe`; the setup to
`build\installer\Output\Stage2_Processing_Setup.exe`. The public build then
writes `dist\SHA256SUMS.txt` with **four** entries:

| Release entry | Local input |
| --- | --- |
| Stage2_Processing.exe | dist\Stage 2 - Processing.exe |
| Stage2_Processing_Setup.exe | build\installer\Output\Stage2_Processing_Setup.exe |
| Stage2_Guide_AI_Processing.pdf | docs\USER_GUIDE.pdf |
| inference.onnx | assets\orientation\inference.onnx |

Generate the manifest only after those artifacts are final. It does not contain
a hash of itself. Verify all four entries rather than checking the app alone.

Exercise the actual frozen UI without processing worker documents: Reports,
Guide, API Usage, review/notification dialogs, normal/minimum window sizes and
relevant display scaling. When no active work will be interrupted, install the
final setup and check executable/guide hashes and Desktop/Start Menu targets.
After release publication, compare the downloaded assets with those same final
hashes. Source changes alone do not update the installed application.

Legacy private preconfigured-installer tooling is separate from this public
build. Its credential-bearing output must not be placed in a public release.

The 7 MB `PP-LCNet_x1_0_doc_ori` ONNX model is included in the executable. It
is CPU-only and is never downloaded at runtime. Its pinned revision, Apache-2.0
licence and SHA-256 are recorded in `assets/orientation/PROVENANCE.md`.

## What to read next

- **[VOCABULARY_GUIDE.md](VOCABULARY_GUIDE.md)** — read this **before** changing
  any classification behaviour. There is one golden rule and it matters.
- **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** — common failures and fixes.
- **[../README.md](../README.md)** — how the pieces fit together.

## Where Stage 2 keeps its own files

Not in the repository, and not on your Desktop —
**`%APPDATA%\DocReviewAIStation\`** (paste that into Explorer's address bar):

| File | What it is |
|---|---|
| `config.json` | model choice, FX rate, budget ceiling, toggles (never the API key) |
| `Filename Identification Record.xlsx` | the live controlled vocabulary |
| `filename change record.csv` | every rename made |
| `Doc Review AI filename change record.xlsx` | logged only when you override the AI |
| `Misnaming Record.xlsx` | the running log of misclassifications found |

These contain real worker data. Never commit them, and never attach them to an
issue.
