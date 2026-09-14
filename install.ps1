#Requires -Version 5.1
<#
.SYNOPSIS
    Stage 2 - Processing : Windows installer.

.DESCRIPTION
    Installs the complete, ready-to-use app on a Windows 10/11 PC:

      - the app + Desktop and Start Menu shortcuts
            -> %LOCALAPPDATA%\Programs\Stage 2 - Processing\
      - the user guide (the in-app "Guide" button finds it here)
            -> %LOCALAPPDATA%\Lifted\Guides\
      - LibreOffice, if it is missing (converts Office files properly)

    This repository is PUBLIC, so the download is an ordinary HTTPS request.
    No GitHub account, no GitHub CLI, no sign-in, no Git and no Python are
    needed, and no administrator rights.

    Colleagues normally never call this script by hand - they paste the
    one-line command from the README, which fetches and runs this file.

.PARAMETER Tag
    Install a specific release (e.g. "v1.5.5"). Default: the latest published
    release, which by definition excludes drafts and pre-releases.

.PARAMETER SkipLibreOffice
    Do not install LibreOffice. Office files (.docx/.xlsx/.pptx) then convert
    with a text-only fallback, which classifies noticeably worse. Advanced use
    only - the default is to install it.

.PARAMETER WithAuditReview
    Also set this machine up to run the post-run AI document review
    (Reports -> AI Document Review). Off by default, because a colleague who
    only processes documents never needs it and it adds a Python install and a
    source download.

    It provisions the three things prepare_workflow() actually requires and
    that a fresh PC does not have:

      1. a Stage 2 source folder containing src\ai_review.py and
         src\Stage2_Processing.pyw - downloaded for the SAME release being
         installed, so the review code matches the app. No Git is involved:
         the audit-review role only needs those files, not a .git directory
         (that is required for Improve Stage 2 / code learning, which this
         switch deliberately does not enable).
      2. a real Python, because resolve_python_runtime() will not use the
         frozen exe's own interpreter.
      3. the paths written into Stage 2's settings, so there is nothing left
         to pick in Settings -> AI workflows.

    The workspace folder and the Master review ledger are NOT provisioned:
    Stage 2 defaults and creates both on demand, and the audit-review role
    never requires the ledger to exist beforehand.

    You still have to sign in to the Codex or Claude CLI yourself - that is an
    account action this installer must not attempt.

.PARAMETER CheckOnly
    Report what this PC has and which version would be installed, then stop
    without downloading or changing anything.

.PARAMETER KeepDownload
    Keep the downloaded files instead of deleting them afterwards.

.EXAMPLE
    .\install.ps1
.EXAMPLE
    .\install.ps1 -CheckOnly
.EXAMPLE
    .\install.ps1 -SkipLibreOffice

.NOTES
    Exit codes: 0 success | 2 prerequisites | 4 release/asset not found
                5 download or checksum failure | 6 install failed
                7 post-install check failed
    Log: %LOCALAPPDATA%\Lifted\Logs\Stage2-install.log
#>
[CmdletBinding()]
param(
    [string] $Tag,
    [switch] $SkipLibreOffice,
    [switch] $WithAuditReview,
    [switch] $CheckOnly,
    [switch] $KeepDownload
)

$ErrorActionPreference = 'Stop'
# PowerShell 7.4+ turns a non-zero native exit code into a terminating error
# when ErrorActionPreference is Stop. We check exit codes explicitly and want
# our own friendly messages, so switch that off where the variable exists.
$PSNativeCommandUseErrorActionPreference = $false

# Windows PowerShell renders a progress bar for every Invoke-WebRequest chunk,
# which makes an 85 MB download many times slower. Turn it off before any
# download happens.
$ProgressPreference = 'SilentlyContinue'

