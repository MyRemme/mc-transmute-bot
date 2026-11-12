# -*- mode: python ; coding: utf-8 -*-

analysis = Analysis(
    ["gui.py"],
    pathex=[],
    binaries=[],
    datas=[("assets/success.wav", "assets"), ("assets/app.png", "assets"),
           ("assets/app.ico", "assets")],
    hiddenimports=["pynput.keyboard._win32", "pynput.mouse._win32"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
archive = PYZ(analysis.pure)
executable = EXE(
    archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="嬗变台自动刷取",
    icon="assets/app.ico",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
collection = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="MCTransmuteBot",
)
