; ===========================================================================
;  Stage2 Processing - Windows installer  (Inno Setup 6.1+)
; ---------------------------------------------------------------------------
;  Built by build_installer.py, which passes these /D defines:
;     ApiKeyFile = path to a temp plaintext key file (embedded as a dontcopy
;                  file 'apikey.dat'; build_installer.py creates + deletes it,
;                  it is NEVER committed and NEVER lands in {app}).
;     LoUrl      = full URL of the current LibreOffice x64 MSI.
;     LoMsi      = the MSI filename (e.g. LibreOffice_26.2.4_Win_x86-64.msi).
;     AppExe / AppIco = source paths of the built payload exe / icon.
;  The Anthropic API key is NOT stored in this .iss file. It is embedded only
;  as the compiled-in 'apikey.dat' resource and written to Windows Credential
;  Manager at install time by stage2_credhelper.exe.
; ===========================================================================

#ifndef AppExe
#define AppExe "dist\Stage2 Processing.exe"
#endif
#ifndef AppIco
#define AppIco "stage2.ico"
#endif
#ifndef CredHelper
#define CredHelper "dist\stage2_credhelper.exe"
#endif
#ifndef LoUrl
#define LoUrl "https://download.documentfoundation.org/libreoffice/stable/26.2.4/win/x86_64/LibreOffice_26.2.4_Win_x86-64.msi"
#endif
#ifndef LoMsi
#define LoMsi "LibreOffice_x64.msi"
#endif
; Shared add-on payload: the user GUIDES and Stage 2's helper TOOLS. These
; were previously never shipped, so on any machine but the developer's the
; "Guide" button said "Guide not found" and every Tools entry showed
; "(NOT FOUND)". Both now install to %LOCALAPPDATA%\Lifted\{Guides,Tools},
; which is where the app looks first.
; Override with:  ISCC.exe /DAddOns="<path to payload>" Stage2_Installer.iss
; The payload folder (Guides\*.pdf + Tools\*.exe) is NOT in this repository -
; the Tools are separately built exes and the Guides are the stage PDFs.
#ifndef AddOns
  #define AddOns AddBackslash(SourcePath) + "payload"
#endif

#define MyAppName "Stage2 Processing"
#define MyAppVersion "1.4.0"
#define MyAppPublisher "Lifted / Ali Jabbar"
#define MyAppExeName "Stage2 Processing.exe"

