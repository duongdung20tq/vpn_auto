# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['vpn_auto/rdp_main.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=['vpn_auto.cisco', 'vpn_auto.cisco_watchdog', 'vpn_auto.pulse', 'vpn_auto.watchdog', 'vpn_auto.prehook', 'vpn_auto.rdp', 'vpn_auto.rdp_ui', 'vpn_auto.rdp_store'],
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
    name='RdpAuto',
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
