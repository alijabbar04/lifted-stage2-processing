@echo off
rem ===========================================================================
rem  Stage 2 - Processing : double-click installer
rem ---------------------------------------------------------------------------
rem  For anyone who would rather not touch PowerShell at all. Download this one
rem  file and double-click it - it fetches everything else by itself.
rem
rem  All it does is start PowerShell for the installer with an execution-policy
rem  bypass that lives and dies with that ONE process:
rem
rem      powershell.exe -NoProfile -ExecutionPolicy Bypass ...
rem
rem  Nothing on this PC is changed by that: it is not Set-ExecutionPolicy, it
rem  touches no registry setting, and every other PowerShell window keeps the
rem  policy it had. This is the documented, supported way to run one script.
rem
rem  No administrator rights are needed. Anything typed after the file name is
rem  passed on to the installer (for example:  install.cmd -CheckOnly).
rem ===========================================================================
setlocal
title Stage 2 - Processing : install

rem %~dp0 keeps its trailing backslash and is quoted everywhere below, so a
rem folder name containing spaces (Desktop, OneDrive, "My Documents") is fine.
rem Nothing here depends on the current directory, so double-clicking from any
rem location - or running it from C:\Windows\System32 - behaves identically.
set "HERE=%~dp0"
set "LOCAL=%HERE%install.ps1"

rem Use the full path to PowerShell in case PATH is unusual on this machine.
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS%" set "PS=powershell.exe"

if exist "%LOCAL%" (
    rem A repo checkout, or a folder someone was sent: use the installer here.
    "%PS%" -NoProfile -ExecutionPolicy Bypass -File "%LOCAL%" %*
) else (
    rem Downloaded on its own. Fetch the current installer from the latest
    rem published release and run it. Stage 2's repository is public, so this
    rem needs no GitHub account, no sign-in and no GitHub CLI.
    rem 3072 is Tls12 - spelled numerically so no .NET enum name is needed.
    "%PS%" -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue';[Net.ServicePointManager]::SecurityProtocol=[Net.ServicePointManager]::SecurityProtocol -bor 3072;$d=Join-Path $env:TEMP ('Stage2Boot-'+[guid]::NewGuid().ToString('N'));$null=New-Item -ItemType Directory -Force $d;$f=Join-Path $d 'install.ps1';try{Invoke-WebRequest -Uri 'https://github.com/alijabbar04/lifted-stage2-processing/releases/latest/download/install.ps1' -OutFile $f -UseBasicParsing -TimeoutSec 120}catch{Write-Host ('Could not download the installer: '+$_.Exception.Message) -ForegroundColor Red;exit 5};if(-not(Test-Path -LiteralPath $f)){Write-Host 'Could not download the installer. Check your internet connection and try again.' -ForegroundColor Red;exit 5};& $f %*"
)
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo The installer stopped without finishing ^(code %RC%^). The reason is
    echo printed above. You can close this window and try again.
)
rem Double-clicked windows close instantly otherwise, taking the message with them.
pause
exit /b %RC%
