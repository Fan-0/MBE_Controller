"""
RHEED Camera — standalone entry point.

    python rheed_app.py            scan for cameras, connect manually
    python rheed_app.py --sim      start straight on the simulated rotating source
    python rheed_app.py --video F  open video file F in the 🎞 Video analysis mode

The window just hosts rheed.rheed_tab.RHEEDTab, the same widget that will become a
tab of the main controller (see CONTEXT Section 20). Needs no serial hardware.
"""

import argparse
import logging
import os
import sys

if getattr(sys, "frozen", False):
    _BASE = sys._MEIPASS
else:
    _BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _BASE)

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QPalette
from PyQt5.QtWidgets import QApplication, QMainWindow

from rheed.rheed_tab import RHEEDTab, BG, PBG, FG, BL
import wheel_guard
import log_paths


def apply_dark_theme(app):
    """Same palette as gui_app.apply_dark_theme (not imported: it drags in devices/)."""
    app.setStyle("Fusion"); p = QPalette()
    for role, c in [
            (QPalette.Window, BG), (QPalette.WindowText, FG), (QPalette.Base, PBG),
            (QPalette.AlternateBase, "#31314a"), (QPalette.Text, FG), (QPalette.Button, PBG),
            (QPalette.ButtonText, FG), (QPalette.Highlight, BL), (QPalette.HighlightedText, "#fff")]:
        p.setColor(role, QColor(c))
    app.setPalette(p)


class RHEEDWindow(QMainWindow):
    def __init__(self, start_sim=False, video_path=None):
        super().__init__()
        self.setWindowTitle("RHEED Camera — CovAP MBE")
        self.tab = RHEEDTab(start_sim=start_sim, video_path=video_path,
                            out_root=log_paths.RHEED_DIR,
                            video_root=os.path.join(log_paths.LOCAL_ROOT, "rheed", "videos"))
        self.setCentralWidget(self.tab)
        self.setMinimumSize(900, 600)

    def closeEvent(self, e):
        self.tab.shutdown()
        e.accept()


def main():
    ap = argparse.ArgumentParser(description="RHEED camera viewer / rotation-sync capture")
    ap.add_argument("--sim", action="store_true", help="start on the simulated source")
    ap.add_argument("--video", metavar="FILE", help="open a video file in Video analysis mode")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setApplicationName("RHEED Camera")
    apply_dark_theme(app)
    wheel_guard.install(app)          # mouse wheel never changes spin boxes / dropdowns / sliders
    w = RHEEDWindow(start_sim=args.sim, video_path=args.video)
    scr = app.primaryScreen().availableGeometry()
    w.resize(max(900, int(scr.width() * 0.85)), max(600, int(scr.height() * 0.85)))
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
