# ============================================================================
#  Stage 2 - Processing : one-command developer setup
#  Installs the Python dependencies and helps you store the Anthropic API key.
#  Run from the repo root:   .\setup.ps1
#
#  Optional switch:  .\setup.ps1 -SkipKey    (dependencies only)
# ============================================================================
param([switch]$SkipKey)

$ErrorActionPreference = "Stop"

Write-Host "=== Stage 2 setup ===" -ForegroundColor Cyan

# 1. Check Python is available and is 3.11+
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Host "Python was not found on PATH." -ForegroundColor Red
    Write-Host "Install Python 3.13 from https://www.python.org/downloads/ and tick"
    Write-Host "'Add python.exe to PATH' during install, then re-run this script."
    exit 1
}
$ver = (python --version) -replace "Python ", ""
Write-Host "Found Python $ver"
$parts = $ver.Split(".")
if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 11)) {
    Write-Host "Python 3.11 or newer is required (3.13 recommended). Found $ver." -ForegroundColor Red
    exit 1
}

# 2. Install pinned Python dependencies
Write-Host "`n[1/3] Installing Python packages (pymupdf, pillow, openpyxl, keyring)..." -ForegroundColor Cyan
python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python -m pip install -r (Join-Path $PSScriptRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# 3. LibreOffice is needed to convert Office files (.docx/.xlsx/.pptx) to PDF.
#    Stage 2 works without it, but those files convert as text-only.
Write-Host "`n[2/3] Checking for LibreOffice (used to convert Office files to PDF)..." -ForegroundColor Cyan
$soffice = @(
    "$env:ProgramFiles\LibreOffice\program\soffice.exe",
    "${env:ProgramFiles(x86)}\LibreOffice\program\soffice.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($soffice) {
    Write-Host "Found LibreOffice: $soffice" -ForegroundColor Green
} else {
    Write-Host "LibreOffice not found (OPTIONAL)." -ForegroundColor Yellow
    Write-Host "Word/Excel/PowerPoint files will convert as TEXT-ONLY until you install it."
    Write-Host "Get it from https://www.libreoffice.org/download - Stage 2 picks it up"
    Write-Host "automatically afterwards, no reconfiguration needed."
}

# 4. Store the Anthropic API key in the OS credential store.
#    Same service/username Stage 2 reads (see get_api_key() in the source), so
#    doing it here means the app is ready on first launch.
if ($SkipKey) {
    Write-Host "`n[3/3] Skipping API key setup (-SkipKey)." -ForegroundColor Yellow
} else {
    Write-Host "`n[3/3] Anthropic API key" -ForegroundColor Cyan
    $already = python -c "import keyring; print('yes' if keyring.get_password('DocReviewAIStation','anthropic_api_key') else 'no')" 2>$null
    if ($already -eq "yes") {
        Write-Host "A key is already stored in the OS credential store. Leaving it alone." -ForegroundColor Green
        Write-Host "(Change it any time from the app: Settings > API key.)"
    } else {
        Write-Host "Stage 2 needs an Anthropic API key to classify documents."
        Write-Host "Ask Ali Jabbar for one - it is NEVER stored in this repository." -ForegroundColor Yellow
        Write-Host "Press Enter with nothing typed to skip and paste it into the app later."
        $sec = Read-Host "Paste the API key (hidden)" -AsSecureString
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
        if ([string]::IsNullOrWhiteSpace($plain)) {
            Write-Host "Skipped. Add it later via the app: Settings > API key." -ForegroundColor Yellow
        } elseif (-not $plain.StartsWith("sk-ant-")) {
            Write-Host "That does not look like an Anthropic key (expected it to start" -ForegroundColor Red
            Write-Host "with 'sk-ant-'). Nothing was saved - re-run setup or use the app." -ForegroundColor Red
        } else {
            $env:STAGE2_KEY_IN = $plain
            python -c "import keyring, os; keyring.set_password('DocReviewAIStation','anthropic_api_key', os.environ['STAGE2_KEY_IN']); print('stored')"
            $ok = ($LASTEXITCODE -eq 0)
            Remove-Item Env:\STAGE2_KEY_IN -ErrorAction SilentlyContinue
            $plain = $null
            if ($ok) {
                Write-Host "Key stored in the Windows Credential Manager." -ForegroundColor Green
            } else {
                Write-Host "Could not store the key. Paste it into the app instead:" -ForegroundColor Red
                Write-Host "Settings > API key." -ForegroundColor Red
            }
        }
    }
}

Write-Host "`nDone. Run the app from source with:" -ForegroundColor Green
Write-Host "    python src\Stage2_Processing.pyw"
Write-Host "(or pythonw to run without a console window)"
