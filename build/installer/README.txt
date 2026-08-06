Stage2 Processing
=================

WHAT GOT INSTALLED
------------------
- Stage2 Processing.exe  (the app) + a Start Menu and Desktop shortcut.
- Your Anthropic API key was written to the Windows Credential Manager
  automatically, so the app is ready to use immediately — you do NOT need to
  paste a key into Settings.
- LibreOffice (if it wasn't already installed) — used to convert Word / Excel /
  PowerPoint files to PDF.

HOW TO RUN IT
-------------
Double-click the "Stage2 Processing" icon on your Desktop.

LIBREOFFICE
-----------
LibreOffice is REQUIRED for full conversion of Office documents (.docx, .xlsx,
.pptx) to PDF. The installer installs it silently the first time. If you had no
internet during install, Office files will convert as TEXT-ONLY until you
install LibreOffice (from https://www.libreoffice.org/download) — Stage 2 then
picks it up automatically, no reconfiguration needed.

THE API KEY
-----------
The Anthropic API key is stored in the Windows Credential Manager (the same
secure store Stage 2 uses when you paste a key into Settings). It is NOT saved
in plain text on disk. Every document Stage 2 processes uses this key and bills
the key's Anthropic account.

UNINSTALLING
------------
Uninstall from Windows Settings > Apps (or Add/Remove Programs). This removes
the app, its shortcuts, and the API key from the Credential Manager. LibreOffice
is left installed (you may use it for other things).

SECURITY NOTE
-------------
This installer carried a pre-configured API key. Treat the installer file as
sensitive — do not post it publicly or share it beyond the intended team.
