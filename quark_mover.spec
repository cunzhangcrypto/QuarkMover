# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from pathlib import Path

block_cipher = None

# Find DrissionPage configs.ini (robust for CI)
datas_extra = []
try:
    import DrissionPage
    dp_path = Path(DrissionPage.__file__).parent
    dp_ini = dp_path / '_configs' / 'configs.ini'
    if dp_ini.exists():
        datas_extra.append((str(dp_ini), 'DrissionPage/_configs'))
except ImportError:
    pass

# static 目录（二维码等）
if Path('static').is_dir():
    for f in Path('static').iterdir():
        if f.is_file():
            datas_extra.append((str(f), 'static'))

a = Analysis(
    ['quark_mover.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('config/config.example.json', 'config'),
    ] + datas_extra,
    hiddenimports=[
        'DrissionPage',
        'DrissionPage._base',
        'DrissionPage._base.chromium',
        'DrissionPage._base.driver',
        'DrissionPage._base.base',
        'DrissionPage._configs',
        'DrissionPage._configs.chromium_options',
        'DrissionPage._configs.session_options',
        'DrissionPage._elements',
        'DrissionPage._functions',
        'DrissionPage._functions.browser',
        'DrissionPage._pages',
        'DrissionPage._pages.chromium_page',
        'DrissionPage._units',
        'lxml', 'lxml.etree', 'lxml.html',
        'requests',
        'cssselect',
        'DownloadKit',
        'websocket',
        'click',
        'tldextract',
        'psutil',
        'httpx',
        'httpcore',
        'loguru',
        'h11',
        'certifi',
        'idna',
        'sniffio',
        'anyio',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='QuarkMover',
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
    icon=None,
)
