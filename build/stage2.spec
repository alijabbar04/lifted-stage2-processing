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
# Bundle the icon, guide and provider-neutral review rules so the portable
# executable has the same workflow assets as the installed application.

import os

SRC = os.path.join(SPECPATH, os.pardir, 'src')
ORIENTATION_ASSETS = os.path.join(
    SPECPATH, os.pardir, 'assets', 'orientation')

a = Analysis(
    [os.path.join(SRC, 'Stage2_Processing.pyw')],
    pathex=[SRC],
    binaries=[],
    datas=[(ORIENTATION_ASSETS, os.path.join('assets', 'orientation')),
           (os.path.join(SRC, 'stage2.ico'), '.'),
           (os.path.join(SPECPATH, os.pardir, 'docs', 'ai-review'), os.path.join('docs', 'ai-review')),
           (os.path.join(SPECPATH, os.pardir, 'docs', 'USER_GUIDE.pdf'), 'docs')],
    hiddenimports=['onnxruntime'],
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
    # Some managed Windows sessions expose C:\Windows\Temp through
    # GetTempPathW even for a per-user launch. Tcl/Tk can unpack there but
    # then fail to read its own init.tcl. Pin one-file extraction to a
    # per-user directory; PyInstaller expands %LOCALAPPDATA% on Windows and
    # still creates an isolated _MEI... child for each process.
    runtime_tmpdir=r'%LOCALAPPDATA%\Lifted\Stage2Runtime',
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[os.path.join(SRC, 'stage2.ico')],
)
