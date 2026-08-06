# -*- mode: python ; coding: utf-8 -*-
#
# Stage 2 - Processing : PyInstaller spec
#
# Faithful to the spec that produced the shipped v1.0.0 exe
# (SHA-256 E582CE31FA4637219FB9A80F1E51AEDB512BA9986D39575C8628AAF0203AD8A9),
# with the developer-specific absolute paths replaced by repo-relative ones.
#
# Build with:  .\build\build.ps1        (from the repo root)
#
# NOTE ON datas: deliberately empty, matching the production build. The app
# resolves stage2.ico from beside the script and falls back to an embedded PNG
# when it is absent, so the frozen exe gets its icon from the EXE(icon=...)
# resource below. api_usage.py is picked up automatically by import analysis
# because it sits next to Stage2_Processing.pyw in src/.

import os

SRC = os.path.join(SPECPATH, os.pardir, 'src')

a = Analysis(
    [os.path.join(SRC, 'Stage2_Processing.pyw')],
    pathex=[SRC],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Stage 2 - Processing',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[os.path.join(SRC, 'stage2.ico')],
)
