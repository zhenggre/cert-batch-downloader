# -*- mode: python ; coding: utf-8 -*-


# 用 SPECPATH 定位，不写死绝对路径。本文件在 开发维护/，工具本体在 ../程序/
import os as _os
_SRC = _os.path.normpath(_os.path.join(SPECPATH, '..', '程序'))

a = Analysis(
    [_os.path.join(_SRC, 'web_app.py')],
    pathex=[],
    binaries=[],
    datas=[(_os.path.join(_SRC, 'web'), 'web')],
    hiddenimports=['openpyxl', 'requests', 'error_help', 'school_merge', 'site_probe', 'platform_jinshuju'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pandas', 'numpy', 'matplotlib', 'scipy', 'PIL', 'lxml', 'pytest', 'setuptools', 'pydoc', 'tkinter'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='证书下载器',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='证书下载器',
)
