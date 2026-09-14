#Requires -Version 5.1
<#
.SYNOPSIS
    Tests the installer and setup helper functions - the real shipped ones.

.DESCRIPTION
    install.ps1 and setup.ps1 both run top-to-bottom, so they cannot simply be
    dot-sourced in a test. Instead this parses each script, lifts the named
    function definitions straight out of its AST, and defines them here. The
    code under test is therefore byte-for-byte the code that ships - not a copy
    that can drift.

    Nothing here installs, downloads or deletes anything outside a temporary
    folder it creates itself.

    Run:  powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\test_installer_functions.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

$RepoRoot = Split-Path $PSScriptRoot -Parent
$script:Passed = 0
$script:Failed = 0

function Test-Case {
    param([string] $Name, [scriptblock] $Body)
    try {
        $result = & $Body
        if ($result) {
            $script:Passed++
            Write-Host ("  PASS  " + $Name) -ForegroundColor Green
        } else {
            $script:Failed++
            Write-Host ("  FAIL  " + $Name) -ForegroundColor Red
        }
    } catch {
        $script:Failed++
        Write-Host ("  FAIL  " + $Name) -ForegroundColor Red
        Write-Host ("        " + $_.Exception.Message) -ForegroundColor Red
    }
}

# Lift named functions out of a script's AST and define them in this session.
function Import-FunctionsFrom {
    param([string] $ScriptPath, [string[]] $Names)
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($ScriptPath, [ref]$null, [ref]$errors)
    if ($errors.Count) { throw "$ScriptPath has syntax errors: $($errors[0].Message)" }
    $definitions = $ast.FindAll({
        param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
    }, $true)
    foreach ($name in $Names) {
        $definition = $definitions | Where-Object { $_.Name -eq $name } | Select-Object -First 1
        if (-not $definition) { throw "$ScriptPath does not define $name" }
        # Define it in the global scope so Test-Case scriptblocks can see it.
        Set-Item -Path ("function:global:" + $name) -Value $definition.Body.GetScriptBlock()
    }
}

Write-Host ""
Write-Host "Stage 2 installer/setup function tests" -ForegroundColor Cyan

$installPs1 = Join-Path $RepoRoot 'install.ps1'
$setupPs1   = Join-Path $RepoRoot 'setup.ps1'

Import-FunctionsFrom -ScriptPath $installPs1 -Names @(
    'Assert-Checksum', 'Find-LibreOffice', 'Find-Winget', 'Test-LooksLikeNetworkFailure',
    'Test-Stage2Source', 'Install-Stage2Source', 'Set-WorkflowPaths', 'Find-RealPython',
    'Invoke-Download')
Import-FunctionsFrom -ScriptPath $setupPs1 -Names @(
    'Test-PythonCandidate', 'Get-PythonCandidatePaths')

# Test-PythonCandidate reads these from its script scope.
$global:MinMinor = 11
$global:MaxMinor = 13

# Write-Log is called by install.ps1's helpers; stub it out.
Set-Item -Path 'function:global:Write-Log' -Value { param($Text, $Level) }

