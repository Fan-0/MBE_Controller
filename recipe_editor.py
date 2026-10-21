#!/usr/bin/env python3
"""
Recipe Editor — standalone recipe builder for the Vacuum Deposition Control System.

Lets anyone build, edit, and validate deposition recipes (.xlsx) on any PC —
no hardware, PySerial, or Modbus drivers required. Recipes created here are
saved in the exact format the main VacDepositionControl app expects, so they
can be handed to a lab-mate and loaded directly via File > Load Recipe there.

Entry point: python recipe_editor.py
Build:       pyinstaller recipe_editor.spec  ->  dist/RecipeEditor/RecipeEditor.exe
"""
import os
import sys
import types

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QPalette, QColor, QFont
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFrame, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QComboBox, QFileDialog, QMessageBox,
)

try:
    import pyqtgraph as pg
    HAS_PG = True
except Exception:
    HAS_PG = False

from recipe_engine import RecipeEngine
# Timing constants only — bep_manager is stdlib-only, so the editor stays
# hardware-free and still estimates MEASURE_BEP steps correctly.
from bep_manager import BASELINE_DURATION, OPEN_DURATION, SETTLE_TIME
import wheel_guard

# ── Palette (matches the main VacDepositionControl app) ─────────────────
BG = "#1e1e2e"; PBG = "#2a2a3e"; BDR = "#44446a"
BL = "#5e9cf3"; GR = "#50fa7b"; RD = "#ff5555"; OR = "#ffb86c"; PU = "#bd93f9"
FG = "#cdd6f4"; DIM = "#6c7086"
# Source colour coding, shared by the shutter bands and the BEP window labels
SRC_IN_C = "#22ee44"   # Indium — green
SRC_SE_C = "#ee6622"   # Selenium — orange
BEP_C    = "#c0c0c0"   # MEASURE_BEP window — silver

# Actions that get a param1 source dropdown (mirrors gui_app.py)
ACTION_CH_OPTS = {
    "SET_TEMP":      ["Selenium (CH1)", "Substrate (CH2)", "Indium (EPC)"],
    "RAMP_TEMP":     ["Selenium (CH1)", "Substrate (CH2)", "Indium (EPC)"],
    "INCREASE_TEMP": ["Selenium (CH1)", "Substrate (CH2)", "Indium (EPC)"],
    "WAIT_TEMP":     ["Selenium (CH1)", "Substrate (CH2)", "Indium (EPC)"],
    "HEATER_ON":     ["Selenium (CH1)", "Substrate (CH2)", "Indium (EPC)"],
    "HEATER_OFF":    ["Selenium (CH1)", "Substrate (CH2)", "Indium (EPC)"],
}
ACTION_SHUTTER_OPTS = {"OPEN_SHUTTER": ["In", "Se", "Both"],
                       "CLOSE_SHUTTER": ["In", "Se", "Both"],
                       "MEASURE_BEP": ["In", "Se", "Both"]}

ALL_ACTIONS = [
    "SET_TEMP", "RAMP_TEMP", "INCREASE_TEMP", "WAIT_TEMP", "WAIT_PRESSURE", "WAIT_TIME",
    "OPEN_SHUTTER", "CLOSE_SHUTTER",
    "IG_ON", "IG_OFF", "HEATER_ON", "HEATER_OFF", "MEASURE_BEP", "RHEED_VIDEO", "GOTO", "END",
]


def bep_duration(src) -> float:
    """Wall-clock a MEASURE_BEP step costs, for the preview timeline estimate."""
    one = BASELINE_DURATION + OPEN_DURATION
    return one*2 + SETTLE_TIME if "both" in str(src or "both").lower() else one


def apply_dark_theme(app):
    app.setStyle("Fusion")
    p = QPalette()
    for role, c in [
        (QPalette.Window, BG), (QPalette.WindowText, FG), (QPalette.Base, PBG),
        (QPalette.AlternateBase, "#31314a"), (QPalette.Text, FG), (QPalette.Button, PBG),
        (QPalette.ButtonText, FG), (QPalette.Highlight, BL), (QPalette.HighlightedText, "#fff"),
    ]:
        p.setColor(role, QColor(c))
    app.setPalette(p)


