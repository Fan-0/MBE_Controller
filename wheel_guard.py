"""
Mouse-wheel guard — the wheel never changes an input widget's value.

Problem (user report 2026-10-06): scrolling a panel to reach another section changed
whatever spin box / dropdown / slider happened to pass under the cursor (Recipe
editor tables, RHEED step panels, …).

install(app) puts ONE application-wide event filter in place, so it covers every
window and every widget created later (table cell editors, dialogs, the RHEED tab
embedded in the main controller). For QAbstractSpinBox (QSpinBox, QDoubleSpinBox,
date/time edits), QComboBox and QSlider the wheel event is NOT delivered to the
widget; a copy goes to the viewport of the nearest enclosing QAbstractScrollArea
(scroll area, table, list…) instead, so the page / table still scrolls under the
cursor. (Re-sending to the parent widget does NOT work: Qt only propagates
SPONTANEOUS wheel events up the parent chain, a sendEvent() copy stops at the first
receiver — found while testing.)
Values still change by typing, the arrow buttons, the keyboard and the dropdown list.

Deliberately NOT blocked: QScrollBar (it is a QAbstractSlider too — scrolling must
keep working), pyqtgraph plots (wheel = zoom), tab bars, text/list views.

Standalone (PyQt5 only): imported by main.py, recipe_editor.py and rheed_app.py.
"""

from PyQt5.QtCore import QEvent, QObject, QPointF
from PyQt5.QtGui import QWheelEvent
from PyQt5.QtWidgets import (QAbstractScrollArea, QAbstractSpinBox, QApplication, QComboBox,
                             QSlider)

_GUARDED = (QAbstractSpinBox, QComboBox, QSlider)


def _scroll_target(w):
    """Viewport of the nearest enclosing scroll area (None if there is none)."""
    p = w.parentWidget()
    while p is not None:
        if isinstance(p, QAbstractScrollArea) and p.viewport() is not None:
            return p.viewport()
        p = p.parentWidget()
    return None


class WheelGuard(QObject):
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Wheel and isinstance(obj, _GUARDED):
            vp = _scroll_target(obj)
            if vp is not None and vp.isAncestorOf(obj):
                local = QPointF(obj.mapTo(vp, event.pos()))
                fwd = QWheelEvent(local, event.globalPosF(), event.pixelDelta(), event.angleDelta(),
                                  event.buttons(), event.modifiers(), event.phase(), event.inverted())
                QApplication.sendEvent(vp, fwd)
            return True                             # never reaches the input widget
        return False


_guard = None


def install(app: QApplication) -> WheelGuard:
    """Install once per QApplication (repeat calls are harmless)."""
    global _guard
    if _guard is None:
        _guard = WheelGuard(app)
        app.installEventFilter(_guard)
    return _guard
