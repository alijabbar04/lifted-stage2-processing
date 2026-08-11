# ============================================================================
#  Stage 2 - Processing : one-command install
#
#  Downloads the app from this repo's GitHub Release and sets it up:
#    - the app + Desktop and Start Menu shortcuts
#         -> %LOCALAPPDATA%\Programs\Stage 2 - Processing\
#    - the user guide (the in-app "Guide" button finds it here)
#         -> %LOCALAPPDATA%\Lifted\Guides\
#  No admin rights and no Python required.
#
#  Usage (see README for the full walkthrough):
#      .\install.ps1
#  Prerequisite: a GitHub account invited to this (private) repo.
#
#  NOTE ON THE API KEY: this installs the app only. You paste your own
#  Anthropic API key on first run (cog -> API key), and it is stored in the
#  Windows Credential Manager. The maintainer also has a full installer that
#  pre-configures the key, but it is deliberately NOT published to GitHub
#  because the key is compiled into it - ask for that one directly if you
#  would rather not paste a key.
# ============================================================================
$ErrorActionPreference = "Stop"
$Repo      = "alijabbar04/lifted-stage2-processing"
$Tag       = "v1.1.0"
$AppAsset  = "Stage2_Processing.exe"
$GuideAsset = "Stage2_Guide_AI_Processing.pdf"

# The app finds a guide by matching a filename that STARTS WITH "stage 2"
# (see find_guide_pdf in the source), so it must be saved under this exact
# name - not the underscored asset name GitHub serves.
$GuideName = "Stage 2 Guide - AI Processing.pdf"

$AppDir   = Join-Path $env:LOCALAPPDATA "Programs\Stage 2 - Processing"
$GuideDir = Join-Path $env:LOCALAPPDATA "Lifted\Guides"

function Find-Gh {
    $cmd = Get-Command gh -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($p in @("$env:ProgramFiles\GitHub CLI\gh.exe",
                     "$env:LOCALAPPDATA\Programs\GitHub CLI\gh.exe")) {
        if (Test-Path $p) { return $p }
    }
    return $null
}

# Run a native exe and return its exit code, WITHOUT letting anything it writes
# to stderr abort the script. Needed because $ErrorActionPreference = "Stop"
# turns native-command stderr into a terminating error in Windows PowerShell
# 5.1 - and `gh auth status` writes to stderr on the completely normal
# "not signed in yet" path, which would otherwise kill the install with a
# confusing NativeCommandError instead of starting the sign-in flow.
# $Interactive leaves the streams alone so browser/device-code prompts work.
function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments, [switch]$Quiet, [switch]$Interactive)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        if ($Interactive)  { & $Exe @Arguments }
        elseif ($Quiet)    { & $Exe @Arguments 2>&1 | Out-Null }
        else               { & $Exe @Arguments 2>&1 | ForEach-Object { Write-Host $_ } }
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
}

Write-Host "=== Stage 2 - Processing : install ===" -ForegroundColor Cyan

# --- 1. GitHub CLI (needed because the repo is private) ---------------------
$gh = Find-Gh
if (-not $gh) {
    Write-Host "`n[1/5] GitHub CLI not found - installing it with winget..." -ForegroundColor Cyan
    Invoke-Native "winget" @("install","--id","GitHub.cli","-e",
                             "--accept-source-agreements",
                             "--accept-package-agreements") | Out-Null
    $gh = Find-Gh
    if (-not $gh) {
        Write-Host "GitHub CLI was installed but isn't visible yet." -ForegroundColor Yellow
        Write-Host "Close this PowerShell window, open a new one, and run install.ps1 again."
        exit 1
    }
} else {
    Write-Host "`n[1/5] GitHub CLI found." -ForegroundColor Cyan
}