class RecipeTable(QTableWidget):
    """QTableWidget with drag-and-drop row reordering (mirrors gui_app.RecipeTable).

    Qt's built-in InternalMove only moves QTableWidgetItems, not cell widgets
    (QComboBox etc.), so the drop is intercepted and a row_moved signal is
    emitted instead; the caller rebuilds the row through the normal populate
    path so every ComboBox is recreated with correct row-index lambdas.
    """
    row_moved = pyqtSignal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDragDropOverwriteMode(False)
        self.setDropIndicatorShown(True)

    def dropEvent(self, event):
        if event.source() is not self:
            event.ignore()
            return
        src_rows = sorted({idx.row() for idx in self.selectedIndexes()})
        if len(src_rows) != 1:
            event.ignore()
            return
        src = src_rows[0]
        target = self.indexAt(event.pos())
        if target.isValid():
            dst = target.row()
            if self.dropIndicatorPosition() == QAbstractItemView.BelowItem:
                dst += 1
        else:
            dst = self.rowCount()
        event.setDropAction(Qt.IgnoreAction)
        event.accept()
        if dst != src and dst != src + 1:
            self.row_moved.emit(src, dst)


def fmt_duration(sec):
    """Compact human duration: '45s', '12m 30s', '2h 05m'."""
    sec = int(max(0, round(sec)))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h: return f"{h}h {m:02d}m"
    if m: return f"{m}m {s:02d}s"
    return f"{s}s"


