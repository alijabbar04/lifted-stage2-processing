param(
    [Parameter(Mandatory = $true)]
    [string]$ExePath,
    [string]$ExpectedVersion = "1.5.3",
    [string]$ExpectedBuild = "2026.09.08-state1"
)

$ErrorActionPreference = "Stop"
$ExePath = [IO.Path]::GetFullPath($ExePath)
if (-not (Test-Path -LiteralPath $ExePath -PathType Leaf)) {
    throw "Frozen application not found: $ExePath"
}

$RuntimeBase = Join-Path $env:LOCALAPPDATA "Lifted\Stage2Runtime"
$BeforeIds = @(
    Get-CimInstance Win32_Process |
        Where-Object { $_.ExecutablePath -eq $ExePath } |
        ForEach-Object ProcessId
)
$OldTemp = $env:TEMP
$OldTmp = $env:TMP
$Failure = $null
$WindowOpened = $false

try {
    # Reproduce the environment that caused Tcl/Tk startup to fail. The frozen
    # application must ignore it in favour of its pinned per-user runtime dir.
    $env:TEMP = Join-Path $env:WINDIR "Temp"
    $env:TMP = $env:TEMP
    Start-Process -FilePath $ExePath `
        -WorkingDirectory (Split-Path -Parent $ExePath) `
        -WindowStyle Hidden | Out-Null
}
finally {
    $env:TEMP = $OldTemp
    $env:TMP = $OldTmp
}

$Deadline = (Get-Date).AddSeconds(25)
do {
    Start-Sleep -Milliseconds 250
    $Current = @(
        Get-CimInstance Win32_Process |
            Where-Object {
                $_.ExecutablePath -eq $ExePath -and
                $_.ProcessId -notin $BeforeIds
            }
    )
    foreach ($ProcessInfo in $Current) {
        $Process = Get-Process -Id $ProcessInfo.ProcessId `
            -ErrorAction SilentlyContinue
        if (-not $Process -or -not $Process.MainWindowTitle) { continue }
        if ($Process.MainWindowTitle -match "Unhandled exception|init\.tcl") {
            $Failure = "Frozen startup error: $($Process.MainWindowTitle)"
            break
        }
        if (($Process.MainWindowTitle -match ("Stage 2.*Processing.*v" + [regex]::Escape($ExpectedVersion))) -and
            $Process.MainWindowTitle.Contains($ExpectedBuild)) {
            $WindowOpened = $true
            Write-Host "Verified window title: $($Process.MainWindowTitle)"
            break
        }
    }
} while (-not $Failure -and -not $WindowOpened -and (Get-Date) -lt $Deadline)

if (-not $Failure -and -not $WindowOpened) {
    $Failure = "The real Stage 2 v$ExpectedVersion / $ExpectedBuild window did not open within 25 seconds."
}

if (-not $Failure) {
    $Extraction = @(
        Get-ChildItem -LiteralPath $RuntimeBase -Directory -Filter "_MEI*" `
            -ErrorAction SilentlyContinue
    )
    if ($Extraction.Count -eq 0) {
        $Failure = "The app did not extract under the pinned per-user runtime directory."
    }
}

# Close only processes created by this smoke test. Never touch an instance that
# was already running before the test began.
$Current = @(
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.ExecutablePath -eq $ExePath -and
            $_.ProcessId -notin $BeforeIds
        }
)
foreach ($ProcessInfo in $Current) {
    # These exact-path processes were started by this test without a folder or
    # work queue. Process cleanup requires no window input and cannot interrupt
    # a pre-existing production run (excluded by BeforeIds).
    Stop-Process -Id $ProcessInfo.ProcessId -ErrorAction SilentlyContinue
}
$CloseDeadline = (Get-Date).AddSeconds(10)
do {
    Start-Sleep -Milliseconds 250
    $Remaining = @(
        Get-CimInstance Win32_Process |
            Where-Object {
                $_.ExecutablePath -eq $ExePath -and
                $_.ProcessId -notin $BeforeIds
            }
    )
} while ($Remaining.Count -gt 0 -and (Get-Date) -lt $CloseDeadline)
foreach ($ProcessInfo in $Remaining) {
    Stop-Process -Id $ProcessInfo.ProcessId -Force -ErrorAction SilentlyContinue
}

if ($Failure) { throw $Failure }
Write-Host "Frozen GUI smoke test passed: Stage 2 v$ExpectedVersion opened from $RuntimeBase" `
    -ForegroundColor Green
