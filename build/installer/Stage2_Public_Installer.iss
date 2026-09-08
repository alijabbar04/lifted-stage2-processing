; Public, credential-free Stage 2 installer. Safe to publish on GitHub.
; The Anthropic API key is entered once in Stage 2 Settings and is stored in
; Windows Credential Manager. Never add a key or apikey.dat to this script.

#define MyAppName "Stage 2 - Processing"
#define MyAppVersion "1.5.1"
#define MyAppPublisher "Lifted"
#define MyAppExeName "Stage 2 - Processing.exe"
#define SrcExe "..\..\dist\Stage 2 - Processing.exe"
#define SrcIco "..\..\src\stage2.ico"
#define GuideSrc "..\..\docs\USER_GUIDE.pdf"

[Setup]
AppId={{6A85F75A-33C9-47D1-97A7-4D993183550B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DisableProgramGroupPage=yes
DisableWelcomePage=no
OutputDir=Output
OutputBaseFilename=Stage2_Processing_Setup
InfoBeforeFile=first_run_public.txt
SetupIconFile={#SrcIco}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#SrcExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SrcIco}"; DestDir: "{app}"; DestName: "stage2.ico"; Flags: ignoreversion
Source: "README_public.txt"; DestDir: "{app}"; DestName: "README.txt"; Flags: ignoreversion isreadme
Source: "{#GuideSrc}"; DestDir: "{localappdata}\Lifted\Guides"; DestName: "Stage 2 Guide - AI Processing.pdf"; Flags: ignoreversion
Source: "{#GuideSrc}"; DestDir: "{app}\Guides"; DestName: "Stage 2 Guide - AI Processing.pdf"; Flags: ignoreversion
Source: "{#GuideSrc}"; DestDir: "{%USERPROFILE}\Documents\Lifted\Guides"; DestName: "Stage 2 Guide - AI Processing.pdf"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\stage2.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\stage2.ico"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName} now"; Flags: nowait postinstall skipifsilent