def simulate_recipe_timeline(steps):
    """Walk recipe steps and build a proportional timeline for the preview plot.

    GOTO loops are multiplied out in TIME but drawn as a single cycle: the loop
    body is traced once at its true size, then the clock jumps forward by the
    remaining passes so the total estimate is loop-aware. The loop's dashed
    region spans that whole multiplied span and carries the pass count, which
    keeps a 50-pass recipe readable instead of drawing 50 identical cycles.

    Returns:
      pts_t, pts_se, pts_sub  : temperature trace arrays
      shutter_segs            : List[(t0,t1,in_open,se_open)]
      rate_markers            : List[(t, rate_C/min, ch)]  — RAMP_TEMP events
      goto_regions            : List[(t0, t1, label_str)]
      unk_regions             : List[(t0, t1)]  — WAIT_TEMP / WAIT_PRESSURE
      bep_regions             : List[(t0, t1, source)]  — MEASURE_BEP windows
    """
    t = 0.0
    se_t, sub_t = 25.0, 25.0
    se_rate, sub_rate = 4.0, 100.0
    # Indium is tracked for TIMING only — the preview plots Se and Sub, but an
    # Indium calibration run still has to advance the clock correctly.
    in_t, in_rate = 25.0, 100.0      # EPC3004 factory SP rate
    cur_in = cur_se = False

    pts_t = [0.0]; pts_se = [se_t]; pts_sub = [sub_t]
    shutter_segs = []; cur_sh_t = 0.0
    rate_markers = []; goto_regions = []; unk_regions = []; bep_regions = []
    step_times = {}
    UNKNOWN_DT = 120.0

    def snap():
        pts_t.append(t); pts_se.append(se_t); pts_sub.append(sub_t)

    def push(dt):
        nonlocal t
        t += max(0.0, float(dt))
        pts_t.append(t); pts_se.append(se_t); pts_sub.append(sub_t)

    def flush_sh():
        if t > cur_sh_t:
            shutter_segs.append((cur_sh_t, t, cur_in, cur_se))

    def stretch(from_t, factor):
        """Scale everything drawn at/after from_t by `factor`, about from_t.

        Used by GOTO so the single drawn cycle is widened in proportion to fill
        the whole looped duration, instead of a cycle plus dead space.
        """
        nonlocal cur_sh_t
        def sc(v): return from_t + (v - from_t)*factor if v >= from_t else v
        for i, v in enumerate(pts_t):
            if v >= from_t: pts_t[i] = sc(v)
        for i, (a0, b0, x, y) in enumerate(shutter_segs):
            if b0 >= from_t: shutter_segs[i] = (sc(a0), sc(b0), x, y)
        for i, (a0, b0) in enumerate(unk_regions):
            if b0 >= from_t: unk_regions[i] = (sc(a0), sc(b0))
        for i, (tm, r, c) in enumerate(rate_markers):
            if tm >= from_t: rate_markers[i] = (sc(tm), r, c)
        for i, (a0, b0, lb) in enumerate(goto_regions):
            if b0 >= from_t: goto_regions[i] = (sc(a0), sc(b0), lb)
        for i, (a0, b0, sv) in enumerate(bep_regions):
            if b0 >= from_t: bep_regions[i] = (sc(a0), sc(b0), sv)
        for k, v in list(step_times.items()):
            if v >= from_t: step_times[k] = sc(v)
        if cur_sh_t >= from_t: cur_sh_t = sc(cur_sh_t)

    for s in steps:
        step_times[s.step] = t
        a = s.action

        if a == "RAMP_TEMP":
            try:
                rate = float(s.param2) if s.param2 else 4.0
                if RecipeEngine._is_indium_src(s.param1):
                    in_rate = rate          # timing only — In is not plotted
                else:
                    ch = RecipeEngine._src_to_ch(str(s.param1 or ""))
                    if ch == 1: se_rate = rate
                    else:       sub_rate = rate
                    rate_markers.append((t, rate, ch))
            except Exception:
                pass
            snap()

        elif a == "SET_TEMP":
            try:
                p1 = s.param1
                if RecipeEngine._is_indium_src(p1):
                    target = float(s.param2)
                    push(abs(target - in_t) / max(in_rate, 0.01) * 60.0); in_t = target
                else:
                    if isinstance(p1, str) and any(c.isalpha() for c in p1):
                        ch = RecipeEngine._src_to_ch(p1); target = float(s.param2)
                    else:
                        ch = 1; target = float(p1)
                    if ch == 1:
                        push(abs(target - se_t) / max(se_rate, 0.01) * 60.0); se_t = target
                        pts_se[-1] = se_t
                    else:
                        push(abs(target - sub_t) / max(sub_rate, 0.01) * 60.0); sub_t = target
                        pts_sub[-1] = sub_t
            except Exception:
                snap()

        elif a == "INCREASE_TEMP":
            try:
                delta = float(s.param2)
                if RecipeEngine._is_indium_src(s.param1):
                    push(abs(delta) / max(in_rate, 0.01) * 60.0); in_t += delta
                else:
                    ch = RecipeEngine._src_to_ch(str(s.param1 or ""))
                    if ch == 1:
                        push(abs(delta) / max(se_rate, 0.01) * 60.0); se_t += delta
                        pts_se[-1] = se_t
                    else:
                        push(abs(delta) / max(sub_rate, 0.01) * 60.0); sub_t += delta
                        pts_sub[-1] = sub_t
            except Exception:
                snap()

        elif a == "MEASURE_BEP":
            t0b = t
            push(bep_duration(s.param1))
            bep_regions.append((t0b, t, str(s.param1 or "Both")))

        elif a == "WAIT_TIME":
            try: push(float(s.param1))
            except Exception: push(10)

        elif a == "RHEED_VIDEO":                # blocks for the clip, like WAIT_TIME
            try: push(max(float(s.param1), 0.0) or 1.0)
            except Exception: push(1.0)

        elif a in ("WAIT_TEMP", "WAIT_PRESSURE"):
            t0u = t; push(UNKNOWN_DT); unk_regions.append((t0u, t))

        elif a in ("OPEN_SHUTTER", "CLOSE_SHUTTER"):
            flush_sh(); cur_sh_t = t
            src = str(s.param1 or "").lower()
            if a == "OPEN_SHUTTER":
                if "both" in src: cur_in = True; cur_se = True
                elif "se" in src: cur_se = True
                else:             cur_in = True
            else:
                if "both" in src: cur_in = False; cur_se = False
                elif "se" in src: cur_se = False
                else:             cur_in = False
            snap()

        elif a == "GOTO":
            try:
                tgt = int(float(s.param1))
                loops = int(float(s.param2)) if s.param2 is not None else 1
                body_start = step_times.get(tgt)
                if body_start is None:
                    snap()
                else:
                    body_dur = max(0.0, t - body_start)
                    passes   = max(1, loops + 1)
                    if body_dur > 0 and passes > 1:
                        stretch(body_start, passes)   # widen the one cycle to fill the loop
                        t = body_start + body_dur*passes
                    else:
                        snap()
                    goto_regions.append(
                        (body_start, t,
                         f"×{passes}  ({fmt_duration(body_dur*passes)})\n"
                         f"cycle length: {fmt_duration(body_dur)}"))
            except Exception:
                snap()

        else:
            snap()  # HEATER_ON/OFF, IG_ON/OFF, END — no time advance

    flush_sh()
    return (pts_t, pts_se, pts_sub, shutter_segs, rate_markers,
            goto_regions, unk_regions, bep_regions)