[Setup]
AppId={{7C3B2E14-9A55-4E2C-9E1F-2B7A9E2F5A21}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableWelcomePage=no
OutputDir=Output
OutputBaseFilename=Stage2_Processing_Preconfigured_Setup
; instructions shown INSIDE the installer (so the exe needs no README beside it)
InfoBeforeFile=first_run.txt
SetupIconFile={#AppIco}
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
; Allow a per-user fallback (installs under %LOCALAPPDATA%\Programs) if the
; user cannot / will not elevate - {autopf} then resolves there automatically.
PrivilegesRequiredOverridesAllowed=dialog commandline
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#AppExe}"; DestDir: "{app}"; DestName: "{#MyAppExeName}"; Flags: ignoreversion
Source: "{#CredHelper}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#AppIco}"; DestDir: "{app}"; DestName: "stage2.ico"; Flags: ignoreversion
Source: "README.txt"; DestDir: "{app}"; Flags: ignoreversion isreadme
; ---- USER GUIDES (all three stages; <1 MB) -------------------------------
Source: "{#AddOns}\Guides\*.pdf"; DestDir: "{localappdata}\Lifted\Guides"; Flags: ignoreversion skipifsourcedoesntexist
; also the legacy folder an OLDER app build reads (exactly
; Path.home()/"Documents"/"Lifted"/"Guides"), so guides work even if
; another stage on this PC has not been updated yet.
Source: "{#AddOns}\Guides\*.pdf"; DestDir: "{%USERPROFILE}\Documents\Lifted\Guides"; Flags: ignoreversion skipifsourcedoesntexist
; Stage 2's current guide is mandatory and wins over any older add-on copy.
Source: "..\..\docs\USER_GUIDE.pdf"; DestDir: "{app}\Guides"; DestName: "Stage 2 Guide - AI Processing.pdf"; Flags: ignoreversion
Source: "..\..\docs\USER_GUIDE.pdf"; DestDir: "{localappdata}\Lifted\Guides"; DestName: "Stage 2 Guide - AI Processing.pdf"; Flags: ignoreversion
Source: "..\..\docs\USER_GUIDE.pdf"; DestDir: "{%USERPROFILE}\Documents\Lifted\Guides"; DestName: "Stage 2 Guide - AI Processing.pdf"; Flags: ignoreversion
; ---- STAGE 2 HELPER TOOLS (the Tools panel) ------------------------------
; Per-user, so no elevation is needed for them and they survive an app
; upgrade. Stage 2 is the only consumer, so they ship only here.
Source: "{#AddOns}\Tools\*.exe"; DestDir: "{localappdata}\Lifted\Tools"; Flags: ignoreversion skipifsourcedoesntexist
; The API key, embedded as a compiled-in resource only (never copied to {app}).
; ExtractTemporaryFile() drops it in {tmp} just long enough for the credential
; helper to read + delete it.
Source: "apikey.dat"; Flags: dontcopy

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\stage2.ico"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
; Desktop shortcut is REQUIRED (how the colleague launches Stage 2) - always created.
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\stage2.ico"

[Run]
; (a) Write the pre-bundled Anthropic API key into Windows Credential Manager,
;     under the same keyring service/user Stage 2 reads from. BeforeInstall
;     extracts the embedded key to {tmp}; the helper reads it and deletes it.
Filename: "{app}\stage2_credhelper.exe"; Parameters: "set ""{tmp}\apikey.dat"""; \
    Flags: runhidden waituntilterminated; StatusMsg: "Configuring the Anthropic API key..."; \
    BeforeInstall: PrepApiKey

; (b) Install LibreOffice silently, but only if it isn't already present AND the
;     MSI downloaded OK (NeedLibreOffice does the detect + download-with-progress).
Filename: "{sys}\msiexec.exe"; Parameters: "/i ""{tmp}\{#LoMsi}"" /qn /norestart"; \
    Flags: waituntilterminated; StatusMsg: "Installing LibreOffice (350 MB - this may take a minute)..."; \
    Check: NeedLibreOffice

; Optional: offer to launch Stage 2 at the end.
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName} now"; \
    Flags: nowait postinstall skipifsilent

[UninstallRun]
; Remove the API key from Windows Credential Manager on uninstall. LibreOffice
; is deliberately left installed (may be used by other apps).
Filename: "{app}\stage2_credhelper.exe"; Parameters: "del"; \
    Flags: runhidden waituntilterminated; RunOnceId: "DelApiKey"

[Code]
// NOTE: comments here use // deliberately - Inno's { } block comments would be
// terminated early by a '}' inside constants like {tmp} / {app}.
var
  LoWarn: Boolean;          // set if LibreOffice could not be installed
  LoWarnMsg: String;

// Extract the compiled-in key resource to {tmp} just before the credential
// helper runs. The helper reads and then deletes it.
procedure PrepApiKey;
begin
  ExtractTemporaryFile('apikey.dat');
end;

// True if a working soffice.exe is already on this machine.
function LibreOfficeInstalled: Boolean;
begin
  Result :=
    FileExists('C:\Program Files\LibreOffice\program\soffice.exe') or
    FileExists('C:\Program Files (x86)\LibreOffice\program\soffice.exe') or
    FileExists(ExpandConstant('{autopf}\LibreOffice\program\soffice.exe')) or
    FileExists(ExpandConstant('{localappdata}\Programs\LibreOffice\program\soffice.exe'));
end;

// Download the LibreOffice MSI to {tmp}, showing a progress page. Returns True
// only when a plausibly-valid MSI (large, non-empty) is on disk. A truncated
// download or an HTML error page is orders of magnitude smaller than the real
// ~350 MB MSI, so the size gate is our "valid MSI" signal (Inno's Pascal RTL
// cannot cheaply read arbitrary file bytes); msiexec itself rejects a corrupt
// package as a final backstop.
function DownloadLibreOfficeMsi: Boolean;
var
  DownloadPage: TDownloadWizardPage;
  Dest: String;
  Size: Int64;
begin
  Result := False;
  Dest := ExpandConstant('{tmp}\{#LoMsi}');
  DownloadPage := CreateDownloadPage(
    'Downloading LibreOffice',
    'LibreOffice is required to convert Office files (.docx/.xlsx/.pptx) to PDF.'
    + #13#10 + 'Downloading the installer (~350 MB) - this can take a few minutes...',
    nil);
  DownloadPage.Clear;
  DownloadPage.Add('{#LoUrl}', '{#LoMsi}', '');
  DownloadPage.Show;
  try
    try
      DownloadPage.Download;
      if FileExists(Dest) then
      begin
        Size := 0;
        if not FileSize64(Dest, Size) then
          Size := 0;
        if Size > 50000000 then   // > ~50 MB => a real MSI, not an error page
          Result := True
        else
        begin
          LoWarnMsg := 'the download looked too small to be a valid MSI';
          DeleteFile(Dest);
        end;
      end
      else
        LoWarnMsg := 'the download did not complete';
    except
      LoWarnMsg := GetExceptionMessage;
      Result := False;
    end;
  finally
    DownloadPage.Hide;
  end;
end;

// [Run] Check: install LibreOffice only if it's missing AND we could fetch a
// valid MSI. Never aborts the whole install - on any failure it records a
// warning shown at the end and returns False so msiexec is skipped.
function NeedLibreOffice: Boolean;
begin
  Result := False;
  if LibreOfficeInstalled then
    Exit;
  if DownloadLibreOfficeMsi then
    Result := True
  else
  begin
    LoWarn := True;
    if LoWarnMsg = '' then
      LoWarnMsg := 'no internet connection or the download was blocked';
  end;
end;

// After the wizard finishes, if LibreOffice could not be installed, tell the
// user plainly that Office-file conversion falls back to text-only until they
// install it - but the app is otherwise ready.
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssDone) and LoWarn then
    MsgBox('Stage 2 is installed and ready.' + #13#10 + #13#10 +
      'However, LibreOffice could NOT be installed automatically (' + LoWarnMsg + ').'
      + #13#10 + 'Word / Excel / PowerPoint files will convert as TEXT-ONLY until '
      + 'LibreOffice is installed. Install it later from libreoffice.org and Stage 2 '
      + 'will use it automatically - no reconfiguration needed.',
      mbInformation, MB_OK);
end;
