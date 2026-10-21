"""
Vacuum Deposition Control System – Entry Point
Run with:  python main.py
Build EXE: pyinstaller build.spec
"""

import sys
import os
import logging

# Ensure the package root is on sys.path when frozen (PyInstaller)
if getattr(sys, "frozen", False):
    _BASE = sys._MEIPASS
else:
    _BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _BASE)

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt
from gui_app import MainWindow, apply_dark_theme
import wheel_guard


def main():
    # Route all WARNING+ log messages to stderr with timestamps
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # High-DPI support
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps,    True)

    app = QApplication(sys.argv)
    app.setApplicationName("Vac Deposition Control")
    apply_dark_theme(app)
    wheel_guard.install(app)          # mouse wheel never changes spin boxes / dropdowns / sliders

    window = MainWindow()
    screen = app.primaryScreen().availableGeometry()
    w = max(900, int(screen.width()  * 0.90))
    h = max(600, int(screen.height() * 0.90))
    window.resize(w, h)
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