# Un-patched Windows PowerShell 5.1 still defaults to TLS 1.0, which GitHub
# refuses outright - the download would fail with a bare "connection closed".
try {
    [Net.ServicePointManager]::SecurityProtocol =
        [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

# When Windows PowerShell is started from a process PowerShell 7 set the
# environment for, it inherits PowerShell 7's PSModulePath, finds PowerShell
# 7's copies of its own modules first and refuses to load them - which makes
# Get-FileHash silently stop existing. Drop the PowerShell 7 entries.
if ($PSVersionTable.PSEdition -ne 'Core') {
    $kept = @($env:PSModulePath -split ';' | Where-Object {
        $_ -and -not (($_ -match '(?i)microsoft\.powershell_') -or
                      (($_ -match '(?i)\\PowerShell\\') -and ($_ -notmatch '(?i)\\WindowsPowerShell\\')))
    })
    $systemModules = Join-Path $env:SystemRoot 'system32\WindowsPowerShell\v1.0\Modules'
    if ($kept -notcontains $systemModules) { $kept += $systemModules }
    $env:PSModulePath = ($kept -join ';')
}

# --- constants ---------------------------------------------------------------
$Repo        = 'alijabbar04/lifted-stage2-processing'
$AppName     = 'Stage 2 - Processing'
$AppAsset    = 'Stage2_Processing.exe'
$GuideAsset  = 'Stage2_Guide_AI_Processing.pdf'
$SumsAsset   = 'SHA256SUMS.txt'
# The app locates a guide by matching a filename that STARTS WITH "stage 2"
# (see find_guide_pdf in the source), so it must land under this exact name,
# not the underscored name GitHub serves.
$GuideName   = 'Stage 2 Guide - AI Processing.pdf'
$AppDir      = Join-Path $env:LOCALAPPDATA ('Programs\' + $AppName)
$AppExe      = Join-Path $AppDir ($AppName + '.exe')
$GuideDir    = Join-Path $env:LOCALAPPDATA 'Lifted\Guides'
$LogDir      = Join-Path $env:LOCALAPPDATA 'Lifted\Logs'
$LogPath     = Join-Path $LogDir 'Stage2-install.log'
$TotalSteps  = if ($WithAuditReview) { 7 } else { 6 }

# The per-user folders Stage 2 works in. The app creates each of these on
# demand, so this is not load-bearing - but creating them up front means a
# brand-new machine has the whole layout from minute one, the folders are
# there to be browsed before the first run, and anything that cannot be
# created (a redirected or locked profile) is reported now rather than
# halfway through someone's first batch.
#
# Deliberately NOT created here:
#   - the Master AI review ledger workbook. ai_review.sync_records creates
#     both its folder and the workbook, with the exact sheets it needs, the
#     first time a review is recorded. An empty .xlsx planted here would add
#     nothing and could go stale or sit locked by Excel.
#   - anything under C:\Lifted. That is a machine-wide legacy location that
#     Stage 2 only uses if it already exists; a per-user path is correct on a
#     new PC and needs no admin rights.
$DataDirs = [ordered]@{
    'Lifted\Guides'                = 'user guides (the in-app Guide button)'
    'Lifted\Logs'                  = 'install and run logs'
    'Lifted\Stage2\runs'           = 'run snapshots and activity logs'
    'Lifted\Stage2\ai-workflows'   = 'AI review workspace (maintainer feature)'
    'Lifted\Stage2Diagnostics'     = 'crash and window diagnostics'
}

# A unique working folder per run: two installers running at once must not
# share a directory, and the name is what the cleanup check keys on.
$WorkDir     = Join-Path $env:TEMP ('Stage2Install-' + [guid]::NewGuid().ToString('N'))

# =============================================================================
#  Output and logging
# =============================================================================
# Console output stays short and friendly; the log carries the detail we would
# need to diagnose a colleague's failure remotely. Nothing secret is written:
# this installer never sees an API key (that is pasted into the app on first
# run and kept in the Windows Credential Manager) and never sees a token.

function Write-Log {
    param([string] $Text, [string] $Level = 'INFO')
    try {
        if (-not (Test-Path -LiteralPath $LogDir)) {
            New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
        }
        $stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
        Add-Content -LiteralPath $LogPath -Value ("$stamp  $Level  $Text") -ErrorAction SilentlyContinue
    } catch {
        # Logging must never be the reason an install fails.
    }
}

function Write-Step {
    param([int] $Number, [string] $Text)
    Write-Host ""
    Write-Host ("[{0}/{1}] {2}" -f $Number, $TotalSteps, $Text) -ForegroundColor Cyan
    Write-Log -Text "STEP $Number/$TotalSteps - $Text"
}

function Write-Ok   { param([string] $T) Write-Host ("      OK   " + $T) -ForegroundColor Green;  Write-Log -Text "OK - $T" }
function Write-Info { param([string] $T) Write-Host ("           " + $T) -ForegroundColor Gray;   Write-Log -Text "   $T" }
function Write-Note { param([string] $T) Write-Host ("      NOTE " + $T) -ForegroundColor Yellow; Write-Log -Text "NOTE - $T" -Level 'WARN' }

# Delete the working folder - but only if it really is the one we made, under
# the real temp folder, with the exact name shape we generated. A cleanup step
# that trusts a variable is how installers delete the wrong directory.
function Remove-WorkDir {
    if ($KeepDownload) { return }
    try {
        if (-not (Test-Path -LiteralPath $WorkDir)) { return }
        $resolved = [IO.Path]::GetFullPath($WorkDir)
        $tempRoot = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\') + '\'
        if ($resolved.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -and
            [IO.Path]::GetFileName($resolved) -match '^Stage2Install-[0-9a-f]{32}$') {
            Remove-Item -LiteralPath $resolved -Recurse -Force -ErrorAction SilentlyContinue
        } else {
            Write-Log -Text "Refused to clean an unexpected working folder: $resolved" -Level 'WARN'
        }
    } catch {
        Write-Log -Text ("Cleanup failed: " + $_.Exception.Message) -Level 'WARN'
    }
}

function Stop-WithProblem {
    param(
        [string]   $Problem,
        [string[]] $WhatToDo = @(),
        [int]      $Code = 1,
        [string]   $Detail
    )
    Write-Log -Text "FAILED ($Code) - $Problem" -Level 'ERROR'
    if ($Detail) { Write-Log -Text "DETAIL - $Detail" -Level 'ERROR' }
    Write-Host ""
    Write-Host "-----------------------------------------------------------------" -ForegroundColor Red
    Write-Host ("PROBLEM: " + $Problem) -ForegroundColor Red
    if ($WhatToDo.Count -gt 0) {
        Write-Host ""
        Write-Host "What to do:" -ForegroundColor Yellow
        foreach ($line in $WhatToDo) { Write-Host ("  " + $line) -ForegroundColor Yellow }
    }
    Write-Host ""
    Write-Host ("A log for the maintainer is at: " + $LogPath) -ForegroundColor DarkGray
    Write-Host "-----------------------------------------------------------------" -ForegroundColor Red
    Remove-WorkDir
    exit $Code
}

# =============================================================================
#  Helpers
# =============================================================================

# Run a native command and return its exit code, without letting anything it
# writes to stderr become a terminating error. Windows PowerShell turns a
# native command's REDIRECTED stderr into an error record, and with
# ErrorActionPreference = Stop that aborts the script - on output that is
# often purely informational (winget progress, for one).
function Invoke-Native {
    param(
        [Parameter(Mandatory)][string]   $Exe,
        [Parameter(Mandatory)][string[]] $Arguments,
        [switch] $Show
    )
    $ErrorActionPreference = 'Continue'   # function-local
    $captured = New-Object System.Collections.Generic.List[string]
    foreach ($item in (& $Exe @Arguments 2>&1)) {
        $line = if ($item -is [System.Management.Automation.ErrorRecord]) { $item.ToString() } else { [string]$item }
        $captured.Add($line)
        if ($Show -and $line.Trim()) { Write-Host ("           " + $line) -ForegroundColor DarkGray }
    }
    $code = $LASTEXITCODE
    $script:NativeOutput = ($captured -join "`n")
    Write-Log -Text "$Exe exited $code"
    if ($script:NativeOutput) { Write-Log -Text ("output: " + $script:NativeOutput) }
    return $code
}

# Download one URL to one file. Returns $true only when the file really exists
# afterwards - never "probably worked".
function Invoke-Download {
    param([Parameter(Mandatory)][string] $Url, [Parameter(Mandatory)][string] $Destination)
    $script:DownloadError = $null
    Write-Log -Text "GET $Url"
    try {
        Invoke-WebRequest -Uri $Url -OutFile $Destination -UseBasicParsing -TimeoutSec 900
    } catch {
        $script:DownloadError = $_.Exception.Message
        Write-Log -Text ("download failed: " + $script:DownloadError) -Level 'ERROR'
        return $false
    }
    if (-not (Test-Path -LiteralPath $Destination -PathType Leaf)) {
        $script:DownloadError = 'the file was not written'
        return $false
    }
    if ((Get-Item -LiteralPath $Destination).Length -le 0) {
        $script:DownloadError = 'the file came back empty'
        Remove-Item -LiteralPath $Destination -Force -ErrorAction SilentlyContinue
        return $false
    }
    return $true
}

function Test-LooksLikeNetworkFailure {
    param([string] $Text)
    if (-not $Text) { return $false }
    return ($Text -match 'remote name could not be resolved|No such host|actively refused|timed out|Unable to connect|SSL/TLS|underlying connection was closed|proxy')
}

# Read one SHA-256 from a SHA256SUMS.txt line ("<64 hex> *<name>") and compare.
# The manifest must name the asset exactly once - an ambiguous manifest is a
# failure, not something to pick the first match from.
function Assert-Checksum {
    param(
        [Parameter(Mandatory)][string] $ManifestPath,
        [Parameter(Mandatory)][string] $FilePath,
        [Parameter(Mandatory)][string] $AssetName
    )
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        throw "the checksum list is missing."
    }
    if (-not (Test-Path -LiteralPath $FilePath -PathType Leaf)) {
        throw "$AssetName was not downloaded."
    }
    $pattern = '^([0-9A-Fa-f]{64}) \*' + [regex]::Escape($AssetName) + '$'
    $entries = @(Get-Content -LiteralPath $ManifestPath | Where-Object { $_ -match $pattern })
    if ($entries.Count -ne 1) {
        throw "the checksum list should name $AssetName exactly once, but names it $($entries.Count) times."
    }
    $expected = ([regex]::Match($entries[0], $pattern)).Groups[1].Value.ToUpperInvariant()
    $actual   = (Get-FileHash -Algorithm SHA256 -LiteralPath $FilePath).Hash.ToUpperInvariant()
    Write-Log -Text "checksum $AssetName expected=$expected actual=$actual"
    if ($actual -ne $expected) {
        throw "$AssetName does not match its published checksum."
    }
    return $actual
}

# ---- LibreOffice -------------------------------------------------------------
# Deliberately does NOT rely on PATH alone: winget's LibreOffice package does
# not put soffice.exe on PATH, and even when something does, this process's
# PATH is a snapshot taken before the install ran.
function Find-LibreOffice {
    $candidates = New-Object System.Collections.Generic.List[string]

    # 1. PATH, if it happens to be there.
    $onPath = Get-Command soffice.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($onPath) { $candidates.Add($onPath.Source) }

    # 2. The registry is authoritative and needs no PATH refresh. App Paths is
    #    what Explorer itself uses to resolve "soffice".
    foreach ($key in @('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\soffice.exe',
                       'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\soffice.exe')) {
        try {
            $entry = Get-ItemProperty -Path $key -ErrorAction SilentlyContinue
            if ($entry -and $entry.'(default)') { $candidates.Add([string]$entry.'(default)') }
        } catch { }
    }
    # LibreOffice records its own install path too.
    foreach ($key in @('HKLM:\SOFTWARE\LibreOffice\UNO\InstallPath',
                       'HKLM:\SOFTWARE\WOW6432Node\LibreOffice\UNO\InstallPath')) {
        try {
            $entry = Get-ItemProperty -Path $key -ErrorAction SilentlyContinue
            if ($entry -and $entry.'(default)') {
                $candidates.Add((Join-Path ([string]$entry.'(default)') 'soffice.exe'))
            }
        } catch { }
    }

    # 3. Ordinary install locations, including a per-user install.
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)},
                        (Join-Path $env:LOCALAPPDATA 'Programs'))) {
        if ($base) { $candidates.Add((Join-Path $base 'LibreOffice\program\soffice.exe')) }
    }

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            # soffice.bin sitting alongside proves this is a real installation
            # rather than a stub or a stale shortcut target.
            $binary = Join-Path (Split-Path -Parent $candidate) 'soffice.bin'
            if (Test-Path -LiteralPath $binary -PathType Leaf) {
                Write-Log -Text "LibreOffice found at $candidate"
                return $candidate
            }
        }
    }
    Write-Log -Text 'LibreOffice not found'
    return $null
}

