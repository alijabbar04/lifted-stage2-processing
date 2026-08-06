# Installing Stage 2 — Processing

This guide assumes no technical knowledge. Follow it top to bottom.

There are **two ways** to get Stage 2. Pick one.

---

# Option A — Just use the app (recommended)

You do not need Python, this repository, or any developer tools.

## 1. Download it

Go to the repository's **Releases** page and download
**`Stage 2 - Processing.exe`** from the latest release (about 56 MB).

Put it somewhere sensible — your Desktop is fine. It is a single file; there is
nothing to install.

> Windows may warn that the file is "not commonly downloaded" because it is not
> code-signed. Choose **Keep**, then if SmartScreen appears, **More info → Run
> anyway**. This is expected for an internally-built tool.

## 2. Get an API key

Stage 2 reads documents using the Claude API, which needs an Anthropic API key.

**Ask Ali Jabbar for a key.** It is never stored in this repository and never
sent over email or chat where it can be forwarded. Every document you process
bills that key's Anthropic account, so treat it like a company credit card.

## 3. First run

Double-click **`Stage 2 - Processing.exe`**. Then:

1. Click the **cog (⚙) button** to open Settings.
2. Paste the API key into the **API key** field and save.

The key is stored in the **Windows Credential Manager** — the operating
system's encrypted credential store — not in a text file. You only do this
once; it is remembered for every future run.

> If a colleague gave you the packaged **installer** (`Stage2_Processing_Setup.exe`)
> instead of the bare exe, the key was already configured for you during install
> and you can skip step 3 entirely. That installer is deliberately not published
> to GitHub, because it carries the API key inside it.

## 4. Install LibreOffice (optional but recommended)

LibreOffice converts Word / Excel / PowerPoint documents to PDF. Stage 2 works
without it, but those files convert as **text only**, which loses stamps,
signatures and layout — and the classifier reads pages as images, so a
text-only conversion classifies badly.

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
│                           (the 15 types Stage 3 uploads one at a time)
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
3. Choose **Live** (results as it goes) or **Overnight Batch** (half price,
   ready in about an hour, up to 24). Batch is the right choice for a big run
   you can leave overnight.
4. Start it. The cost estimate in GBP updates live from the real token counts
   the API returns. The run stops on its own at the budget ceiling
   (**£25** by default, changeable in Settings).

Read **`docs/USER_GUIDE.pdf`** for the full walkthrough with screenshots. It is
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

The repository is private, so Git will ask you to sign in to GitHub. If you have
no access, ask Ali Jabbar to add your GitHub account.

## 4. Run setup

```powershell
.\setup.ps1
```

This checks your Python version, installs the four packages Stage 2 needs
(`pymupdf`, `pillow`, `openpyxl`, `keyring`), checks for LibreOffice, and then
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

## 6. Rebuild the exe after a change

```powershell
.\build\build.ps1
```

Takes a few minutes and writes `dist\Stage 2 - Processing.exe`. Back up the old
exe before replacing it.

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
