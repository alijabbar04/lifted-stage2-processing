$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Script = Join-Path $PSScriptRoot "installer\Stage2_Public_Installer.iss"
$App = Join-Path $RepoRoot "dist\Stage 2 - Processing.exe"

if (-not (Test-Path $App)) {
    throw "Build the app first with .\build\build.ps1; missing: $App"
}

$Candidates = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
)
$Iscc = $Candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Iscc) {
    throw "Inno Setup 6 is required. Install it with: winget install --id JRSoftware.InnoSetup -e"
}

& $Iscc $Script
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$Output = Join-Path $PSScriptRoot "installer\Output\Stage2_Processing_Setup.exe"
if (-not (Test-Path $Output)) { throw "Installer was not produced: $Output" }
Write-Host "Built: $Output" -ForegroundColor Green
