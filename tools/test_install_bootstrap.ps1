# Isolated bootstrap guide-fix check
#
# This test extracts only Get-VerifiedReleaseAssetHash from this checkout's
# install.ps1 (or an explicitly supplied staged script). It never dot-sources
# or invokes install.ps1, and performs no GitHub, installer, shortcut,
# credential, or live-repository operation.

param(
    [string]$InstallScriptPath = (Join-Path (Split-Path -Parent $PSScriptRoot) "install.ps1")
)

$ErrorActionPreference = "Stop"
$sourcePath = if ($InstallScriptPath) {
    (Resolve-Path -LiteralPath $InstallScriptPath).Path
} else {
    Join-Path $PSScriptRoot "install.ps1.patch"
}
$sourceText = [IO.File]::ReadAllText($sourcePath)
if ($InstallScriptPath) {
    $tokens = $null
    $parseErrors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile(
        $sourcePath, [ref]$tokens, [ref]$parseErrors)
    if ($parseErrors.Count) { throw "Staged install script did not parse." }
    $functions = @($ast.Find({ param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq "Get-VerifiedReleaseAssetHash" }, $true))
    if ($functions.Count -ne 1) { throw "Staged install script has no unique helper." }
    $helperText = $functions[0].Extent.Text
} else {
    $patchLines = @(Get-Content -LiteralPath $sourcePath)
    $start = [Array]::IndexOf($patchLines, "+function Get-VerifiedReleaseAssetHash {")
    if ($start -lt 0) { throw "Inactive patch does not contain the helper." }
    $end = -1
    for ($i = $start; $i -lt $patchLines.Count; $i++) {
        if ($patchLines[$i] -eq "+}") { $end = $i; break }
    }
    if ($end -lt 0) { throw "Could not find the helper's closing brace." }
    $helperText = (($patchLines[$start..$end] |
        ForEach-Object { $_.Substring(1) }) -join [Environment]::NewLine)
}
. ([scriptblock]::Create($helperText))

$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
$fixture = Join-Path $tempRoot ("Stage2GuideValidation-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $fixture | Out-Null
try {
    $guide = Join-Path $fixture "Stage2_Guide_AI_Processing.pdf"
    $manifest = Join-Path $fixture "SHA256SUMS.txt"
    [IO.File]::WriteAllText($guide, "synthetic guide bytes")
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $guide).Hash
    $validLine = "$hash *Stage2_Guide_AI_Processing.pdf"

    # Good guide and unique manifest entry.
    [IO.File]::WriteAllText($manifest, $validLine + [Environment]::NewLine)
    $actual = Get-VerifiedReleaseAssetHash -ManifestPath $manifest -AssetPath $guide -AssetName "Stage2_Guide_AI_Processing.pdf"
    if ($actual -ine $hash) { throw "Good case returned the wrong hash." }

    function Expect-Failure {
        param([string]$Name, [scriptblock]$Action, [string]$Pattern)
        $failed = $false
        $message = ""
        try { & $Action } catch {
            $failed = $true
            $message = $_.Exception.Message
        }
        if (-not $failed) { throw "$Name unexpectedly passed." }
        if ($message -notmatch $Pattern) {
            throw "$Name had the wrong error: $message"
        }
    }

    # Missing downloaded guide file.
    Remove-Item -LiteralPath $guide -Force
    Expect-Failure "missing guide" {
        Get-VerifiedReleaseAssetHash -ManifestPath $manifest -AssetPath $guide -AssetName "Stage2_Guide_AI_Processing.pdf"
    } "asset is missing"
    [IO.File]::WriteAllText($guide, "synthetic guide bytes")

    # Missing guide entry in an otherwise readable manifest.
    [IO.File]::WriteAllText($manifest, ("0" * 64) + " *Stage2_Processing.exe" + [Environment]::NewLine)
    Expect-Failure "missing manifest entry" {
        Get-VerifiedReleaseAssetHash -ManifestPath $manifest -AssetPath $guide -AssetName "Stage2_Guide_AI_Processing.pdf"
    } "exactly one entry.*found 0"

    # Duplicate guide entries are rejected rather than first-match accepted.
    [IO.File]::WriteAllText($manifest, ($validLine + [Environment]::NewLine + $validLine + [Environment]::NewLine))
    Expect-Failure "duplicate manifest entry" {
        Get-VerifiedReleaseAssetHash -ManifestPath $manifest -AssetPath $guide -AssetName "Stage2_Guide_AI_Processing.pdf"
    } "exactly one entry.*found 2"

    # A changed guide fails the recorded hash.
    [IO.File]::WriteAllText($guide, "changed synthetic guide bytes")
    [IO.File]::WriteAllText($manifest, $validLine + [Environment]::NewLine)
    Expect-Failure "mismatched guide" {
        Get-VerifiedReleaseAssetHash -ManifestPath $manifest -AssetPath $guide -AssetName "Stage2_Guide_AI_Processing.pdf"
    } "Checksum mismatch"

    # Static assertions for the guarded guide path and copy source.
    $guideValidation = $sourceText.IndexOf('-AssetPath $DownloadedGuide')
    $installSection = $sourceText.IndexOf('# --- 4. Install into the user profile')
    $guideCopy = $sourceText.IndexOf('Copy-Item -LiteralPath $DownloadedGuide')
    if ($guideValidation -lt 0 -or $installSection -lt 0 -or
        $guideValidation -gt $installSection -or $guideCopy -lt $installSection) {
        throw "Guide validation/copy is not guarded before install completion."
    }
    if ($sourceText -match '(?m)^\+?\s*if\s*\(\$guideOk\)') {
        throw "Old optional guide-copy branch remains in the patch."
    }
    if ($sourceText -notmatch 'Copy-Item -LiteralPath \$DownloadedGuide') {
        throw "Guide copy source is missing."
    }
    if ($sourceText -notmatch "Guide download failed.*nothing was installed") {
        throw "Missing-guide download path lacks the no-install message."
    }

    Write-Output "PASS: good, missing, missing-entry, duplicate, mismatch, and guarded-path cases."
}
finally {
    $resolvedFixture = [IO.Path]::GetFullPath($fixture)
    if ($resolvedFixture.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -and
        [IO.Path]::GetFileName($resolvedFixture) -match '^Stage2GuideValidation-[0-9a-f]{32}$' -and
        (Test-Path -LiteralPath $resolvedFixture)) {
        Remove-Item -LiteralPath $resolvedFixture -Recurse -Force
    } elseif (Test-Path -LiteralPath $resolvedFixture) {
        throw "Refusing to remove an unexpected synthetic fixture path: $resolvedFixture"
    }
}