function Find-Winget {
    $cmd = Get-Command winget.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { return $cmd.Source }
    # App Installer puts winget in WindowsApps, which is on PATH for interactive
    # sessions but not always for a process started another way.
    $candidate = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\winget.exe'
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    return $null
}

function Test-AppRunning {
    return (@(Get-Process -ErrorAction SilentlyContinue |
              Where-Object { $_.ProcessName -eq $AppName }).Count -gt 0)
}

# =============================================================================
#  Optional: post-run AI document review prerequisites
# =============================================================================
# Only reached with -WithAuditReview. See the .PARAMETER notes above for why
# each of these is genuinely required and why the ledger is not.

# Stage 2 keeps its settings here. The API key is deliberately NOT in this file
# (the app resolves it from the Windows Credential Manager), so writing to it
# never risks a secret.
$ConfigPath   = Join-Path $env:APPDATA 'DocReviewAIStation\config.json'
$SourceRoot   = Join-Path $env:LOCALAPPDATA 'Lifted\Stage2\source'
$WorkspaceDir = Join-Path $env:LOCALAPPDATA 'Lifted\Stage2\ai-workflows'

function Test-Stage2Source {
    param([string] $Root)
    if (-not $Root) { return $false }
    foreach ($needed in 'src\ai_review.py', 'src\Stage2_Processing.pyw') {
        if (-not (Test-Path -LiteralPath (Join-Path $Root $needed) -PathType Leaf)) { return $false }
    }
    return $true
}

