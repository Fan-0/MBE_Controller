"""Shared look for the RHEED panels (palette duplicated from gui_app so this package
never imports it) + small widget helpers used by rheed_tab and video_tab."""

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QBrush, QColor, QPainter, QPen
from PyQt5.QtWidgets import QGroupBox, QLabel, QPushButton

BG = "#1e1e2e"; PBG = "#2a2a3e"; BDR = "#44446a"
BL = "#5e9cf3"; GR = "#50fa7b"; RD = "#ff5555"; OR = "#ffb86c"; PU = "#bd93f9"
FG = "#cdd6f4"; DIM = "#6c7086"; YL = "#f1fa8c"; CY = "#8be9fd"

TABS_CSS = (f"QTabWidget::pane{{border:1px solid {BDR};background:{PBG};}}"
            f"QTabBar::tab{{background:{BG};color:{DIM};padding:4px 12px;border:1px solid {BDR};"
            f"border-bottom:none;border-radius:4px 4px 0 0;}}"
            f"QTabBar::tab:selected{{background:{PBG};color:{FG};}}")


class _Dot(QLabel):
    """Status LED (same look as gui_app.LED, but with arbitrary colour)."""
    def __init__(self, sz=14):
        super().__init__(); self._c = QColor(DIM); self._sz = sz; self.setFixedSize(sz, sz)

    def set_color(self, c):
        if QColor(c) != self._c:
            self._c = QColor(c); self.update()

    def paintEvent(self, _):
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        g = QColor(self._c); g.setAlpha(45); p.setBrush(QBrush(g)); p.setPen(Qt.NoPen)
        p.drawEllipse(0, 0, self._sz, self._sz)
        p.setBrush(QBrush(self._c)); p.setPen(QPen(self._c.darker(160), 1))
        p.drawEllipse(3, 3, self._sz - 6, self._sz - 6)


class _ClickLabel(QLabel):
    """QLabel that emits clicked() — used for result thumbnails."""
    clicked = pyqtSignal()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(e)


def _btn(text, color=BL, w=None):
    b = QPushButton(text)
    b.setStyleSheet(f"QPushButton{{background:{PBG};color:{color};border:1px solid {color};"
                    f"border-radius:4px;padding:4px 10px;}}"
                    f"QPushButton:hover{{background:{BDR};}}"
                    f"QPushButton:disabled{{color:{DIM};border-color:{BDR};}}")
    if w:
        b.setFixedWidth(w)
    return b


def _group(title):
    g = QGroupBox(title)
    g.setStyleSheet(f"QGroupBox{{color:{FG};border:1px solid {BDR};border-radius:5px;"
                    f"margin-top:8px;padding-top:6px;font-weight:bold;}}"
                    f"QGroupBox::title{{subcontrol-origin:margin;left:8px;padding:0 4px;}}")
    return g


def np_to_pixmap(img):
    """BGR or grey uint8 numpy image → QPixmap (copies the data)."""
    import numpy as np
    from PyQt5.QtGui import QImage, QPixmap
    a = np.ascontiguousarray(img)
    if a.ndim == 2:
        q = QImage(a.data, a.shape[1], a.shape[0], a.strides[0], QImage.Format_Grayscale8)
    else:
        a = np.ascontiguousarray(a[:, :, ::-1])            # BGR → RGB
        q = QImage(a.data, a.shape[1], a.shape[0], a.strides[0], QImage.Format_RGB888)
    return QPixmap.fromImage(q.copy())


def display_levels(img, lo_pct=0.5, hi_pct=99.9):
    """Percentile display levels for a dim RHEED frame (subsampled — cheap per frame)."""
    import numpy as np
    s = max(1, img.shape[1] // 240)                  # full frames subsampled, thumbnails not
    sub = img[::s, ::s]
    lo, hi = float(np.percentile(sub, lo_pct)), float(np.percentile(sub, hi_pct))
    return lo, max(hi, lo + 4.0)
