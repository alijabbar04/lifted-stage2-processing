# -*- mode: python ; coding: utf-8 -*-
#
# stage2_credhelper : tiny console exe the INSTALLER runs to write / remove the
# Anthropic API key in the Windows Credential Manager. Bundles keyring so it
# works on a machine with no Python.
#
# Built automatically by build_installer.py; build by hand with:
#     python -m PyInstaller --noconfirm build\installer\stage2_credhelper.spec

import os

from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []
tmp_ret = collect_all('keyring')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    [os.path.join(SPECPATH, 'credhelper.py')],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    name='stage2_credhelper',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