$sandbox = Join-Path $env:TEMP ('Stage2Tests-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $sandbox -Force | Out-Null

try {
    # =========================================================================
    Write-Host ""
    Write-Host "Python detection (the Microsoft Store alias bug)" -ForegroundColor Cyan

    # THE regression test. A Store execution alias is a zero-byte reparse point
    # that Get-Command finds and Test-Path confirms, but that is not Python.
    # The old setup.ps1 printed "Found Python" for it and then crashed calling
    # .Split() on its output. Detection must reject it.
    $storeAlias = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\python.exe'
    if (Test-Path -LiteralPath $storeAlias) {
        Test-Case "a Microsoft Store python alias is rejected" {
            (Test-PythonCandidate -Exe $storeAlias) -eq $null
        }
    } else {
        # Simulate one, so this test means something on a PC without the alias.
        $fakeAlias = Join-Path $sandbox 'python.exe'
        New-Item -ItemType File -Path $fakeAlias -Force | Out-Null
        Test-Case "a zero-byte python.exe is rejected (simulated Store alias)" {
            (Test-PythonCandidate -Exe $fakeAlias) -eq $null
        }
    }

    Test-Case "a path that does not exist is rejected" {
        (Test-PythonCandidate -Exe (Join-Path $sandbox 'no-such-python.exe')) -eq $null
    }

    # An executable that runs but is not Python must not be mistaken for one.
    # The probe's shape is what proves it: two integers followed by a path that
    # exists is not something another program emits by accident.
    Test-Case "a non-Python executable is rejected" {
        (Test-PythonCandidate -Exe (Join-Path $env:SystemRoot 'System32\cmd.exe')) -eq $null
    }

    # Windows PowerShell 5.1 strips double quotes when it builds a native
    # command line, so a probe containing print("X") reaches Python as
    # print(X) and raises NameError. That silently made detection reject every
    # real interpreter. The shipped probe must therefore contain no double
    # quote at all - assert that on the source text itself, because the bug is
    # invisible when these tests run under PowerShell 7.
    Test-Case "the Python probe contains no double quotes (PowerShell 5.1 mangles them)" {
        $source = Get-Content -LiteralPath $setupPs1 -Raw
        $match = [regex]::Match($source, '\$probe\s*=\s*''([^'']*)''')
        $match.Success -and ($match.Groups[1].Value -notmatch '"') -and
            ($match.Groups[1].Value -match 'version_info')
    }

    $real = @(Get-PythonCandidatePaths | ForEach-Object { Test-PythonCandidate -Exe $_ } |
              Where-Object { $_ }) | Select-Object -First 1
    if ($real) {
        Test-Case "a real Python is accepted and reports a usable version" {
            $real.Major -eq 3 -and $real.Minor -gt 0 -and (Test-Path -LiteralPath $real.Path)
        }
        Test-Case "the reported path is a real interpreter, not the alias" {
            (Get-Item -LiteralPath $real.Path).Length -gt 0
        }
        Write-Host ("        found Python " + $real.Version + " at " + $real.Path) -ForegroundColor DarkGray
    } else {
        Write-Host "  SKIP  no real Python on this PC to accept" -ForegroundColor Yellow
    }

    # =========================================================================
    Write-Host ""
    Write-Host "Checksum verification" -ForegroundColor Cyan

    $payload = Join-Path $sandbox 'Stage2_Processing.exe'
    Set-Content -LiteralPath $payload -Value 'pretend binary' -NoNewline
    $trueHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $payload).Hash.ToLowerInvariant()
    $manifest = Join-Path $sandbox 'SHA256SUMS.txt'

    Set-Content -LiteralPath $manifest -Value "$trueHash *Stage2_Processing.exe"
    Test-Case "a matching checksum is accepted" {
        (Assert-Checksum -ManifestPath $manifest -FilePath $payload -AssetName 'Stage2_Processing.exe') -ne $null
    }

    # The guard has to be able to FAIL on the thing it claims to catch.
    Set-Content -LiteralPath $manifest -Value ("0" * 64 + " *Stage2_Processing.exe")
    Test-Case "a WRONG checksum is rejected" {
        try { Assert-Checksum -ManifestPath $manifest -FilePath $payload -AssetName 'Stage2_Processing.exe'; $false }
        catch { $_.Exception.Message -match 'does not match' }
    }

    Set-Content -LiteralPath $manifest -Value "$trueHash *SomethingElse.exe"
    Test-Case "an asset missing from the manifest is rejected" {
        try { Assert-Checksum -ManifestPath $manifest -FilePath $payload -AssetName 'Stage2_Processing.exe'; $false }
        catch { $_.Exception.Message -match 'exactly once' }
    }

    # Two entries for one name is ambiguous - taking the first would let an
    # attacker who can append to the manifest choose the hash.
    Set-Content -LiteralPath $manifest -Value @("$trueHash *Stage2_Processing.exe",
                                                ("0" * 64 + " *Stage2_Processing.exe"))
    Test-Case "a duplicated manifest entry is rejected" {
        try { Assert-Checksum -ManifestPath $manifest -FilePath $payload -AssetName 'Stage2_Processing.exe'; $false }
        catch { $_.Exception.Message -match 'exactly once' }
    }

    Test-Case "a missing manifest is rejected" {
        try { Assert-Checksum -ManifestPath (Join-Path $sandbox 'nope.txt') -FilePath $payload -AssetName 'x.exe'; $false }
        catch { $_.Exception.Message -match 'missing' }
    }

    # =========================================================================
    Write-Host ""
    Write-Host "Error classification" -ForegroundColor Cyan

    Test-Case "a DNS failure reads as a network problem" {
        Test-LooksLikeNetworkFailure 'The remote name could not be resolved: api.github.com'
    }
    Test-Case "a TLS failure reads as a network problem" {
        Test-LooksLikeNetworkFailure 'Could not create SSL/TLS secure channel.'
    }
    Test-Case "a 404 does NOT read as a network problem" {
        -not (Test-LooksLikeNetworkFailure 'The remote server returned an error: (404) Not Found.')
    }
    Test-Case "empty text does NOT read as a network problem" {
        -not (Test-LooksLikeNetworkFailure '')
    }

    # =========================================================================
    Write-Host ""
    Write-Host "Dependency discovery" -ForegroundColor Cyan

    Test-Case "Find-LibreOffice returns either null or a real soffice.exe" {
        $found = Find-LibreOffice
        ($null -eq $found) -or ((Test-Path -LiteralPath $found -PathType Leaf) -and ($found -match 'soffice\.exe$'))
    }
    Test-Case "Find-Winget returns either null or a real winget.exe" {
        $found = Find-Winget
        ($null -eq $found) -or (Test-Path -LiteralPath $found -PathType Leaf)
    }
    Test-Case "Find-LibreOffice does not depend on the current directory" {
        $before = Find-LibreOffice
        Push-Location $env:SystemRoot
        try { $after = Find-LibreOffice } finally { Pop-Location }
        $before -eq $after
    }

    # =========================================================================
    Write-Host ""
    Write-Host "Per-user data folders" -ForegroundColor Cyan

    $installSource = Get-Content -LiteralPath $installPs1 -Raw

    # The folder names are a contract with the application: Stage 2 reads and
    # writes these exact paths (stage2_run_state.py, stage2_diagnostics.py,
    # stage2_ai_workflows.default_workspace_root). Inventing a folder here
    # would create an empty directory the app never looks at.
    foreach ($expected in 'Lifted\Guides', 'Lifted\Logs', 'Lifted\Stage2\runs',
                          'Lifted\Stage2\ai-workflows', 'Lifted\Stage2Diagnostics') {
        Test-Case "the installer provisions $expected" {
            $installSource -match ([regex]::Escape("'" + $expected + "'"))
        }
    }

    # The ledger workbook must NOT be pre-created. ai_review.sync_records
    # creates it - with the Review Log, Run Log and Instructions sheets it
    # needs - the first time a review is recorded. An empty .xlsx planted by
    # the installer adds nothing and can sit locked by Excel.
    Test-Case "the installer does NOT create the review ledger workbook" {
        $installSource -notmatch 'New-Item[^\r\n]*Master_Filename_Review_Ledger'
    }
    # C:\Lifted is a machine-wide legacy location that may need admin rights.
    # A new PC must use the per-user path instead.
    Test-Case "the installer never creates anything under C:\Lifted" {
        $installSource -notmatch 'New-Item[^\r\n]*C:\\Lifted'
    }

    # -CheckOnly writes nothing, so it has to keep working while the app is
    # open - that is precisely when someone is diagnosing a problem.
    Test-Case "-CheckOnly is not blocked by the app being open" {
        $installSource -match '\$appRunning -and -not \$CheckOnly'
    }

    # =========================================================================
    Write-Host ""
    Write-Host "Post-run AI review setup (-WithAuditReview)" -ForegroundColor Cyan

    # These run against a sandbox: the script-scope paths the functions read
    # are redefined here, so nothing touches the real config or install.
    $global:Repo         = 'alijabbar04/lifted-stage2-processing'
    $global:WorkDir      = Join-Path $sandbox 'work'
    $global:SourceRoot   = Join-Path $sandbox 'source'
    $global:WorkspaceDir = Join-Path $sandbox 'ai-workflows'
    $global:ConfigPath   = Join-Path $sandbox 'config.json'
    New-Item -ItemType Directory -Path $global:WorkDir -Force | Out-Null

    Test-Case "a folder without the review sources is rejected" {
        -not (Test-Stage2Source $sandbox)
    }
    Test-Case "the repo checkout itself is accepted" {
        Test-Stage2Source $RepoRoot
    }
    # prepare_workflow() needs these two files and, for the audit-review role,
    # explicitly does NOT need a .git directory.
    Test-Case "acceptance needs exactly ai_review.py and Stage2_Processing.pyw" {
        $partial = Join-Path $sandbox 'partial'
        New-Item -ItemType Directory -Path (Join-Path $partial 'src') -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $partial 'src\ai_review.py') -Value '#'
        $before = Test-Stage2Source $partial          # one file only -> no
        Set-Content -LiteralPath (Join-Path $partial 'src\Stage2_Processing.pyw') -Value '#'
        $after = Test-Stage2Source $partial           # both files -> yes, no .git
        (-not $before) -and $after
    }

    Test-Case "the Microsoft Store python stub is not offered as a runtime" {
        $stub = Join-Path $sandbox 'stub\python.exe'
        New-Item -ItemType File -Path $stub -Force | Out-Null   # zero bytes
        $found = Find-RealPython
        # Either a real interpreter, or nothing - never the zero-byte stub.
        ($null -eq $found) -or ((Get-Item -LiteralPath $found).Length -gt 0)
    }

    # --- config.json merging -------------------------------------------------
    # config.json also holds the user's model, care-home and account choices.
    # Losing those would be a far worse outcome than not filling in a path.
    $existing = @{
        api_model = 'test-model'
        fx_rate   = 1.23
        ai_workflows = @{ care_home = 'Test Home'; 'audit-review_model' = 'sol' }
    } | ConvertTo-Json -Depth 8
    Set-Content -LiteralPath $global:ConfigPath -Value $existing -Encoding UTF8

    Test-Case "writing the paths keeps every other setting" {
        $null = Set-WorkflowPaths -Source 'C:\some\source'
        $after = Get-Content -LiteralPath $global:ConfigPath -Raw | ConvertFrom-Json
        ($after.api_model -eq 'test-model') -and ($after.fx_rate -eq 1.23) -and
        ($after.ai_workflows.care_home -eq 'Test Home') -and
        ($after.ai_workflows.'audit-review_model' -eq 'sol')
    }
    Test-Case "the paths are actually written" {
        $after = Get-Content -LiteralPath $global:ConfigPath -Raw | ConvertFrom-Json
        ($after.ai_workflows.source_root -eq 'C:\some\source') -and
        ($after.ai_workflows.workspace_root -eq $global:WorkspaceDir)
    }
    Test-Case "an existing source_root is never overwritten" {
        # Someone pointing Stage 2 at their own checkout keeps it.
        $null = Set-WorkflowPaths -Source 'C:\a\different\source'
        $after = Get-Content -LiteralPath $global:ConfigPath -Raw | ConvertFrom-Json
        $after.ai_workflows.source_root -eq 'C:\some\source'
    }
    Test-Case "no config file yet is handled" {
        Remove-Item -LiteralPath $global:ConfigPath -Force
        $ok = Set-WorkflowPaths -Source 'C:\fresh\source'
        $after = Get-Content -LiteralPath $global:ConfigPath -Raw | ConvertFrom-Json
        $ok -and ($after.ai_workflows.source_root -eq 'C:\fresh\source')
    }

    # --- the real download ---------------------------------------------------
    # Network-dependent, so it is skipped rather than failed when offline.
    $latestTag = $null
    try {
        $latestTag = (Invoke-RestMethod -Uri "https://api.github.com/repos/$global:Repo/releases/latest" `
                        -UseBasicParsing -TimeoutSec 30 `
                        -Headers @{ 'User-Agent' = 'Lifted-Stage2-Tests' }).tag_name
    } catch { }
    if ($latestTag) {
        Test-Case "the release source really downloads and contains the review code" {
            $placed = Install-Stage2Source -ReleaseTag $latestTag
            $placed -and (Test-Stage2Source $placed)
        }
        Write-Host ("        source tag " + $latestTag) -ForegroundColor DarkGray
    } else {
        Write-Host "  SKIP  no network for the source download test" -ForegroundColor Yellow
    }

    # =========================================================================
    Write-Host ""
    Write-Host "The command we send colleagues" -ForegroundColor Cyan

    # The one-line command in the README is the only thing most people ever
    # touch, and it cannot be exercised by running it here - so check the
    # properties that make it work when pasted into a strange PC.
    $readme = Get-Content -LiteralPath (Join-Path $RepoRoot 'README.md') -Raw
    $marked = [regex]::Match($readme,
        '<!--\s*install-command\s*-->\s*```powershell\r?\n(?<cmd>.*?)\r?\n```', 'Singleline')

    Test-Case "README carries a marked install command" { $marked.Success }

    if ($marked.Success) {
        $command = $marked.Groups['cmd'].Value
        Test-Case "the command is valid PowerShell" {
            $errs = $null
            [void][System.Management.Automation.Language.Parser]::ParseInput($command, [ref]$null, [ref]$errs)
            $errs.Count -eq 0
        }
        Test-Case "the command is a single line" { $command -notmatch '\r?\n' }
        Test-Case "the command pins no version" { $command -notmatch 'v\d+\.\d+\.\d+' }
        Test-Case "the command uses a process-only policy bypass" {
            $command -match '-NoProfile\s+-ExecutionPolicy\s+Bypass\s+-File'
        }
        Test-Case "the command never runs an installer it failed to download" {
            $command -match 'Test-Path'
        }
        # Stage 2's repository is public: needing GitHub here would be a
        # regression, not a feature.
        Test-Case "the command needs no GitHub CLI and no sign-in" {
            ($command -notmatch '\bgh\b') -and ($command -notmatch 'auth login')
        }
        Test-Case "the command forces TLS 1.2 (old Windows defaults to 1.0)" {
            $command -match 'SecurityProtocol'
        }
        Test-Case "the command contains only straight quotes" {
            $command -notmatch '[\u2018\u2019\u201C\u201D]'
        }
    }

} finally {
    Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ""
if ($script:Failed -eq 0) {
    Write-Host ("All $script:Passed tests passed.") -ForegroundColor Green
    exit 0
}
Write-Host ("$script:Failed of " + ($script:Passed + $script:Failed) + " tests FAILED.") -ForegroundColor Red
exit 1
