Stage 2 - Processing v1.5.3 - Obsidian / Jade (build 2026.09.08-state1)
===========================

Stage 2 classifies, ranks and organises worker documents for Stage 3, with
optional orientation correction and supported bundle splitting. Employment
Contracts are placed in Overwrite Documents.

This is the public, credential-free setup. It installs the app, shortcuts and
guide, not a preconfigured API key, bot tokens or LibreOffice. No GitHub account
or repository invitation is needed to download it from the public release.

Page orientation is checked locally with a bundled CPU-only ONNX model. The
default for a new configuration is Audit only: pages are not changed and no orientation-related
API calls are made. Automatic high-confidence correction is opt-in in Settings.

The app stores its Anthropic API key in Windows Credential Manager. If this is
a new installation, open Settings (the cog) and enter the key once. Updating the
app does not erase an existing key.

On Windows, the batch writer keeps the same `.docreview_batch_writer.lock`
path while work is active and makes a best-effort cleanup after unlocking and
closing it. Another standard Python holder can keep the file open, leaving a
harmless remnant for the next successful use. POSIX and request/ledger locks
remain permanent; never delete lock files manually.

LibreOffice is recommended for Office documents:
  winget install --id TheDocumentFoundation.LibreOffice -e

Keep originals. A completed conversion or filename audit does not prove every
signature, image, cell, form value or layout association survived. Use Guide
for the processing, Reports, API Usage and separate AI-review workflows.

Uninstall from Windows Settings > Apps. User documents and reports are left in
place.
