# ============================================================================
#  Stage 2 - Processing : one-command exe rebuild
#  Run from anywhere:   .\build\build.ps1
#  Output:              <repo>\dist\Stage 2 - Processing.exe
#
#  Prerequisite: .\setup.ps1 has been run once (installs pymupdf/pillow/
#  onnxruntime/openpyxl/keyring).
#
#  This builds the APP ONLY. For the credential-free public end-user installer,
#  run build\build_public_installer.ps1 after building the current user guide.
#  The separate private build\installer\build_installer.py embeds an API key;
#  never use its output for a public release or publish it to GitHub.
# ============================================================================
$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent

Write-Host "=== Building Stage 2 - Processing ===" -ForegroundColor Cyan

# ---------------------------------------------------------------------------
#  Find a real Python, rather than assuming `python` is on PATH.
#
#  It frequently is not: a per-user Python install does not always add itself,
#  and Windows ships an execution alias at %LOCALAPPDATA%\Microsoft\WindowsApps
#  \python.exe that is a zero-byte stub whose only job is to open the Microsoft
#  Store. Calling bare `python` therefore either fails outright or runs the
#  stub, and the build dies with an unhelpful error before it starts.
#
#  The probe below contains no string literals on purpose: Windows PowerShell
#  5.1 strips double quotes when it builds a native command line, so a probe
#  containing print("X") reaches Python as print(X) and raises NameError.
# ---------------------------------------------------------------------------
function Test-Python {
    param([string] $Exe)
    if (-not $Exe) { return $false }
    if (-not (Test-Path -LiteralPath $Exe -PathType Leaf)) { return $false }
    try { if ((Get-Item -LiteralPath $Exe).Length -eq 0) { return $false } } catch { return $false }
    $ErrorActionPreference = "Continue"   # native stderr must not be fatal here
    $out = & $Exe -c 'import sys;print(sys.version_info[0]);print(sys.version_info[1])' 2>$null
    if ($LASTEXITCODE -ne 0) { return $false }
    $lines = @($out | ForEach-Object { [string]$_ })
    return ($lines.Count -ge 2 -and $lines[0].Trim() -eq "3")
}

function Find-BuildPython {
    param([string] $RepoRoot)
    # The project venv first - setup.ps1 puts the pinned dependencies there.
    $venv = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (Test-Python $venv) { return $venv }
    foreach ($candidate in @(Get-Command python.exe -All -ErrorAction SilentlyContinue |
                             ForEach-Object { $_.Source })) {
        if (Test-Python $candidate) { return $candidate }
    }
    $base = Join-Path $env:LOCALAPPDATA "Programs\Python"
    if (Test-Path -LiteralPath $base) {
        foreach ($dir in @(Get-ChildItem -LiteralPath $base -Directory -Filter "Python3*" -ErrorAction SilentlyContinue |
                           Sort-Object Name -Descending)) {
            $candidate = Join-Path $dir.FullName "python.exe"
            if (Test-Python $candidate) { return $candidate }
        }
    }
    return $null
}

$python = Find-BuildPython -RepoRoot $repo
if (-not $python) {
    Write-Host "No usable Python found." -ForegroundColor Red
    Write-Host "Run .\setup.ps1 first - it installs Python if needed and creates .venv."
    exit 1
}
Write-Host "Python: $python" -ForegroundColor Gray

# The interpreter must also be the one carrying the app's dependencies, or
# PyInstaller analyses an incomplete environment and produces an exe that only
# fails at run time. Single quotes: see the quoting note above.
$ErrorActionPreference = "Continue"
& $python -c "import fitz, PIL, onnxruntime, openpyxl; print('DEPS_OK')" | Out-Null
$depsOk = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = "Stop"
if (-not $depsOk) {
    Write-Host "That Python is missing Stage 2's dependencies." -ForegroundColor Red
    Write-Host "Run .\setup.ps1 first, then build again."
    exit 1
}

# Pinned PyInstaller. 6.22.1 includes the current one-file environment
# validation fix and is the minimum permitted release for distributable builds.
& $python -m pip install "pyinstaller==6.22.1" --quiet
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# Compile check before the (slow) freeze
& $python -m py_compile (Join-Path $repo "src\Stage2_Processing.pyw")
if ($LASTEXITCODE -ne 0) {
    Write-Host "Source failed to compile - fix the error above first." -ForegroundColor Red
    exit 1
}

# Work dir goes to pybuild/ (git-ignored) because build/ holds tracked files.
& $python -m PyInstaller --noconfirm `
    --distpath (Join-Path $repo "dist") `
    --workpath (Join-Path $repo "pybuild") `
    (Join-Path $PSScriptRoot "stage2.spec")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$exe = Join-Path $repo "dist\Stage 2 - Processing.exe"
if (Test-Path $exe) {
    $mb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    & (Join-Path $repo "tools\smoke_test_frozen_app.ps1") -ExePath $exe
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host "`nDONE: $exe ($mb MB)" -ForegroundColor Green
    Write-Host "Deploy by copying it over the existing exe (back the old one up first)."
} else {
    Write-Host "`nBuild finished but the exe was not found in dist\ - check output above." -ForegroundColor Red
    exit 1
}
