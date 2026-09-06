Stage 2 - Processing v1.4.0 - Obsidian Compact
===========================

Stage 2 classifies, rotates, splits, ranks and organises worker documents for
Stage 3. Employment Contracts are placed in Overwrite Documents.

Page orientation is checked locally with a bundled CPU-only ONNX model. The
v1.3.2 default is Audit only: pages are not changed and no orientation-related
API calls are made. Automatic high-confidence correction is opt-in in Settings.

The app stores its Anthropic API key in Windows Credential Manager. If this is
a new installation, open Settings (the cog) and enter the key once. Updating the
app does not erase an existing key.

LibreOffice is recommended for Office documents:
  winget install --id TheDocumentFoundation.LibreOffice -e

Uninstall from Windows Settings > Apps. User documents and reports are left in
place.