# --- 2. GitHub sign-in ------------------------------------------------------
if ((Invoke-Native $gh @("auth","status") -Quiet) -ne 0) {
    Write-Host "`n[2/5] Signing in to GitHub - a browser window will open." -ForegroundColor Cyan
    Write-Host "Use the GitHub account that was invited to this repository."
    # -Interactive: the web/device-code flow needs the real console.
    if ((Invoke-Native $gh @("auth","login","--hostname","github.com",
                             "--web","--git-protocol","https") -Interactive) -ne 0) {
        Write-Host "GitHub sign-in did not complete - run install.ps1 again to retry." -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "`n[2/5] Already signed in to GitHub." -ForegroundColor Cyan
}

# --- 3. Download the app (~57 MB) ------------------------------------------
$dl = Join-Path $env:TEMP "Stage2Install"
New-Item -ItemType Directory -Force $dl | Out-Null
Write-Host "`n[3/5] Downloading the app (~57 MB, one time)..." -ForegroundColor Cyan
if ((Invoke-Native $gh @("release","download",$Tag,"--repo",$Repo,
                         "--pattern",$AppAsset,"--dir",$dl,"--clobber")) -ne 0) {
    Write-Host "Download failed. Most likely your GitHub account has not been" -ForegroundColor Red
    Write-Host "invited to the repo yet - ask the maintainer, accept the emailed"
    Write-Host "invitation, then run install.ps1 again."
    exit 1
}

# The user guide is a separate, small asset. Not fatal if it is missing.
$guideOk = (Invoke-Native $gh @("release","download",$Tag,"--repo",$Repo,
                                "--pattern",$GuideAsset,"--dir",$dl,
                                "--clobber") -Quiet) -eq 0

# --- 4. Install into the user profile (no admin needed) --------------------
Write-Host "`n[4/5] Installing to $AppDir ..." -ForegroundColor Cyan
New-Item -ItemType Directory -Force $AppDir   | Out-Null
New-Item -ItemType Directory -Force $GuideDir | Out-Null

$appExe = Join-Path $AppDir "Stage 2 - Processing.exe"
Copy-Item (Join-Path $dl $AppAsset) $appExe -Force

if ($guideOk) {
    Copy-Item (Join-Path $dl $GuideAsset) (Join-Path $GuideDir $GuideName) -Force
}

# Desktop + Start Menu shortcuts
$shell = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath("Desktop"),
                   (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"))) {
    $lnk = $shell.CreateShortcut((Join-Path $dir "Stage 2 - Processing.lnk"))
    $lnk.TargetPath       = $appExe
    $lnk.WorkingDirectory = $AppDir
    $lnk.IconLocation     = $appExe
    $lnk.Description      = "Stage 2 - AI document classification and renaming"
    $lnk.Save()
}

# --- 5. LibreOffice check (optional dependency) ----------------------------
Write-Host "`n[5/5] Checking for LibreOffice (converts Office files to PDF)..." -ForegroundColor Cyan
$soffice = @("$env:ProgramFiles\LibreOffice\program\soffice.exe",
             "${env:ProgramFiles(x86)}\LibreOffice\program\soffice.exe") |
           Where-Object { Test-Path $_ } | Select-Object -First 1
if ($soffice) {
    Write-Host "  Found LibreOffice." -ForegroundColor Green
} else {
    Write-Host "  Not installed (OPTIONAL, but recommended)." -ForegroundColor Yellow
    Write-Host "  Word/Excel/PowerPoint files convert as TEXT-ONLY without it, which"
    Write-Host "  classifies badly. Install it any time with:"
    Write-Host "      winget install --id TheDocumentFoundation.LibreOffice -e" -ForegroundColor Cyan
    Write-Host "  Stage 2 picks it up automatically afterwards."
}

# --- Verify -----------------------------------------------------------------
Write-Host ""
$ok = $true
foreach ($check in @(
    @{ Path = $appExe;                            What = "App + shortcuts" },
    @{ Path = (Join-Path $GuideDir $GuideName);    What = "User guide (in-app Guide button)" })) {
    if (Test-Path $check.Path) {
        Write-Host ("  OK      " + $check.What) -ForegroundColor Green
    } else {
        Write-Host ("  MISSING " + $check.What + "  -> " + $check.Path) -ForegroundColor Red
        $ok = $false
    }
}

Remove-Item $dl -Recurse -Force -ErrorAction SilentlyContinue

if ($ok) {
    Write-Host "`nInstall complete." -ForegroundColor Green
    Write-Host "Launch 'Stage 2 - Processing' from the Desktop shortcut."
    Write-Host "`nFIRST RUN: click the cog, paste the Anthropic API key you were given," -ForegroundColor Yellow
    Write-Host "and save. It is stored in the Windows Credential Manager, and you only" -ForegroundColor Yellow
    Write-Host "do it once. Ask the maintainer for the key - it is never in this repo." -ForegroundColor Yellow
} else {
    Write-Host "`nSomething is missing - run install.ps1 again, or see" -ForegroundColor Yellow
    Write-Host "docs/TROUBLESHOOTING.md."
    exit 1
}
