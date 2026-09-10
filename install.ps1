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
#  Prerequisite: GitHub CLI (the repository itself is public).
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
$Tag       = "v1.5.4"
$AppAsset  = "Stage2_Processing.exe"
$ChecksumAsset = "SHA256SUMS.txt"
$GuideAsset = "Stage2_Guide_AI_Processing.pdf"

# The app finds a guide by matching a filename that STARTS WITH "stage 2"
# (see find_guide_pdf in the source), so it must be saved under this exact
# name - not the underscored asset name GitHub serves.
$GuideName = "Stage 2 Guide - AI Processing.pdf"

$AppDir   = Join-Path $env:LOCALAPPDATA "Programs\Stage 2 - Processing"
$GuideDir = Join-Path $env:LOCALAPPDATA "Lifted\Guides"

function Get-VerifiedReleaseAssetHash {
    param(
        [Parameter(Mandatory)][string]$ManifestPath,
        [Parameter(Mandatory)][string]$AssetPath,
        [Parameter(Mandatory)][string]$AssetName
    )
    if (-not (Test-Path -LiteralPath $AssetPath -PathType Leaf)) {
        throw "Downloaded release asset is missing: $AssetName."
    }
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        throw "Checksum manifest is missing: $ManifestPath."
    }
    $pattern = '^([0-9A-Fa-f]{64}) \*' + [regex]::Escape($AssetName) + '$'
    $entries = @(Get-Content -LiteralPath $ManifestPath |
        Where-Object { $_ -match $pattern })
    if ($entries.Count -ne 1) {
        throw "Checksum manifest must contain exactly one entry for $AssetName (found $($entries.Count))."
    }
    $expected = ([regex]::Match($entries[0], $pattern)).Groups[1].Value.ToUpperInvariant()
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $AssetPath).Hash.ToUpperInvariant()
    if ($actual -ine $expected) {
        throw "Checksum mismatch for $AssetName. Expected $expected; actual $actual."
    }
    return $actual
}

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

# --- 1. GitHub CLI ----------------------------------------------------------
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

# --- 3. Download and verify the app (~75 MB) -------------------------------
$dl = Join-Path $env:TEMP ("Stage2Install-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force $dl | Out-Null
Write-Host "`n[3/5] Downloading the app (~75 MB, one time)..." -ForegroundColor Cyan
if ((Invoke-Native $gh @("release","download",$Tag,"--repo",$Repo,
                         "--pattern",$AppAsset,"--dir",$dl,"--clobber")) -ne 0) {
    Write-Host "Download failed. Most likely your GitHub account has not been" -ForegroundColor Red
    Write-Host "invited to the repo yet - ask the maintainer, accept the emailed"
    Write-Host "invitation, then run install.ps1 again."
    exit 1
}

if ((Invoke-Native $gh @("release","download",$Tag,"--repo",$Repo,
                         "--pattern",$ChecksumAsset,"--dir",$dl,
                         "--clobber")) -ne 0) {
    Write-Host "Checksum download failed; the app has NOT been installed." -ForegroundColor Red
    Write-Host "Ask the maintainer to attach $ChecksumAsset to release $Tag."
    exit 1
}

$DownloadedApp = Join-Path $dl $AppAsset
$ChecksumPath = Join-Path $dl $ChecksumAsset
$DownloadedGuide = Join-Path $dl $GuideAsset
try {
    $ActualHash = Get-VerifiedReleaseAssetHash -ManifestPath $ChecksumPath `
        -AssetPath $DownloadedApp -AssetName $AppAsset
} catch {
    Write-Host "SECURITY ERROR: $($_.Exception.Message)" -ForegroundColor Red
    Remove-Item -LiteralPath $DownloadedApp -Force -ErrorAction SilentlyContinue
    Write-Host "The unverified executable was removed and nothing was installed." -ForegroundColor Red
    exit 1
}
Write-Host "  SHA-256 verified: $ActualHash" -ForegroundColor Green

Write-Host "`n[3/5] Downloading the user guide..." -ForegroundColor Cyan
if ((Invoke-Native $gh @("release","download",$Tag,"--repo",$Repo,
                         "--pattern",$GuideAsset,"--dir",$dl,
                         "--clobber")) -ne 0) {
    Write-Host "Guide download failed for release $Tag; nothing was installed." -ForegroundColor Red
    Write-Host "Check that $GuideAsset is attached to the release, then run install.ps1 again."
    exit 1
}
try {
    $GuideHash = Get-VerifiedReleaseAssetHash -ManifestPath $ChecksumPath `
        -AssetPath $DownloadedGuide -AssetName $GuideAsset
} catch {
    Write-Host "SECURITY ERROR: $($_.Exception.Message)" -ForegroundColor Red
    Remove-Item -LiteralPath $DownloadedGuide -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $DownloadedApp -Force -ErrorAction SilentlyContinue
    Write-Host "The unverified guide was removed and nothing was installed." -ForegroundColor Red
    Write-Host "Obtain a matching $ChecksumAsset from release $Tag, then run install.ps1 again."
    exit 1
}
Write-Host "  Guide SHA-256 verified: $GuideHash" -ForegroundColor Green

# --- 4. Install into the user profile (no admin needed) --------------------
Write-Host "`n[4/5] Installing to $AppDir ..." -ForegroundColor Cyan
New-Item -ItemType Directory -Force $AppDir   | Out-Null
New-Item -ItemType Directory -Force $GuideDir | Out-Null

$appExe = Join-Path $AppDir "Stage 2 - Processing.exe"
Copy-Item -LiteralPath $DownloadedApp -Destination $appExe -Force

New-Item -ItemType Directory -Force (Join-Path $AppDir "Guides") | Out-Null
Copy-Item -LiteralPath $DownloadedGuide -Destination (Join-Path $GuideDir $GuideName) -Force
Copy-Item -LiteralPath $DownloadedGuide -Destination (Join-Path $AppDir "Guides\$GuideName") -Force

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

$ResolvedDownload = [IO.Path]::GetFullPath($dl)
$ResolvedTemp = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\') + '\'
if ($ResolvedDownload.StartsWith($ResolvedTemp, [StringComparison]::OrdinalIgnoreCase) -and
    [IO.Path]::GetFileName($ResolvedDownload) -match '^Stage2Install-[0-9a-f]{32}$') {
    Remove-Item -LiteralPath $ResolvedDownload -Recurse -Force -ErrorAction SilentlyContinue
} else {
    throw "Unexpected download directory; cleanup was skipped."
}

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