# Download the source for the exact release being installed, so the review code
# and the installed app are the same version. Public repo, so this is a plain
# anonymous HTTPS request - no Git, no GitHub account.
function Install-Stage2Source {
    param([Parameter(Mandatory)][string] $ReleaseTag)

    $zip     = Join-Path $WorkDir 'stage2-source.zip'
    $extract = Join-Path $WorkDir 'stage2-source'
    $url     = "https://github.com/$Repo/archive/refs/tags/$ReleaseTag.zip"

    if (-not (Invoke-Download -Url $url -Destination $zip)) {
        Write-Log -Text ("source download failed: " + $script:DownloadError) -Level 'WARN'
        return $null
    }
    try {
        Expand-Archive -LiteralPath $zip -DestinationPath $extract -Force
    } catch {
        Write-Log -Text ("source extract failed: " + $_.Exception.Message) -Level 'WARN'
        return $null
    }

    # GitHub wraps the tree in a single "<repo>-<version>" folder. Find it
    # rather than guessing the name, which varies with how the tag is written.
    $inner = @(Get-ChildItem -LiteralPath $extract -Directory -ErrorAction SilentlyContinue |
               Where-Object { Test-Stage2Source $_.FullName }) | Select-Object -First 1
    if (-not $inner) {
        Write-Log -Text 'downloaded source does not contain src\ai_review.py' -Level 'WARN'
        return $null
    }

    # Replace the previous copy wholesale: a half-updated source tree would be
    # worse than none. The staging copy is already complete and verified to
    # contain the files that matter, so the swap is the last step.
    try {
        if (Test-Path -LiteralPath $SourceRoot) {
            Remove-Item -LiteralPath $SourceRoot -Recurse -Force
        }
        New-Item -ItemType Directory -Path (Split-Path -Parent $SourceRoot) -Force | Out-Null
        Move-Item -LiteralPath $inner.FullName -Destination $SourceRoot -Force
    } catch {
        Write-Log -Text ("could not place the source: " + $_.Exception.Message) -Level 'WARN'
        return $null
    }
    if (-not (Test-Stage2Source $SourceRoot)) { return $null }
    return $SourceRoot
}

# resolve_python_runtime() skips the frozen exe's own interpreter, so the
# review needs a real Python on the machine. It looks in
# %LOCALAPPDATA%\Programs\Python\Python*\python.exe and on PATH.
function Find-RealPython {
    $candidates = New-Object System.Collections.Generic.List[string]
    $base = Join-Path $env:LOCALAPPDATA 'Programs\Python'
    if (Test-Path -LiteralPath $base) {
        foreach ($dir in @(Get-ChildItem -LiteralPath $base -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
                           Sort-Object Name -Descending)) {
            $candidates.Add((Join-Path $dir.FullName 'python.exe'))
        }
    }
    foreach ($cmd in @(Get-Command python.exe -All -ErrorAction SilentlyContinue)) {
        if ($cmd.Source) { $candidates.Add($cmd.Source) }
    }
    foreach ($candidate in $candidates) {
        # A zero-length python.exe is the Microsoft Store stub, not an
        # interpreter - the same trap setup.ps1 guards against.
        if ((Test-Path -LiteralPath $candidate -PathType Leaf) -and
            ((Get-Item -LiteralPath $candidate).Length -gt 0)) {
            return $candidate
        }
    }
    return $null
}

# Merge the three paths into config.json without disturbing anything else in
# it. Read, modify, write - never replace the file wholesale, because it also
# holds the user's model, care-home and account choices.
function Set-WorkflowPaths {
    param([Parameter(Mandatory)][string] $Source)
    $config = @{}
    if (Test-Path -LiteralPath $ConfigPath -PathType Leaf) {
        try {
            $raw = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8
            if ($raw.Trim()) {
                $parsed = $raw | ConvertFrom-Json
                foreach ($property in $parsed.PSObject.Properties) { $config[$property.Name] = $property.Value }
            }
        } catch {
            # An unreadable config is the user's, not ours to overwrite.
            Write-Log -Text ("config.json could not be read: " + $_.Exception.Message) -Level 'WARN'
            return $false
        }
    }

    $workflows = @{}
    if ($config.ContainsKey('ai_workflows') -and $config['ai_workflows']) {
        foreach ($property in $config['ai_workflows'].PSObject.Properties) {
            $workflows[$property.Name] = $property.Value
        }
    }

    # Only fill in what is not already set: someone who has deliberately
    # pointed Stage 2 at their own checkout keeps it.
    $changed = $false
    foreach ($pair in @(@{ K = 'source_root';    V = $Source },
                        @{ K = 'workspace_root'; V = $WorkspaceDir })) {
        $current = [string]$workflows[$pair.K]
        if (-not $current.Trim()) { $workflows[$pair.K] = $pair.V; $changed = $true }
    }
    if (-not $changed) { return $true }

    $config['ai_workflows'] = $workflows
    try {
        New-Item -ItemType Directory -Path (Split-Path -Parent $ConfigPath) -Force | Out-Null
        $json = ($config | ConvertTo-Json -Depth 12)
        $utf8 = New-Object Text.UTF8Encoding($false)
        # Write beside the target and move into place, so an interrupted write
        # cannot leave Stage 2 with a truncated config.
        $temp = $ConfigPath + '.tmp'
        [IO.File]::WriteAllText($temp, $json, $utf8)
        Move-Item -LiteralPath $temp -Destination $ConfigPath -Force
        Write-Log -Text "registered source_root and workspace_root in config.json"
        return $true
    } catch {
        Write-Log -Text ("could not write config.json: " + $_.Exception.Message) -Level 'WARN'
        return $false
    }
}