COLUMN_GUIDE_HTML = (
    "<table style='font-size:11px;color:#cdd6f4;border-collapse:collapse;'>"
    "<tr style='color:#bd93f9;font-weight:bold;'>"
    "<td style='padding:0 12px 4px 0'>Column</td>"
    "<td style='padding:0 12px 4px 0'>Role</td>"
    "<td style='padding:0 0 4px 0'>Examples / notes</td></tr>"
    "<tr><td><b>Step</b></td><td>Row number (auto-renumbered on reorder)</td><td>1, 2, 3 ...</td></tr>"
    "<tr style='background:#1e1e2e;'><td><b>Action</b></td><td>Command to execute</td>"
    "<td>SET_TEMP, INCREASE_TEMP, WAIT_TEMP, RAMP_TEMP, OPEN_SHUTTER, CLOSE_SHUTTER,<br>"
    "HEATER_ON/OFF, IG_ON/OFF, WAIT_TIME, WAIT_PRESSURE, MEASURE_BEP, RHEED_VIDEO, GOTO, END</td></tr>"
    "<tr><td><b>Param 1</b></td><td>Primary argument (source / channel / value)</td>"
    "<td>SET_TEMP -> <i>Se</i> or <i>Sub</i>&nbsp;&nbsp;OPEN_SHUTTER -> <i>In / Se / Both</i><br>"
    "MEASURE_BEP -> <i>In / Se / Both</i>&nbsp;&nbsp;"
    "WAIT_TIME -> seconds&nbsp;&nbsp;GOTO -> target step number<br>"
    "<b>RHEED_VIDEO -> clip seconds</b> (0 = still image) - uses the controller's RHEED tab camera; "
    "numbered clips + .txt conditions in rheed/&lt;run&gt;/</td></tr>"
    "<tr style='background:#1e1e2e;'><td><b>Param 2</b></td><td>Secondary value (temperature / rate / loop count)</td>"
    "<td>SET_TEMP -> setpoint C&nbsp;&nbsp;WAIT_TEMP -> tolerance C (default +-2.5)<br>"
    "WAIT_PRESSURE -> Torr threshold&nbsp;&nbsp;GOTO -> extra loops&nbsp;&nbsp;"
    "<b>RAMP_TEMP -> C/min</b> (sets device internal ramp rate)<br>"
    "<b>INCREASE_TEMP -> delta C</b> (setpoint step, negative steps down)&nbsp;&nbsp;"
    "MEASURE_BEP -> (unused)</td></tr>"
    "<tr><td colspan='3' style='padding-top:6px;color:#8be9fd;'>"
    "<b>Flux calibration pattern:</b> INCREASE_TEMP -> WAIT_TEMP -> MEASURE_BEP -> GOTO "
    "gives a temperature-vs-flux sweep; each measurement is saved to its own "
    "<i>bep_measure_rec_#</i> sheet in the run log.</td></tr>"
    "<tr style='background:#1e1e2e;'><td><b>Notes</b></td><td>Free text - for your own reference</td><td></td></tr>"
    "</table>"
)


