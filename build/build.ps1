# ============================================================================
#  Stage 2 - Processing : one-command exe rebuild
#  Run from anywhere:   .\build\build.ps1
#  Output:              <repo>\dist\Stage 2 - Processing.exe
#
#  Prerequisite: .\setup.ps1 has been run once (installs pymupdf/pillow/
#  onnxruntime/openpyxl/keyring).
#
#  This builds the APP ONLY. To build the end-user installer (app + API key +
#  LibreOffice + guides/tools payload) see build\installer\build_installer.py -
#  that one needs Inno Setup and an API key, and its output must NOT be
#  published to GitHub because the key is compiled into it.
# ============================================================================
$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent

Write-Host "=== Building Stage 2 - Processing ===" -ForegroundColor Cyan

# Pinned PyInstaller. 6.22.1 includes the current one-file environment
# validation fix and is the minimum permitted release for distributable builds.
python -m pip install "pyinstaller==6.22.1" --quiet
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# Compile check before the (slow) freeze
python -m py_compile (Join-Path $repo "src\Stage2_Processing.pyw")
if ($LASTEXITCODE -ne 0) {
    Write-Host "Source failed to compile - fix the error above first." -ForegroundColor Red
    exit 1
}

# Work dir goes to pybuild/ (git-ignored) because build/ holds tracked files.
python -m PyInstaller --noconfirm `
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