# Create Stage 2's per-user data folders. Idempotent: an existing folder is
# left exactly as it is, with whatever is already in it.
function New-DataFolders {
    $failures = New-Object System.Collections.Generic.List[string]
    foreach ($entry in $DataDirs.GetEnumerator()) {
        $path = Join-Path $env:LOCALAPPDATA $entry.Key
        try {
            if (-not (Test-Path -LiteralPath $path)) {
                New-Item -ItemType Directory -Path $path -Force | Out-Null
                Write-Log -Text ("created " + $path + "  (" + $entry.Value + ")")
            }
        } catch {
            $failures.Add($entry.Key)
            Write-Log -Text ("could not create " + $path + ": " + $_.Exception.Message) -Level 'WARN'
        }
    }
    return $failures
}

# =============================================================================
Write-Log -Text '==================================================================='
Write-Log -Text ("Stage 2 installer starting. Windows " + [Environment]::OSVersion.Version +
                 ", PowerShell " + $PSVersionTable.PSVersion + " (" + $PSVersionTable.PSEdition + ")" +
                 ", " + $env:PROCESSOR_ARCHITECTURE)

Write-Host ""
Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host "  Stage 2 - Processing : installer" -ForegroundColor Cyan
Write-Host "=================================================================" -ForegroundColor Cyan

New-Item -ItemType Directory -Path $WorkDir -Force | Out-Null