class RecipeEditor(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Recipe Editor — Vacuum Deposition Control")
        self.resize(1050, 780)
        self.setMinimumSize(760, 520)
        self._path = None
        self._dirty = False
        self._loading = False
        self._engine = RecipeEngine()
        self._build()
        self._rtab_add()
        self._set_dirty(False)

    # ── UI construction ──────────────────────────────────────────────
    def _build(self):
        c = QWidget(); self.setCentralWidget(c)
        v = QVBoxLayout(c); v.setContentsMargins(8, 6, 8, 6); v.setSpacing(6)

        tb = QHBoxLayout()
        self.b_new  = QPushButton("🆕 New")
        self.b_load = QPushButton("📂 Open…")
        self.b_save = QPushButton("💾 Save")
        self.b_save_as = QPushButton("💾 Save As…")
        self.b_add  = QPushButton("+ Row")
        self.b_del  = QPushButton("✕ Del Row")
        self.b_up   = QPushButton("▲ Up")
        self.b_down = QPushButton("▼ Down")
        for b in [self.b_new, self.b_load, self.b_save, self.b_save_as,
                  self.b_add, self.b_del, self.b_up, self.b_down]:
            b.setStyleSheet(
                f"QPushButton{{background:{PBG};color:{FG};border:1px solid {BDR};"
                f"border-radius:4px;padding:4px 10px;}}"
                f"QPushButton:hover{{background:#3a3a5e;}}"
            )
        self.b_new.clicked.connect(self._new_recipe)
        self.b_load.clicked.connect(self._load_recipe)
        self.b_save.clicked.connect(self._save_recipe)
        self.b_save_as.clicked.connect(self._save_recipe_as)
        self.b_add.clicked.connect(lambda: (self._rtab_add(), self._set_dirty(True)))
        self.b_del.clicked.connect(self._rtab_del)
        self.b_up.clicked.connect(lambda: self._move_selected(-1))
        self.b_down.clicked.connect(lambda: self._move_selected(1))

        self.rf = QLabel("New recipe (unsaved)")
        self.rf.setStyleSheet(f"color:{DIM};font-size:10px;")

        tb.addWidget(self.b_new); tb.addWidget(self.b_load)
        tb.addWidget(self.b_save); tb.addWidget(self.b_save_as)
        tb.addSpacing(10); tb.addWidget(self.rf); tb.addStretch(1)
        tb.addWidget(self.b_up); tb.addWidget(self.b_down)
        tb.addSpacing(8); tb.addWidget(self.b_add); tb.addWidget(self.b_del)
        v.addLayout(tb)

        # ── Collapsible column guide ────────────────────────────────
        self._help_visible = False
        help_toggle = QPushButton("▶  Column Guide")
        help_toggle.setStyleSheet(
            f"QPushButton{{background:transparent;color:{DIM};border:none;"
            f"font-size:10px;text-align:left;padding:1px 4px;}}"
            f"QPushButton:hover{{color:{FG};}}"
        )
        self._help_frame = QFrame(); self._help_frame.setVisible(False)
        self._help_frame.setStyleSheet(f"QFrame{{background:{PBG};border:1px solid {BDR};border-radius:4px;}}")
        hfl = QVBoxLayout(self._help_frame); hfl.setContentsMargins(8, 6, 8, 6)
        help_lbl = QLabel(COLUMN_GUIDE_HTML); help_lbl.setTextFormat(Qt.RichText); help_lbl.setWordWrap(True)
        hfl.addWidget(help_lbl)

        def _toggle_help():
            self._help_visible = not self._help_visible
            self._help_frame.setVisible(self._help_visible)
            help_toggle.setText("▼  Column Guide" if self._help_visible else "▶  Column Guide")

        help_toggle.clicked.connect(_toggle_help)
        v.addWidget(help_toggle)
        v.addWidget(self._help_frame)

        # ── Recipe table ─────────────────────────────────────────────
        self.rtab = RecipeTable(); self.rtab.setColumnCount(5)
        self.rtab.setHorizontalHeaderLabels(
            ["Step", "Action", "Param 1  (source/value)", "Param 2  (°C / rate / loops)", "Notes"])
        self.rtab.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.rtab.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.rtab.horizontalHeader().setSectionResizeMode(2, QHeaderView.Interactive)
        self.rtab.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.rtab.setColumnWidth(2, 180); self.rtab.setColumnWidth(3, 110)
        self.rtab.setSelectionBehavior(QTableWidget.SelectRows)
        vh = self.rtab.verticalHeader(); vh.setDefaultSectionSize(24); vh.setFixedWidth(18)
        vh.setStyleSheet(
            f"QHeaderView::section{{background:{PBG};color:{DIM};border:none;"
            f"border-right:1px solid {BDR};font-size:9px;padding:0;}}"
        )
        self.rtab.setStyleSheet(
            f"QTableWidget{{background:{BG};color:{FG};gridline-color:{BDR};font-size:12px;}}"
            f" QTableWidget::item{{background:{BG};color:{FG};}}"
            f" QTableWidget::item:selected{{background:#3d2e00;color:#f1fa8c;}}"
            f" QHeaderView::section{{background:{PBG};color:{DIM};border:1px solid {BDR};padding:4px;}}"
        )
        self.rtab.row_moved.connect(self._on_row_moved)
        v.addWidget(self.rtab, stretch=3)

        # ── Recipe preview plot (estimated timeline) ────────────────
        self._rp_plot = None; self._rp_items = []
        if HAS_PG:
            rp_frame = QFrame()
            rp_frame.setStyleSheet(f"QFrame{{background:{PBG};border:1px solid {BDR};border-radius:4px;}}")
            rp_fl = QVBoxLayout(rp_frame); rp_fl.setContentsMargins(4, 2, 4, 4); rp_fl.setSpacing(2)
            rp_lbl = QLabel("Recipe Preview  (estimated timeline — RAMP_TEMP sets device rate, SET_TEMP moves to target)")
            rp_lbl.setStyleSheet(f"color:{DIM};font-size:10px;")
            rp_fl.addWidget(rp_lbl)
            self._rp_plot = pg.PlotWidget(background=BG)
            self._rp_plot.showGrid(x=True, y=True, alpha=0.2)
            self._rp_plot.setLabel("left", "Temp", units="°C")
            self._rp_plot.setLabel("bottom", "Est. Time", units="s")
            self._rp_plot.setMaximumHeight(230)
            self._rp_plot.addLegend(offset=(10, 10))
            self._rp_se = self._rp_plot.plot(pen=pg.mkPen(OR, width=2), name="Se (CH1)")
            self._rp_sub = self._rp_plot.plot(pen=pg.mkPen(PU, width=2), name="Sub (CH2)")
            rp_fl.addWidget(self._rp_plot)
            v.addWidget(rp_frame, stretch=2)

        self._rp_timer = QTimer(); self._rp_timer.setSingleShot(True); self._rp_timer.setInterval(400)
        self._rp_timer.timeout.connect(self._refresh_recipe_preview)
        self.rtab.itemChanged.connect(self._on_table_changed)

        self.statusBar().showMessage("Ready")

    # ── Dirty-state / title tracking ──────────────────────────────────
    def _on_table_changed(self, *_):
        if self._loading:
            return
        self._set_dirty(True)
        self._rp_timer.start()

    def _set_dirty(self, d):
        self._dirty = d
        name = os.path.basename(self._path) if self._path else "New recipe (unsaved)"
        self.setWindowTitle(f"Recipe Editor — {name}{' *' if d else ''}")

    # ── Action combobox / row helpers (mirrors gui_app.py) ────────────
    def _action_cb_style(self):
        return f"QComboBox{{background:{PBG};color:{FG};border:1px solid {BDR};border-radius:3px;padding:1px 4px;}}"

    def _make_action_cb(self, action="WAIT_TIME"):
        cb = QComboBox()
        items = list(ALL_ACTIONS)
        if action not in items:
            items = [action] + items
        cb.addItems(items)
        i = cb.findText(action); cb.setCurrentIndex(i if i >= 0 else 0)
        cb.setStyleSheet(self._action_cb_style())
        return cb

    def _make_param1_widget(self, action, value=""):
        if action in ACTION_CH_OPTS:
            cb = QComboBox(); cb.addItems(ACTION_CH_OPTS[action]); cb.setStyleSheet(self._action_cb_style())
            i = cb.findText(str(value)); cb.setCurrentIndex(i if i >= 0 else 0)
            return cb
        if action in ACTION_SHUTTER_OPTS:
            cb = QComboBox(); cb.addItems(ACTION_SHUTTER_OPTS[action]); cb.setStyleSheet(self._action_cb_style())
            i = cb.findText(str(value)); cb.setCurrentIndex(i if i >= 0 else 0)
            return cb
        return None

    def _rtab_add(self, step=None, action="WAIT_TIME", p1="", p2="", notes=""):
        r = self.rtab.rowCount(); self.rtab.insertRow(r)
        self.rtab.setItem(r, 0, QTableWidgetItem(str(step or r + 1)))
        acb = self._make_action_cb(action)
        acb.currentTextChanged.connect(lambda t, row=r: self._on_action_changed(row, t))
        acb.currentTextChanged.connect(lambda _=None: (self._set_dirty(True), self._rp_timer.start()))
        self.rtab.setCellWidget(r, 1, acb)
        w1 = self._make_param1_widget(action, p1)
        if w1:
            self.rtab.setCellWidget(r, 2, w1)
            w1.currentTextChanged.connect(lambda _=None: (self._set_dirty(True), self._rp_timer.start()))
        else:
            self.rtab.setItem(r, 2, QTableWidgetItem("" if p1 is None else str(p1)))
        self.rtab.setItem(r, 3, QTableWidgetItem("" if p2 is None else str(p2)))
        self.rtab.setItem(r, 4, QTableWidgetItem(str(notes) if notes else ""))

    def _on_action_changed(self, row, action):
        old_val = ""
        old_w = self.rtab.cellWidget(row, 2)
        if old_w and hasattr(old_w, "currentText"): old_val = old_w.currentText()
        elif self.rtab.item(row, 2): old_val = self.rtab.item(row, 2).text()
        w1 = self._make_param1_widget(action, old_val)
        if w1:
            self.rtab.setCellWidget(row, 2, w1); self.rtab.setItem(row, 2, None)
            w1.currentTextChanged.connect(lambda _=None: (self._set_dirty(True), self._rp_timer.start()))
        else:
            self.rtab.removeCellWidget(row, 2)
            self.rtab.setItem(row, 2, QTableWidgetItem(old_val if not any(c.isalpha() for c in old_val) else ""))
        self._set_dirty(True)
        self._rp_timer.start()

    def _rtab_del(self):
        rows = sorted({i.row() for i in self.rtab.selectedItems()}, reverse=True)
        if not rows:
            return
        for r in rows: self.rtab.removeRow(r)
        for r in range(self.rtab.rowCount()): self.rtab.setItem(r, 0, QTableWidgetItem(str(r + 1)))
        self._set_dirty(True)
        self._rp_timer.start()

    def _move_selected(self, delta):
        """delta=-1 moves the selected row up one place, +1 moves it down one.

        _on_row_moved uses drag-drop "insert before" semantics (dst is an index
        into the list BEFORE the source row is removed), so moving down by one
        row requires dst = src + 2, not src + 1 — dropping "after" the next row.
        """
        rows = sorted({i.row() for i in self.rtab.selectedItems()})
        if len(rows) != 1:
            return
        src = rows[0]
        if delta < 0:
            dst = src - 1
            if dst < 0:
                return
        else:
            dst = src + 2
            if dst > self.rtab.rowCount():
                return
        self._on_row_moved(src, dst)
        self.rtab.selectRow(src + delta)

    def _on_row_moved(self, src, dst):
        steps = self._rtab_to_steps()
        if src < 0 or src >= len(steps):
            return
        insert_at = dst if src >= dst else dst - 1
        insert_at = max(0, min(insert_at, len(steps) - 1))
        step = steps.pop(src)
        steps.insert(insert_at, step)
        for i, s in enumerate(steps): s.step = i + 1
        self._rtab_populate(steps)
        self.rtab.selectRow(insert_at)
        self._set_dirty(True)

    def _rtab_populate(self, steps):
        self._loading = True
        self.rtab.setRowCount(0)
        for s in steps:
            self._rtab_add(s.step, s.action,
                           s.param1 if s.param1 is not None else "",
                           s.param2 if s.param2 is not None else "",
                           s.notes)
        self._loading = False
        self._rp_timer.start()

    def _rtab_to_steps(self):
        out = []
        for r in range(self.rtab.rowCount()):
            acb = self.rtab.cellWidget(r, 1); act = acb.currentText() if acb else ""

            def _g(c):
                w = self.rtab.cellWidget(r, c)
                if w and hasattr(w, "currentText"): return w.currentText()
                item = self.rtab.item(r, c)
                return item.text().strip() if item else ""

            p1s = _g(2); p2s = _g(3)
            if p1s == "None": p1s = ""
            if p2s == "None": p2s = ""
            p1 = p1s if (p1s and any(c.isalpha() for c in p1s)) else (float(p1s) if p1s else None)
            try: p2 = float(p2s) if p2s else None
            except Exception: p2 = p2s if p2s else None
            out.append(types.SimpleNamespace(step=r + 1, action=act, param1=p1, param2=p2, param3=None, notes=_g(4)))
        return out

    # ── File operations ────────────────────────────────────────────────
    def _confirm_discard(self):
        if not self._dirty:
            return True
        resp = QMessageBox.question(
            self, "Unsaved changes",
            "This recipe has unsaved changes. Save before continuing?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save,
        )
        if resp == QMessageBox.Save:
            return self._save_recipe()
        return resp == QMessageBox.Discard

    def _new_recipe(self):
        if not self._confirm_discard():
            return
        self._rtab_populate([])
        self._rtab_add()
        self._path = None
        self._set_dirty(False)
        self.statusBar().showMessage("New recipe")

    def _load_recipe(self):
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open Recipe", "", "Excel Files (*.xlsx *.xls);;All Files (*)")
        if not path:
            return
        try:
            steps = self._engine.load_excel(path)
            self._rtab_populate(steps)
            self._path = path
            self._set_dirty(False)
            self.rf.setText(os.path.basename(path))
            self.statusBar().showMessage(f"Loaded {os.path.basename(path)} ({len(steps)} steps)")
        except Exception as e:
            QMessageBox.critical(self, "Load Error", str(e))

    def _do_save(self, path):
        import openpyxl
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Recipe"
        ws.append(["Step", "Action", "Param1", "Param2", "Notes"])
        for s in self._rtab_to_steps():
            ws.append([s.step, s.action, s.param1, s.param2, s.notes])
        wb.save(path)

    def _save_recipe(self):
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            QMessageBox.critical(self, "Error", "pip install openpyxl"); return False
        if not self._path:
            return self._save_recipe_as()
        try:
            self._do_save(self._path)
        except Exception as e:
            QMessageBox.critical(self, "Save Error", str(e)); return False
        self.rf.setText(os.path.basename(self._path))
        self._set_dirty(False)
        self.statusBar().showMessage(f"Recipe saved → {self._path}")
        return True

    def _save_recipe_as(self):
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            QMessageBox.critical(self, "Error", "pip install openpyxl"); return False
        default = self._path or "recipe.xlsx"
        path, _ = QFileDialog.getSaveFileName(self, "Save Recipe As", default, "Excel Files (*.xlsx)")
        if not path:
            return False
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        try:
            self._do_save(path)
        except Exception as e:
            QMessageBox.critical(self, "Save Error", str(e)); return False
        self._path = path
        self.rf.setText(os.path.basename(path))
        self._set_dirty(False)
        self.statusBar().showMessage(f"Recipe saved → {path}")
        return True

    def closeEvent(self, event):
        if self._confirm_discard():
            event.accept()
        else:
            event.ignore()

    # ── Recipe preview plot ─────────────────────────────────────────────
    def _refresh_recipe_preview(self):
        if not HAS_PG or self._rp_plot is None:
            return
        steps = self._rtab_to_steps()
        for item in self._rp_items:
            try: self._rp_plot.removeItem(item)
            except Exception: pass
        self._rp_items.clear()

        if not steps:
            self._rp_se.setData([], []); self._rp_sub.setData([], [])
            return

        pts_t, pts_se, pts_sub, shutters, rmarks, gotos, unks, beps = simulate_recipe_timeline(steps)

        self._rp_se.setData(pts_t, pts_se)
        self._rp_sub.setData(pts_t, pts_sub)

        y_top = max(max(pts_se, default=0), max(pts_sub, default=0)) or 100.0

        for t0, t1, in_s, se_s in shutters:
            if not (in_s or se_s) or t1 <= t0: continue
            if in_s and se_s: col = BL          # both sources open: blue
            elif in_s:        col = SRC_IN_C    # In only: green
            else:             col = SRC_SE_C    # Se only: orange
            bc = pg.mkColor(col); bc.setAlpha(50)
            rgn = pg.LinearRegionItem((t0, t1), movable=False, brush=pg.mkBrush(bc))
            ec = pg.mkColor(col); ec.setAlpha(160)
            for ln in rgn.lines: ln.setPen(pg.mkPen(ec, width=1))
            self._rp_plot.addItem(rgn); self._rp_items.append(rgn)

        for t0, t1 in unks:
            if t1 <= t0: continue
            bc = pg.mkColor("#888888"); bc.setAlpha(22)
            rgn = pg.LinearRegionItem((t0, t1), movable=False, brush=pg.mkBrush(bc))
            for ln in rgn.lines: ln.setPen(pg.mkPen("#888888", width=1, style=Qt.DotLine))
            self._rp_plot.addItem(rgn); self._rp_items.append(rgn)

        # ── BEP measurement windows (silver, labelled per source) ────
        for t0, t1, src in beps:
            if t1 <= t0: continue
            bc = pg.mkColor(BEP_C); bc.setAlpha(70)
            rgn = pg.LinearRegionItem((t0, t1), movable=False, brush=pg.mkBrush(bc))
            for ln in rgn.lines: ln.setPen(pg.mkPen("#e8e8e8", width=1))
            self._rp_plot.addItem(rgn); self._rp_items.append(rgn)
            s = str(src or "Both").strip().lower()
            want_in = s.startswith("in") or "both" in s
            want_se = s.startswith("se") or "both" in s
            tags = ([("In", SRC_IN_C)] if want_in else []) + ([("Se", SRC_SE_C)] if want_se else [])
            for k, (txt, col) in enumerate(tags):
                ti = pg.TextItem(txt, color=col, anchor=(0.5, 0.5))
                ti.setFont(QFont("Arial", 9, QFont.Bold))
                ti.setPos((t0 + t1)/2, y_top*(0.55 - 0.09*k))
                self._rp_plot.addItem(ti); self._rp_items.append(ti)

        for t0, t1, label in gotos:
            if t1 <= t0: continue
            bc = pg.mkColor("#ffffff"); bc.setAlpha(18)
            rgn = pg.LinearRegionItem((t0, t1), movable=False, brush=pg.mkBrush(bc))
            for ln in rgn.lines: ln.setPen(pg.mkPen("#cccccc", width=1.5, style=Qt.DashLine))
            self._rp_plot.addItem(rgn); self._rp_items.append(rgn)
            txt = pg.TextItem(label, color="#cccccc", anchor=(0.5, 0.5))
            txt.setFont(QFont("Arial", 11, QFont.Bold))
            txt.setPos((t0 + t1) / 2, y_top * 0.85)
            self._rp_plot.addItem(txt); self._rp_items.append(txt)

        for t_m, rate, ch in rmarks:
            col = OR if ch == 1 else PU
            ln = pg.InfiniteLine(t_m, angle=90, pen=pg.mkPen(col, width=1, style=Qt.DashLine))
            self._rp_plot.addItem(ln); self._rp_items.append(ln)
            txt = pg.TextItem(f" {rate:.0f}°/m", color=col, anchor=(0, 1))
            txt.setFont(QFont("Arial", 8))
            txt.setPos(t_m, y_top)
            self._rp_plot.addItem(txt); self._rp_items.append(txt)

        self._rp_plot.autoRange()


def main():
    app = QApplication(sys.argv)
    apply_dark_theme(app)
    wheel_guard.install(app)          # mouse wheel never changes spin boxes / dropdowns / sliders
    win = RecipeEditor()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
