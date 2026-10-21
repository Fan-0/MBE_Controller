# recipe_editor.spec — PyInstaller spec for the standalone Recipe Editor
#
# Builds a lightweight executable for creating/editing deposition recipes
# with no hardware drivers (pyserial, minimalmodbus) required — safe to hand
# to lab members who need to write recipes ahead of time on their own PC.
#
# Build command (from project directory):
#   pip install pyinstaller PyQt5 pyqtgraph openpyxl
#   pyinstaller recipe_editor.spec
#
# Output: dist/RecipeEditor/RecipeEditor.exe  (one-folder build)

import glob
import os
import sys

from PyInstaller.building.api import PYZ, EXE, COLLECT
from PyInstaller.building.build_main import Analysis

block_cipher = None

# Anaconda/conda-forge Python ships several stdlib extension modules
# (pyexpat, _ctypes, _sqlite3, _bz2, _lzma) linked against DLLs that live in
# Library/bin instead of next to python.exe — a location PyInstaller's
# dependency scanner does not search by default. That silently omits the
# DLLs and the built exe crashes on launch with e.g. "DLL load failed while
# importing pyexpat" / "_ctypes". Only matches when building with a conda
# interpreter; a no-op (empty list) otherwise, so this is safe for anyone
# building from a plain python.org install too.
# conda-forge's PyQt5 package links its .pyd files against Qt DLLs renamed
# with a "_conda" suffix (e.g. Qt5Core_conda.dll) to avoid clashing with a
# pip-installed Qt on the same system. These also live in Library/bin and are
# missed the same way. Only the 3 modules we actually import are pulled in
# here — the full conda Qt5*_conda.dll set is ~170MB (includes an unused
# 112MB WebEngine DLL) and would undo the size trimming above.
_extra_binaries = []
_conda_lib_bin = os.path.join(sys.prefix, "Library", "bin")
if os.path.isdir(_conda_lib_bin):
    _dll_patterns = [
        "libexpat*.dll", "ffi*.dll", "sqlite3*.dll", "libbz2*.dll", "liblzma*.dll",
        "Qt5Core_conda.dll", "Qt5Gui_conda.dll", "Qt5Widgets_conda.dll",
    ]
    for _pattern in _dll_patterns:
        for _dll in glob.glob(os.path.join(_conda_lib_bin, _pattern)):
            _extra_binaries.append((_dll, "."))

a = Analysis(
    ["recipe_editor.py"],
    pathex=["."],
    binaries=_extra_binaries,
    datas=[],
    hiddenimports=[
        "PyQt5.QtCore",
        "PyQt5.QtGui",
        "PyQt5.QtWidgets",
        "pyqtgraph",
        "pyqtgraph.graphicsItems",
        "openpyxl",
        "openpyxl.styles",
        "openpyxl.workbook",
    ],
    hookspath=[],
    runtime_hooks=[],
    # pyqtgraph's PyInstaller hook eagerly collects ALL its submodules, including
    # optional exporters (HDF5Exporter, MatplotlibExporter) whose only purpose is
    # to probe for these libraries. In a conda base env where numba/scipy/h5py/etc.
    # happen to be installed, that pulls ~300MB of unrelated packages into the
    # build even though the recipe table + timeline preview never touch them.
    excludes=[
        "tkinter", "matplotlib", "serial", "minimalmodbus",
        "numba", "llvmlite", "scipy", "h5py",
        "sphinx", "docutils", "alabaster", "babel", "pygments",
        "jedi", "parso", "IPython", "ipykernel", "jupyter", "notebook",
        "zmq", "tornado", "traitlets",
        "lxml", "cryptography",
        "win32com", "win32api", "win32con", "pythoncom", "pywintypes",
    ],
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
    name="RecipeEditor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="RecipeEditor",
)
