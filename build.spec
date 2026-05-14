# build.spec  –  PyInstaller spec file for Vac Deposition Control System
#
# Build command (from project directory):
#   pip install pyinstaller
#   pyinstaller build.spec
#
# Output: dist/VacControl/VacControl.exe  (one-folder build)
# For a single .exe: change onefile=True below (larger, slower first launch)

import sys
from PyInstaller.building.api import PYZ, EXE, COLLECT
from PyInstaller.building.build_main import Analysis

block_cipher = None

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=[
        "PyQt5.QtCore",
        "PyQt5.QtGui",
        "PyQt5.QtWidgets",
        "pyqtgraph",
        "pyqtgraph.graphicsItems",
        "serial",
        "serial.tools",
        "serial.tools.list_ports",
        "openpyxl",
        "openpyxl.styles",
        "openpyxl.workbook",
        "minimalmodbus",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VacControl",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,         # Set True to see console output while debugging
    icon=None,             # Replace with path to .ico file: icon="icon.ico"
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="VacControl",
)
