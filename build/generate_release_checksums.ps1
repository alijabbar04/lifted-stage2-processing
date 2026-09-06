param(
    [string]$OutputPath
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputPath) {
    $OutputPath = Join-Path $RepoRoot "dist\SHA256SUMS.txt"
}

$Artifacts = [ordered]@{
    "Stage2_Processing.exe" = Join-Path $RepoRoot "dist\Stage 2 - Processing.exe"
    "Stage2_Processing_Setup.exe" = Join-Path $PSScriptRoot "installer\Output\Stage2_Processing_Setup.exe"
    "Stage2_Guide_AI_Processing.pdf" = Join-Path $RepoRoot "docs\USER_GUIDE.pdf"
    "inference.onnx" = Join-Path $RepoRoot "assets\orientation\inference.onnx"
}

$Lines = foreach ($Artifact in $Artifacts.GetEnumerator()) {
    if (-not (Test-Path -LiteralPath $Artifact.Value -PathType Leaf)) {
        throw "Release checksum input is missing: $($Artifact.Value)"
    }
    $Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Artifact.Value).Hash.ToLowerInvariant()
    "$Hash *$($Artifact.Key)"
}

$OutputPath = [IO.Path]::GetFullPath($OutputPath)
$OutputDir = Split-Path -Parent $OutputPath
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$TempPath = Join-Path $OutputDir ("." + [IO.Path]::GetFileName($OutputPath) + ".tmp")
$Utf8NoBom = New-Object Text.UTF8Encoding($false)
[IO.File]::WriteAllText($TempPath, (($Lines -join "`n") + "`n"), $Utf8NoBom)
Move-Item -LiteralPath $TempPath -Destination $OutputPath -Force

Write-Host "Checksums: $OutputPath" -ForegroundColor Green