# -----------------------------------------------------------------------------
# [1/6] Check the system
# -----------------------------------------------------------------------------
Write-Step 1 "Checking your system..."

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin  = (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole(
                [Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isAdmin) {
    Write-Note "This PowerShell window is running as Administrator."
    Write-Info "Stage 2 installs into the CURRENT user's profile:"
    Write-Info ("  " + $env:USERPROFILE)
    Write-Info "If that is not your normal account, close this and use an ordinary"
    Write-Info "PowerShell window instead. Administrator rights are never needed."
} else {
    Write-Ok "No administrator rights needed."
}

# A running copy holds its own exe open, so the file could not be replaced.
# -CheckOnly writes nothing, so it must stay usable while the app is open -
# that is exactly when someone is likely to be diagnosing a problem.
$appRunning = Test-AppRunning
if ($appRunning -and -not $CheckOnly) {
    Stop-WithProblem -Code 2 `
        -Problem "'$AppName' is open at the moment, so its files cannot be replaced." `
        -WhatToDo @("Close the Stage 2 window (finish or stop any run first),",
                    "then start this installer again.")
}
if ($appRunning) {
    Write-Note "Stage 2 is open. Nothing is being changed, so this check still works."
    Write-Info "You would need to close it before a real install."
} else {
    Write-Ok "Nothing is in the way."
}

# -----------------------------------------------------------------------------
# [2/6] Work out which version to install
# -----------------------------------------------------------------------------
Write-Step 2 "Finding the latest version of Stage 2..."

# This repository is public, so the GitHub API answers without any credential.
# "releases/latest" is the endpoint GitHub guarantees never returns a draft or
# a pre-release, which is exactly the promise a colleague needs.
$apiUrl = if ($Tag) { "https://api.github.com/repos/$Repo/releases/tags/$Tag" }
          else      { "https://api.github.com/repos/$Repo/releases/latest" }

$release = $null
try {
    $release = Invoke-RestMethod -Uri $apiUrl -UseBasicParsing -TimeoutSec 60 -Headers @{
        'Accept'     = 'application/vnd.github+json'
        'User-Agent' = 'Lifted-Stage2-Installer'
    }
} catch {
    $message = $_.Exception.Message
    Write-Log -Text ("release lookup failed: " + $message) -Level 'ERROR'
    if (Test-LooksLikeNetworkFailure $message) {
        Stop-WithProblem -Code 4 `
            -Problem "Could not reach GitHub." `
            -WhatToDo @("Check your internet connection (and your VPN, if you use one),",
                        "then run the install command again.") `
            -Detail $message
    }
    if ($Tag) {
        Stop-WithProblem -Code 4 `
            -Problem "There is no published release tagged '$Tag'." `
            -WhatToDo @("Leave the version out to install the latest one.") -Detail $message
    }
    Stop-WithProblem -Code 4 `
        -Problem "Could not find a published release of Stage 2." `
        -WhatToDo @("Nothing is wrong with your PC - tell the maintainer.") -Detail $message
}

$releaseTag = $release.tag_name
$assetNames = @($release.assets | ForEach-Object { $_.name })
Write-Log -Text ("release $releaseTag assets: " + ($assetNames -join ', '))

foreach ($needed in @($AppAsset, $GuideAsset, $SumsAsset)) {
    if ($assetNames -notcontains $needed) {
        Stop-WithProblem -Code 4 `
            -Problem ("Release " + $releaseTag + " is missing " + $needed + ".") `
            -WhatToDo @("Nothing is wrong with your PC - the release is incomplete.",
                        "Tell the maintainer, and try again once they have fixed it.") `
            -Detail ("Assets on that release: " + ($assetNames -join ', '))
    }
}

$appSize = ($release.assets | Where-Object { $_.name -eq $AppAsset } | Select-Object -First 1).size
$appMb   = [math]::Max(1, [math]::Round($appSize / 1MB, 0))
Write-Ok ("Version " + $releaseTag + " is the one to install.")

$isUpdate = Test-Path -LiteralPath $AppExe
if ($isUpdate) {
    Write-Info "An existing installation was found - it will be updated in place."
    Write-Info "Your settings, saved API key and work are not touched."
}

if ($CheckOnly) {
    $office = Find-LibreOffice
    Write-Host ""
    Write-Host "-----------------------------------------------------------------" -ForegroundColor Green
    Write-Host "Check complete - nothing was downloaded or changed." -ForegroundColor Green
    Write-Host ("  Would install : " + $releaseTag + "  (" + $appMb + " MB)")
    Write-Host ("  Install folder: " + $AppDir)
    Write-Host ("  Already there : " + $(if ($isUpdate) { 'yes - would update' } else { 'no - first install' }))
    Write-Host ("  LibreOffice   : " + $(if ($office) { $office } else { 'not installed - would be installed' }))
    Write-Host ("  winget        : " + $(if (Find-Winget) { 'available' } else { 'NOT available' }))
    Write-Host ""
    Write-Host "  Stage 2's data folders:"
    foreach ($entry in $DataDirs.GetEnumerator()) {
        $path = Join-Path $env:LOCALAPPDATA $entry.Key
        $state = if (Test-Path -LiteralPath $path) { 'exists ' } else { 'would be created' }
        Write-Host ("    [{0}] {1}" -f $state, $path)
    }
    # The AI review ledger is created by the app the first time a review is
    # recorded - reported here only so a machine can be diagnosed remotely.
    $legacyLedger = 'C:\Lifted\Stage2 Audit Review\Master_Filename_Review_Ledger.xlsx'
    $ledger = if (Test-Path -LiteralPath $legacyLedger) { $legacyLedger }
              else { Join-Path $env:LOCALAPPDATA 'Lifted\Stage2\ai-workflows\Master_Filename_Review_Ledger.xlsx' }
    Write-Host ("    [{0}] {1}" -f $(if (Test-Path -LiteralPath $ledger) { 'exists ' } else { 'made on first review' }), $ledger)
    Write-Host "Re-run without -CheckOnly to install." -ForegroundColor Green
    Write-Host "-----------------------------------------------------------------" -ForegroundColor Green
    Remove-WorkDir
    exit 0
}

# -----------------------------------------------------------------------------
# [3/6] Download
# -----------------------------------------------------------------------------
Write-Step 3 ("Downloading Stage 2 " + $releaseTag + " (about " + $appMb + " MB)...")
Write-Info "This can take a few minutes. Please wait."

# Download by explicit tag rather than the /latest/download/ shortcut, so every
# file in this run provably comes from the SAME release even if the maintainer
# publishes a new one midway through.
$baseUrl       = "https://github.com/$Repo/releases/download/$releaseTag"
$downloadedApp = Join-Path $WorkDir $AppAsset
$downloadedPdf = Join-Path $WorkDir $GuideAsset
$downloadedSum = Join-Path $WorkDir $SumsAsset

foreach ($item in @(
    @{ Name = $SumsAsset;  Path = $downloadedSum; What = 'the checksum list' },
    @{ Name = $AppAsset;   Path = $downloadedApp; What = 'the app' },
    @{ Name = $GuideAsset; Path = $downloadedPdf; What = 'the user guide' })) {
    if (-not (Invoke-Download -Url ($baseUrl + '/' + $item.Name) -Destination $item.Path)) {
        if (Test-LooksLikeNetworkFailure $script:DownloadError) {
            Stop-WithProblem -Code 5 `
                -Problem ("The download of " + $item.What + " was interrupted.") `
                -WhatToDo @("Check your internet connection, then run the install command",
                            "again - it starts over cleanly, nothing is left behind.") `
                -Detail $script:DownloadError
        }
        Stop-WithProblem -Code 5 `
            -Problem ("Could not download " + $item.What + " (" + $item.Name + ").") `
            -WhatToDo @("Run the install command again.",
                        "If it keeps failing, tell the maintainer.") `
            -Detail $script:DownloadError
    }
}
Write-Ok "Download complete."

# --- verify BEFORE anything is installed -------------------------------------
# Nothing downloaded is trusted until it matches the published checksum. On a
# mismatch the binaries are deleted rather than left on disk to be run later.
try {
    $appHash   = Assert-Checksum -ManifestPath $downloadedSum -FilePath $downloadedApp -AssetName $AppAsset
    $guideHash = Assert-Checksum -ManifestPath $downloadedSum -FilePath $downloadedPdf -AssetName $GuideAsset
} catch {
    $reason = $_.Exception.Message
    Remove-Item -LiteralPath $downloadedApp -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $downloadedPdf -Force -ErrorAction SilentlyContinue
    Stop-WithProblem -Code 5 `
        -Problem ("SECURITY CHECK FAILED - " + $reason) `
        -WhatToDo @("The downloaded files were deleted and nothing was installed.",
                    "Run the install command again.",
                    "If it fails the same way twice, tell the maintainer BEFORE retrying -",
                    "do not install Stage 2 from anywhere else in the meantime.") `
        -Detail $reason
}
Write-Ok "Verified against the published checksums."
Write-Log -Text "app sha256 = $appHash; guide sha256 = $guideHash"

# -----------------------------------------------------------------------------
# [4/6] Install
# -----------------------------------------------------------------------------
Write-Step 4 "Installing the app..."

# Re-check: the app may have been started while the download ran. Doing this
# before any file is replaced keeps a working installation working.
if (Test-AppRunning) {
    Stop-WithProblem -Code 6 `
        -Problem "'$AppName' was opened while the download was running." `
        -WhatToDo @("Close it, then run the install command again.")
}

try {
    New-Item -ItemType Directory -Force -Path $AppDir   | Out-Null
    New-Item -ItemType Directory -Force -Path $GuideDir | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $AppDir 'Guides') | Out-Null

    # Stage 2's working folders, so a new machine starts with the full layout.
    $folderFailures = New-DataFolders
    if ($folderFailures.Count -gt 0) {
        # Not fatal: the app creates each of these on demand too. Say so rather
        # than failing an otherwise good install.
        Write-Note ("Could not pre-create " + $folderFailures.Count + " data folder(s).")
        Write-Info "Stage 2 will create them itself when it first needs them."
    }

    # The download is already complete and verified, so replacing the installed
    # files is the last and quickest step - an interrupted or corrupt download
    # can never leave a half-written app behind.
    Copy-Item -LiteralPath $downloadedApp -Destination $AppExe -Force
    Copy-Item -LiteralPath $downloadedPdf -Destination (Join-Path $GuideDir $GuideName) -Force
    Copy-Item -LiteralPath $downloadedPdf -Destination (Join-Path $AppDir ('Guides\' + $GuideName)) -Force
} catch {
    Stop-WithProblem -Code 6 `
        -Problem "The app files could not be written." `
        -WhatToDo @("Make sure Stage 2 is closed, then run the install command again.",
                    "If your PC is managed, antivirus may be blocking the copy -",
                    "send the maintainer the log named below.") `
        -Detail $_.Exception.Message
}
Write-Ok ("Installed to " + $AppDir)

# -----------------------------------------------------------------------------
# [5/6] Shortcuts
# -----------------------------------------------------------------------------
Write-Step 5 "Creating shortcuts..."

# Writing to the same two fixed paths every time is what makes re-running safe:
# an existing shortcut is overwritten, never duplicated, and one somebody
# deleted comes back.
$madeShortcut = $false
try {
    $shell = New-Object -ComObject WScript.Shell
    foreach ($dir in @([Environment]::GetFolderPath('Desktop'),
                       (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'))) {
        if (-not $dir) { continue }
        if (-not (Test-Path -LiteralPath $dir)) { continue }
        $link = $shell.CreateShortcut((Join-Path $dir ($AppName + '.lnk')))
        $link.TargetPath       = $AppExe
        $link.WorkingDirectory = $AppDir
        $link.IconLocation     = $AppExe
        $link.Description      = 'Stage 2 - AI document classification and renaming'
        $link.Save()
        $madeShortcut = $true
    }
} catch {
    Write-Log -Text ('shortcut creation failed: ' + $_.Exception.Message) -Level 'WARN'
}
if ($madeShortcut) {
    Write-Ok "Desktop and Start Menu shortcuts ready."
} else {
    # Not fatal - the app is installed and can be started from its folder.
    Write-Note "Could not create shortcuts."
    Write-Info ("Start the app from: " + $AppExe)
}

# -----------------------------------------------------------------------------
# [6/6] LibreOffice
# -----------------------------------------------------------------------------
Write-Step 6 "LibreOffice (converts Word/Excel/PowerPoint files)..."

$libreOfficeReady = $false
$libreOfficeNote  = $null

if ($SkipLibreOffice) {
    Write-Note "Skipped at your request (-SkipLibreOffice)."
    $libreOfficeNote = 'skipped with -SkipLibreOffice'
} else {
    $soffice = Find-LibreOffice
    if ($soffice) {
        Write-Ok "Already installed."
        Write-Info $soffice
        $libreOfficeReady = $true
    } else {
        Write-Info "Not found. Installing it now - this is the last step."
        $winget = Find-Winget
        if (-not $winget) {
            $libreOfficeNote = "this PC has no 'winget' to install it with"
            Write-Note "This PC has no 'winget', so LibreOffice cannot be installed automatically."
        } else {
            Write-Info "Windows may ask for permission to install it - choose Yes."
            $code = Invoke-Native -Exe $winget -Arguments @(
                'install', '--id', 'TheDocumentFoundation.LibreOffice', '--exact',
                '--source', 'winget', '--accept-package-agreements',
                '--accept-source-agreements') -Show

            # Judge the result by whether LibreOffice is actually there, not by
            # winget's exit code: winget reports "already installed" as a
            # failure code, and a success code does not prove the files landed.
            $soffice = Find-LibreOffice
            if ($soffice) {
                Write-Ok "LibreOffice installed."
                Write-Info $soffice
                Write-Info "Office PDF conversion ready."
                $libreOfficeReady = $true
            } else {
                $libreOfficeNote = "winget finished with code $code but LibreOffice is still not present"
                Write-Note "LibreOffice did not install."
            }
        }
    }
}

# -----------------------------------------------------------------------------
# [7/7] Optional: post-run AI document review
# -----------------------------------------------------------------------------
$auditReviewReady = $false
$auditReviewNotes = New-Object System.Collections.Generic.List[string]

if ($WithAuditReview) {
    Write-Step 7 "Setting up the post-run AI document review..."

    # 1. The source folder - the thing that actually blocks a fresh machine.
    if (Test-Stage2Source $SourceRoot) {
        Write-Info "Updating the Stage 2 source copy..."
    } else {
        Write-Info "Downloading the Stage 2 source for this version..."
    }
    $source = Install-Stage2Source -ReleaseTag $releaseTag
    if ($source) {
        Write-Ok "Review source ready."
        Write-Info $source
    } else {
        $auditReviewNotes.Add("the Stage 2 source copy could not be downloaded")
        Write-Note "Could not set up the review source."
    }

    # 2. A real Python, which the frozen exe cannot stand in for.
    $python = Find-RealPython
    if (-not $python) {
        Write-Info "The review needs a real Python. Installing Python 3.13..."
        $winget = Find-Winget
        if ($winget) {
            Invoke-Native -Exe $winget -Arguments @(
                'install', '--id', 'Python.Python.3.13', '--exact', '--source', 'winget',
                '--silent', '--accept-package-agreements', '--accept-source-agreements') -Show | Out-Null
            # Judge it by finding an interpreter, never by winget's exit code.
            $python = Find-RealPython
        }
    }
    if ($python) {
        Write-Ok "Python available for the review."
        Write-Info $python
    } else {
        $auditReviewNotes.Add("no usable Python was found or installed")
        Write-Note "No Python - the review cannot prepare its queue without one."
    }

    # 3. Write the paths into Stage 2's settings, so nothing is left to pick.
    if ($source) {
        if (Set-WorkflowPaths -Source $source) {
            Write-Ok "Settings filled in - nothing to choose in Settings > AI workflows."
        } else {
            $auditReviewNotes.Add("the paths could not be written to config.json")
            Write-Note "Could not write the settings; pick the folders in Settings instead."
        }
    }

    $auditReviewReady = [bool]($source -and $python)
}

# -----------------------------------------------------------------------------
# Verify what actually landed
# -----------------------------------------------------------------------------
Write-Host ""
$allOk = $true
foreach ($check in @(
    @{ What = 'The app';                       Ok = (Test-Path -LiteralPath $AppExe) },
    @{ What = 'The user guide (Guide button)'; Ok = (Test-Path -LiteralPath (Join-Path $GuideDir $GuideName)) })) {
    if ($check.Ok) {
        Write-Ok $check.What
    } else {
        Write-Host ("      MISS " + $check.What) -ForegroundColor Red
        Write-Log -Text ("MISSING - " + $check.What) -Level 'ERROR'
        $allOk = $false
    }
}
if (-not $allOk) {
    Stop-WithProblem -Code 7 `
        -Problem "The install finished but something did not land on this PC." `
        -WhatToDo @("Run the install command once more.",
                    "If the same piece is missing again, send the maintainer the log below.")
}

Remove-WorkDir
Write-Log -Text "SUCCESS - Stage 2 $releaseTag installed to $AppDir"

# -----------------------------------------------------------------------------
Write-Host ""
Write-Host "=================================================================" -ForegroundColor Green
Write-Host ("  Installation complete - Stage 2 " + $releaseTag) -ForegroundColor Green
Write-Host "=================================================================" -ForegroundColor Green
Write-Host ""
Write-Host "To start:" -ForegroundColor Green
Write-Host ("  Double-click '" + $AppName + "' on your Desktop.")
Write-Host ""
Write-Host "The first time you run it:" -ForegroundColor Green
Write-Host "  Click the cog, paste the Anthropic API key you were given, and save."
Write-Host "  Windows stores it securely for you and you only do it once."
Write-Host "  Ask the maintainer for the key - it is never included in this install."

if ($WithAuditReview) {
    Write-Host ""
    if ($auditReviewReady) {
        Write-Host "Post-run AI document review is set up." -ForegroundColor Green
        Write-Host "  One thing is left, and only you can do it: sign in to the Codex or"
        Write-Host "  Claude CLI you intend to use. Stage 2 verifies that account at launch;"
        Write-Host "  this installer must never sign in on your behalf."
    } else {
        Write-Host "-----------------------------------------------------------------" -ForegroundColor Yellow
        Write-Host "  AI document review is NOT set up" -ForegroundColor Yellow
        Write-Host "-----------------------------------------------------------------" -ForegroundColor Yellow
        Write-Host "Stage 2 itself is installed and processes documents normally." -ForegroundColor Yellow
        foreach ($note in $auditReviewNotes) { Write-Host ("  - " + $note) -ForegroundColor Yellow }
        Write-Host "Re-run the installer with -WithAuditReview to try again." -ForegroundColor Yellow
        Write-Host "-----------------------------------------------------------------" -ForegroundColor Yellow
    }
}

if (-not $libreOfficeReady) {
    Write-Host ""
    Write-Host "-----------------------------------------------------------------" -ForegroundColor Yellow
    Write-Host "  ONE THING IS NOT SET UP: LibreOffice" -ForegroundColor Yellow
    Write-Host "-----------------------------------------------------------------" -ForegroundColor Yellow
    Write-Host "Stage 2 is installed and will run." -ForegroundColor Yellow
    Write-Host "But Word, Excel and PowerPoint files (.docx/.xlsx/.pptx) will be read" -ForegroundColor Yellow
    Write-Host "as plain text only, which classifies them noticeably worse. PDFs and" -ForegroundColor Yellow
    Write-Host "images are unaffected." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "To fix it, run this one line and then restart Stage 2:" -ForegroundColor Yellow
    Write-Host "  winget install --id TheDocumentFoundation.LibreOffice -e" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Stage 2 picks it up automatically afterwards - nothing to configure." -ForegroundColor Yellow
    if ($libreOfficeNote) { Write-Host ("Reason: " + $libreOfficeNote) -ForegroundColor DarkGray }
    Write-Host "-----------------------------------------------------------------" -ForegroundColor Yellow
}

Write-Host ""
Write-Host ("Install log: " + $LogPath) -ForegroundColor DarkGray
Write-Host ""
exit 0
