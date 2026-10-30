# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_all


ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
esptool_datas, esptool_binaries, esptool_imports = collect_all("esptool")

a = Analysis(
    [os.path.join(ROOT, "src", "esp32_flasher_gui.py")],
    pathex=[ROOT],
    binaries=esptool_binaries,
    datas=[(os.path.join(ROOT, "assets"), "assets")] + esptool_datas,
    hiddenimports=esptool_imports + ["serial.tools.list_ports_windows", "ota_ui", "batch_ota"],
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
    name="Ys元思烧录平台",
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
    version=os.path.join(ROOT, "packaging", "version_info_main.txt"),
    icon=[os.path.join(ROOT, "assets", "ysneuro.ico")],
    manifest=os.path.join(ROOT, "packaging", "app.manifest"),
)
