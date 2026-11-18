# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

Windows：产出单文件 dist/ds-trim-gateway.exe
macOS：额外用 BUNDLE 包成 dist/ds-trim-gateway.app
       （不加 BUNDLE 只有裸可执行文件，双击无法启动、也没有 Dock 图标）
"""

import sys

a = Analysis(
    ['launcher.py'],
    pathex=[],
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
    name='ds-trim-gateway',
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
)

if sys.platform == 'darwin':
    app = BUNDLE(
        exe,
        name='ds-trim-gateway.app',
        icon=None,
        bundle_identifier='io.github.zhangxianhe666.dstrim',
        info_plist={
            'CFBundleName': 'ds-trim-gateway',
            'CFBundleDisplayName': 'ds-trim-gateway',
            'CFBundleShortVersionString': '1.0.0',
            'CFBundleVersion': '1.0.0',
            'NSHighResolutionCapable': True,
            'LSMinimumSystemVersion': '11.0',
            'LSBackgroundOnly': False,
        },
    )
