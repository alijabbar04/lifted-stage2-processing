#Requires -Version 5.1
<#
.SYNOPSIS
    Stage 2 - Processing : DEVELOPER setup (run from source).

.DESCRIPTION
    This is NOT how colleagues install Stage 2. They run install.ps1 (or
    install.cmd), which needs no Python, no Git and no GitHub account - see the
    README. This script is for working on the source.

    It finds a usable Python, installs one if there is none, creates a project
    virtual environment in .venv, installs the pinned dependencies into THAT
    environment (never the system Python), installs LibreOffice if it is
    missing, and optionally stores your Anthropic API key.

.PARAMETER SkipKey
    Do not ask about the Anthropic API key.

.PARAMETER SkipLibreOffice
    Do not install LibreOffice.

.PARAMETER Recreate
    Delete and rebuild .venv from scratch.

.NOTES
    Everything is resolved from $PSScriptRoot, so the current directory never
    matters - this works from C:\Windows\System32 like anywhere else.
#>
[CmdletBinding()]
param(
    [switch] $SkipKey,
    [switch] $SkipLibreOffice,
    [switch] $Recreate
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$ProgressPreference = 'SilentlyContinue'

# Stage 2's wheels are pinned to what the production exe was built with
# (Python 3.13). 3.11 is the floor; anything newer than 3.13 is refused rather
# than "probably fine", because pinned wheels like onnxruntime==1.29.0 and
# pymupdf==1.27.2.3 may simply not exist for it yet.
$MinMinor      = 11
$MaxMinor      = 13
$WingetPythonId = 'Python.Python.3.13'

$RepoRoot  = $PSScriptRoot
$VenvDir   = Join-Path $RepoRoot '.venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$Requirements = Join-Path $RepoRoot 'requirements.txt'

function Write-Section { param([string] $T) Write-Host ""; Write-Host $T -ForegroundColor Cyan }
function Write-Ok      { param([string] $T) Write-Host ("  OK    " + $T) -ForegroundColor Green }
function Write-Warn    { param([string] $T) Write-Host ("  NOTE  " + $T) -ForegroundColor Yellow }
function Write-Detail  { param([string] $T) Write-Host ("        " + $T) -ForegroundColor Gray }

function Stop-WithProblem {
    param([string] $Problem, [string[]] $WhatToDo = @(), [int] $Code = 1)
    Write-Host ""
    Write-Host ("PROBLEM: " + $Problem) -ForegroundColor Red
    if ($WhatToDo.Count -gt 0) {
        Write-Host ""
        Write-Host "What to do:" -ForegroundColor Yellow
        foreach ($line in $WhatToDo) { Write-Host ("  " + $line) -ForegroundColor Yellow }
    }
    Write-Host ""
    exit $Code
}

function Update-PathFromRegistry {
    $parts = @([Environment]::GetEnvironmentVariable('Path', 'Machine'),
               [Environment]::GetEnvironmentVariable('Path', 'User'),
               $env:Path) | Where-Object { $_ }
    $env:Path = ($parts -join ';')
}

function Find-Winget {
    $cmd = Get-Command winget.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { return $cmd.Source }
    $candidate = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\winget.exe'
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    return $null
}

# =============================================================================
#  Python discovery
# =============================================================================
# The rule here is: never believe a python.exe exists until it has actually
# RUN and told us about itself.
#
# Windows ships "execution aliases" at %LOCALAPPDATA%\Microsoft\WindowsApps\
# python.exe. They are zero-byte reparse points that exist purely to open the
# Microsoft Store. Get-Command finds them, Test-Path says yes, and `python
# --version` prints "Python was not found; run without arguments to install
# from the Microsoft Store" - on stdout, with a non-zero exit code. Code that
# assumes success there prints "Found Python" and then dies on the next line
# trying to call .Split() on what is actually a string array. That is exactly
# the crash this function exists to make impossible.

function Test-PythonCandidate {
    param([Parameter(Mandatory)][string] $Exe)

    if (-not (Test-Path -LiteralPath $Exe -PathType Leaf)) { return $null }

    # A Store alias is a zero-length file. Cheap to rule out before running it,
    # and running it would pop the Microsoft Store in the developer's face.
    try {
        if ((Get-Item -LiteralPath $Exe).Length -eq 0) { return $null }
    } catch { return $null }

    # Ask Python itself, and judge it by the SHAPE of what comes back.
    #
    # The probe contains no string literals on purpose. Windows PowerShell 5.1
    # - the default shell on a colleague's PC - strips double quotes when it
    # builds a native command line, so `print("PYOK")` reaches Python as
    # `print(PYOK)` and dies with a NameError. Detection would then reject
    # every real Python on the machine. Printing three bare values avoids
    # quoting altogether and works identically in 5.1 and 7.
    #
    # Native stderr is redirected below, so relax ErrorActionPreference
    # locally or Windows PowerShell turns that into a terminating error.
    $probe = 'import sys;print(sys.version_info[0]);print(sys.version_info[1]);print(sys.executable)'
    $output = $null
    $code = $null
    try {
        $ErrorActionPreference = 'Continue'
        $output = & $Exe -c $probe 2>$null
        $code = $LASTEXITCODE
    } catch {
        return $null
    }
    if ($code -ne 0) { return $null }

    # Normalise to an array of lines whatever PowerShell handed back - a single
    # line arrives as a string, several as an object array.
    $lines = @($output | ForEach-Object { [string]$_ } | Where-Object { $_ -ne $null })
    if ($lines.Count -lt 3) { return $null }

    # Two integers followed by a path that exists is a signature nothing else
    # on the system produces by accident.
    $major = 0
    $minor = 0
    if (-not [int]::TryParse($lines[0].Trim(), [ref]$major)) { return $null }
    if (-not [int]::TryParse($lines[1].Trim(), [ref]$minor)) { return $null }
    if ($major -lt 3) { return $null }
    $realExe = $lines[2].Trim()
    if (-not (Test-Path -LiteralPath $realExe -PathType Leaf)) { return $null }

    return [pscustomobject]@{
        Path      = $realExe
        Major     = $major
        Minor     = $minor
        Version   = "$major.$minor"
        Supported = ($major -eq 3 -and $minor -ge $MinMinor -and $minor -le $MaxMinor)
    }
}

# Every place a real Python might be, deliberately including ones that are not
# on PATH - "installed but not on PATH" is a normal Windows state, not an error.
function Get-PythonCandidatePaths {
    $paths = New-Object System.Collections.Generic.List[string]

    # 1. The py launcher knows about every registered installation.
    $py = Get-Command py.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($py) {
        try {
            $ErrorActionPreference = 'Continue'
            foreach ($line in (& $py.Source -0p 2>$null)) {
                $text = ([string]$line).Trim()
                # Lines look like: " -V:3.13 *        C:\...\python.exe"
                $match = [regex]::Match($text, '([A-Za-z]:\\[^\r\n]*python\.exe)$')
                if ($match.Success) { $paths.Add($match.Groups[1].Value) }
            }
        } catch { }
    }

    # 2. Anything named python on PATH (all of them, not just the first).
    foreach ($cmd in @(Get-Command python.exe -All -ErrorAction SilentlyContinue)) {
        if ($cmd.Source) { $paths.Add($cmd.Source) }
    }

    # 3. The registry - authoritative, and needs no PATH refresh, which is what
    #    makes a freshly winget-installed Python visible in THIS process.
    foreach ($hive in @('HKLM:\SOFTWARE\Python\PythonCore',
                        'HKCU:\SOFTWARE\Python\PythonCore',
                        'HKLM:\SOFTWARE\WOW6432Node\Python\PythonCore')) {
        try {
            foreach ($key in @(Get-ChildItem -Path $hive -ErrorAction SilentlyContinue)) {
                $install = Get-ItemProperty -Path (Join-Path $key.PSPath 'InstallPath') -ErrorAction SilentlyContinue
                if ($install) {
                    $dir = if ($install.ExecutablePath) { $install.ExecutablePath }
                           elseif ($install.'(default)') { Join-Path ([string]$install.'(default)') 'python.exe' }
                           else { $null }
                    if ($dir) { $paths.Add([string]$dir) }
                }
            }
        } catch { }
    }

    # 4. Ordinary install locations, newest first.
    foreach ($base in @((Join-Path $env:LOCALAPPDATA 'Programs\Python'),
                        $env:ProgramFiles, ${env:ProgramFiles(x86)}, 'C:\')) {
        if (-not $base) { continue }
        try {
            foreach ($dir in @(Get-ChildItem -LiteralPath $base -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue)) {
                $paths.Add((Join-Path $dir.FullName 'python.exe'))
            }
        } catch { }
    }

    return @($paths | Where-Object { $_ } | Select-Object -Unique)
}

function Find-Python {
    $found = New-Object System.Collections.Generic.List[object]
    $seen  = New-Object System.Collections.Generic.HashSet[string]
    foreach ($candidate in (Get-PythonCandidatePaths)) {
        $info = Test-PythonCandidate -Exe $candidate
        if ($info -and $seen.Add($info.Path.ToLowerInvariant())) { $found.Add($info) }
    }
    # Prefer a supported version, and the newest of those.
    return @($found | Sort-Object -Property @{ Expression = 'Supported'; Descending = $true },
                                            @{ Expression = 'Minor';     Descending = $true })
}

# =============================================================================
Write-Host ""
Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host "  Stage 2 - Processing : developer setup" -ForegroundColor Cyan
Write-Host "=================================================================" -ForegroundColor Cyan
Write-Detail "(Colleagues do not run this - they use install.ps1. See the README.)"

if (-not (Test-Path -LiteralPath $Requirements)) {
    Stop-WithProblem -Code 2 -Problem "requirements.txt is not next to this script." `
        -WhatToDo @("Run setup.ps1 from inside a full checkout of the repository.")
}

# --- 1. Python ---------------------------------------------------------------
Write-Section "[1/4] Python"

$pythons = Find-Python
$usable  = @($pythons | Where-Object { $_.Supported }) | Select-Object -First 1

if ($pythons.Count -gt 0 -and -not $usable) {
    # Parenthesised deliberately: "a" + $array -join ', ' would bind as
    # ("a" + $array) -join ', ' and quietly produce nonsense.
    $versions = (($pythons | ForEach-Object { $_.Version } | Select-Object -Unique) -join ', ')
    Write-Warn ("Found Python " + $versions + " - but Stage 2 needs 3.$MinMinor to 3.$MaxMinor.")
}

if (-not $usable) {
    Write-Detail "No suitable Python found. Installing Python 3.$MaxMinor..."
    $winget = Find-Winget
    if (-not $winget) {
        Stop-WithProblem -Code 2 `
            -Problem "No suitable Python, and this PC has no 'winget' to install one with." `
            -WhatToDo @("Install Python 3.$MaxMinor from https://www.python.org/downloads/",
                        "and tick 'Add python.exe to PATH', then run setup.ps1 again.")
    }
    $ErrorActionPreference = 'Continue'
    & $winget install --id $WingetPythonId --exact --source winget --silent `
        --accept-package-agreements --accept-source-agreements | Out-Host
    $ErrorActionPreference = 'Stop'

    # Do NOT tell the developer to reopen PowerShell. Refresh PATH from the
    # registry and re-run discovery, which also searches the registry directly.
    Update-PathFromRegistry
    $usable = @(Find-Python | Where-Object { $_.Supported }) | Select-Object -First 1
    if (-not $usable) {
        Stop-WithProblem -Code 2 `
            -Problem "Python still is not usable after the install attempt." `
            -WhatToDo @("Close this window, open PowerShell again, and re-run setup.ps1.",
                        "If that fails, install Python 3.$MaxMinor from python.org by hand.")
    }
}

# Only now - after it has run and reported a supported version - is this true.
Write-Ok ("Python " + $usable.Version)
Write-Detail $usable.Path

# --- 2. Virtual environment --------------------------------------------------
Write-Section "[2/4] Virtual environment (.venv)"

if ($Recreate -and (Test-Path -LiteralPath $VenvDir)) {
    Write-Detail "Removing the existing .venv (-Recreate)..."
    Remove-Item -LiteralPath $VenvDir -Recurse -Force
}

# A venv whose python.exe does not answer is a broken leftover, not a venv.
if ((Test-Path -LiteralPath $VenvDir) -and -not (Test-PythonCandidate -Exe $VenvPython)) {
    Write-Warn "The existing .venv is broken - rebuilding it."
    Remove-Item -LiteralPath $VenvDir -Recurse -Force
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Detail "Creating .venv..."
    $ErrorActionPreference = 'Continue'
    & $usable.Path -m venv $VenvDir | Out-Host
    $code = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($code -ne 0 -or -not (Test-Path -LiteralPath $VenvPython)) {
        Stop-WithProblem -Code 3 -Problem "The virtual environment could not be created." `
            -WhatToDo @("Check you can write to: $VenvDir", "Then run setup.ps1 again.")
    }
}
$venvInfo = Test-PythonCandidate -Exe $VenvPython
if (-not $venvInfo) {
    Stop-WithProblem -Code 3 -Problem "The virtual environment's Python does not run." `
        -WhatToDo @("Run:  .\setup.ps1 -Recreate")
}
Write-Ok ("Using .venv (Python " + $venvInfo.Version + ")")

# --- 3. Dependencies ---------------------------------------------------------
Write-Section "[3/4] Dependencies"
Write-Detail "Installing into .venv - your system Python is not touched."

# Always address the interpreter by full path. Relying on whichever `python`
# happens to be on PATH is how packages end up in the wrong environment.
$ErrorActionPreference = 'Continue'
& $VenvPython -m pip install --upgrade pip --quiet | Out-Host
if ($LASTEXITCODE -ne 0) {
    $ErrorActionPreference = 'Stop'
    Stop-WithProblem -Code 3 -Problem "pip could not be upgraded." `
        -WhatToDo @("Check your internet connection, then run setup.ps1 again.")
}
& $VenvPython -m pip install -r $Requirements | Out-Host
$pipCode = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
if ($pipCode -ne 0) {
    Stop-WithProblem -Code 3 -Problem "The dependencies did not install." `
        -WhatToDo @("Check your internet connection, then run setup.ps1 again.",
                    "The full pip output is above.")
}

# Installing without error is not the same as being importable - a wheel built
# for the wrong Python fails only at import time.
$ErrorActionPreference = 'Continue'
# Single quotes only: Windows PowerShell 5.1 strips double quotes out of a
# native command line, which would turn print('...') into a NameError and make
# this check fail on every working install.
$importProbe = "import fitz, PIL, onnxruntime, openpyxl; print('IMPORTS_OK')"
$importResult = & $VenvPython -c $importProbe 2>&1
$importCode = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
if ($importCode -ne 0 -or (@($importResult | ForEach-Object { [string]$_ }) -notcontains 'IMPORTS_OK')) {
    Write-Warn "The packages installed but could not all be imported:"
    foreach ($line in $importResult) { Write-Detail ([string]$line) }
    Stop-WithProblem -Code 3 -Problem "Stage 2's dependencies are not usable." `
        -WhatToDo @("Run:  .\setup.ps1 -Recreate")
}
Write-Ok "All dependencies import cleanly."

# --- 4. LibreOffice ----------------------------------------------------------
Write-Section "[4/4] LibreOffice"

# Same discovery the installer and the app use: registry and known locations,
# never PATH alone - winget's package does not put soffice on PATH.
function Find-LibreOffice {
    $candidates = New-Object System.Collections.Generic.List[string]
    $onPath = Get-Command soffice.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($onPath) { $candidates.Add($onPath.Source) }
    foreach ($key in @('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\soffice.exe',
                       'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\soffice.exe')) {
        try {
            $entry = Get-ItemProperty -Path $key -ErrorAction SilentlyContinue
            if ($entry -and $entry.'(default)') { $candidates.Add([string]$entry.'(default)') }
        } catch { }
    }
    foreach ($key in @('HKLM:\SOFTWARE\LibreOffice\UNO\InstallPath',
                       'HKLM:\SOFTWARE\WOW6432Node\LibreOffice\UNO\InstallPath')) {
        try {
            $entry = Get-ItemProperty -Path $key -ErrorAction SilentlyContinue
            if ($entry -and $entry.'(default)') {
                $candidates.Add((Join-Path ([string]$entry.'(default)') 'soffice.exe'))
            }
        } catch { }
    }
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)},
                        (Join-Path $env:LOCALAPPDATA 'Programs'))) {
        if ($base) { $candidates.Add((Join-Path $base 'LibreOffice\program\soffice.exe')) }
    }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            if (Test-Path -LiteralPath (Join-Path (Split-Path -Parent $candidate) 'soffice.bin') -PathType Leaf) {
                return $candidate
            }
        }
    }
    return $null
}

if ($SkipLibreOffice) {
    Write-Warn "Skipped (-SkipLibreOffice). Office files will convert as text only."
} else {
    $soffice = Find-LibreOffice
    if ($soffice) {
        Write-Ok "Already installed."
        Write-Detail $soffice
    } else {
        Write-Detail "Not found. Installing it - Windows may ask for permission."
        $winget = Find-Winget
        if (-not $winget) {
            Write-Warn "No 'winget' on this PC, so LibreOffice was not installed."
            Write-Detail "Get it from https://www.libreoffice.org/download"
        } else {
            $ErrorActionPreference = 'Continue'
            & $winget install --id TheDocumentFoundation.LibreOffice --exact --source winget `
                --accept-package-agreements --accept-source-agreements | Out-Host
            $ErrorActionPreference = 'Stop'
            # Verify by finding it, never by trusting winget's exit code.
            $soffice = Find-LibreOffice
            if ($soffice) {
                Write-Ok "LibreOffice installed."
                Write-Detail $soffice
            } else {
                Write-Warn "LibreOffice did not install - Office files will convert as text only."
                Write-Detail "Retry with: winget install --id TheDocumentFoundation.LibreOffice -e"
            }
        }
    }
}

# --- Optional: the Anthropic API key -----------------------------------------
# Stored in the OS credential store under the same service/username the app
# reads, so running from source is ready on first launch. The key is never
# written to this repository, to a file, or to any log.
if (-not $SkipKey) {
    Write-Section "Anthropic API key"
    $ErrorActionPreference = 'Continue'
    $already = & $VenvPython -c "import keyring; print('yes' if keyring.get_password('DocReviewAIStation','anthropic_api_key') else 'no')" 2>$null
    $ErrorActionPreference = 'Stop'
    if (@($already | ForEach-Object { [string]$_ }) -contains 'yes') {
        Write-Ok "A key is already stored. Leaving it alone."
        Write-Detail "(Change it any time from the app: Settings > API key.)"
    } else {
        Write-Detail "Stage 2 needs an Anthropic API key to classify documents."
        Write-Detail "Ask Ali Jabbar for one - it is NEVER stored in this repository."
        Write-Detail "Press Enter with nothing typed to skip and paste it into the app later."
        $secure = Read-Host "Paste the API key (hidden)" -AsSecureString
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
        if ([string]::IsNullOrWhiteSpace($plain)) {
            Write-Warn "Skipped. Add it later via the app: Settings > API key."
        } elseif (-not $plain.StartsWith('sk-ant-')) {
            Write-Warn "That does not look like an Anthropic key (expected 'sk-ant-...')."
            Write-Detail "Nothing was saved - re-run setup.ps1 or use the app."
        } else {
            # Passed through the environment, not the command line: a command
            # line is visible to every other process on the machine.
            $env:STAGE2_KEY_IN = $plain
            $ErrorActionPreference = 'Continue'
            & $VenvPython -c "import keyring, os; keyring.set_password('DocReviewAIStation','anthropic_api_key', os.environ['STAGE2_KEY_IN'])" | Out-Null
            $stored = ($LASTEXITCODE -eq 0)
            $ErrorActionPreference = 'Stop'
            Remove-Item Env:\STAGE2_KEY_IN -ErrorAction SilentlyContinue
            $plain = $null
            if ($stored) { Write-Ok "Key stored in the Windows Credential Manager." }
            else { Write-Warn "Could not store the key. Paste it into the app: Settings > API key." }
        }
    }
}

Write-Host ""
Write-Host "=================================================================" -ForegroundColor Green
Write-Host "  Developer setup complete" -ForegroundColor Green
Write-Host "=================================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Run the app from source:" -ForegroundColor Green
Write-Host ("  " + $VenvPython + ' "' + (Join-Path $RepoRoot 'src\Stage2_Processing.pyw') + '"')
Write-Host ""
Write-Host "Or activate the environment first:" -ForegroundColor Green
Write-Host ("  " + (Join-Path $VenvDir 'Scripts\Activate.ps1'))
Write-Host ""
exit 0
