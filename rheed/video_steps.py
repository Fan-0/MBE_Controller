"""
Steps ② and ③ of the 🎞 Video analysis panel (step ① lives in video_tab.py):

  SpatialStep  ② Spatial analysis — azimuth-locked analysis of the per-frame line
               profiles (spatial.py): choose the azimuth (a kept direction or the
               azimuth cursor), the profile band (green box, re-extract), find the
               streak peaks, pick which peak is (00) / which one to follow, streak
               spacing Δ, intensity vs time (azimuth-locked / all frames / rotation-
               normalised) + FFT oscillation period, calibration px→Å and
               identification against materials.csv + an online COD lookup.
               Plots it owns are added to the panel's bottom result tabs:
               📏 Profile & peaks · 🕸 Kymograph · 📈 Intensity vs time · 〰 FFT.
  ExportStep   ③ Export — base folder + typed folder name + what to write; nothing is
               written anywhere else (export.export_analysis).

Everything recomputes from memory (no decoding) except "Re-extract profiles", which
re-reads the video on a worker thread (ProfileExtraction). The COD lookup runs on a
plain thread; both report back through pyqtSignals (main-thread rule, CONTEXT §12).
"""

import math
import os
import subprocess
import sys
import threading
from typing import Optional

import numpy as np
from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox,
                             QFileDialog, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
                             QLineEdit, QMessageBox, QSpinBox, QStackedWidget,
                             QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

try:
    import pyqtgraph as pg
    import pyqtgraph.exporters  # noqa: F401  (ImageExporter for plot screenshots)
    HAS_PG = True
except ImportError:
    HAS_PG = False

from . import spatial as S
from .export import default_folder_name, export_analysis, safe_name
from .ui_common import BG, PBG, BDR, BL, GR, RD, OR, PU, FG, DIM, YL, _btn, _group
from .video_analysis import ProfileExtraction

ORDER_COLORS = [YL, BL, GR, OR, PU, "#8be9fd", "#ff79c6"]
INT_MODES = ["Azimuth-locked (1 point / revolution)", "All frames",
             "All frames ÷ rotation (normalised)"]


def _spin(lo, hi, val, dec=1, step=None, tip=""):
    s = QDoubleSpinBox(); s.setRange(lo, hi); s.setDecimals(dec); s.setValue(val)
    if step:
        s.setSingleStep(step)
    if tip:
        s.setToolTip(tip)
    return s


def _table(cols):
    t = QTableWidget(0, len(cols)); t.setHorizontalHeaderLabels(cols)
    t.verticalHeader().setVisible(False)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)   # fit the column, no h-scroll
    t.horizontalHeader().setMinimumSectionSize(30)
    t.setStyleSheet(f"QTableWidget{{background:{BG};color:{FG};gridline-color:{BDR};}}"
                    f"QHeaderView::section{{background:{PBG};color:{DIM};border:none;}}")
    return t


class _Bridge(QObject):
    prof_progress = pyqtSignal(float, str)
    prof_done = pyqtSignal(object, str)
    cod_done = pyqtSignal(object, str)


# ══════════════════════════════════════════════════════════════════════
# ② Spatial analysis
# ══════════════════════════════════════════════════════════════════════

class SpatialStep(QWidget):
    def __init__(self, panel):
        super().__init__()
        self.p = panel
        self.series: Optional[S.ProfileSeries] = None
        self.materials = S.load_materials()
        self.cod_materials = []           # this session's online results
        self._ref_pos = None              # chosen (00) peak position (px)
        self._sel_pos = None              # peak followed for intensity (px)
        self._delta0 = None               # user spacing seed (px)
        self._state = {}                  # last computed results (for export / plots)
        self._overlay = []                # peak lines drawn on the viewer
        self._kymo_lines = []             # followed-peak window on the kymograph
        self.pe = ProfileExtraction()
        self._b = _Bridge()
        self.pe.on_progress = lambda f, m: self._b.prof_progress.emit(f, m)
        self.pe.on_done = lambda s, m: self._b.prof_done.emit(s, m)
        self._b.prof_progress.connect(lambda f, m: self.band_lbl.setText(f"{m}  ({f * 100:.0f} %)"))
        self._b.prof_done.connect(self._on_profiles)
        self._b.cod_done.connect(self._on_cod)
        self._recalc_t = QTimer(self); self._recalc_t.setSingleShot(True)
        self._recalc_t.timeout.connect(self.recompute)
        self._build()
        self._build_plots()
        self.setEnabled(False)

    # ── UI ────────────────────────────────────────────────────────────
    def _build(self):
        v = QVBoxLayout(self); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(8)
        self.hint_lbl = QLabel("Run ① Analyse first — this step works on its results.")
        self.hint_lbl.setWordWrap(True); self.hint_lbl.setStyleSheet(f"color:{DIM};")
        v.addWidget(self.hint_lbl)

        g = _group("Azimuth"); gl = QGridLayout(g)
        gl.addWidget(QLabel("Direction:"), 0, 0)
        self.az_cb = QComboBox(); self.az_cb.currentIndexChanged.connect(self._changed)
        self.az_cb.setToolTip("Which kept azimuth (peak of 📈 Quality vs azimuth) to analyse —\n"
                              "or the yellow cursor's azimuth (drag it on 📈 Quality vs azimuth).")
        gl.addWidget(self.az_cb, 0, 1)
        gl.addWidget(QLabel("± (°):"), 1, 0)
        self.tol_sp = _spin(0.1, 20, 1.0, tip="Frames within ± this of the azimuth (every revolution) are used")
        self.tol_sp.valueChanged.connect(self._changed)
        gl.addWidget(self.tol_sp, 1, 1)
        self.az_lbl = QLabel(""); self.az_lbl.setStyleSheet(f"color:{DIM};")
        gl.addWidget(self.az_lbl, 2, 0, 1, 2)
        gl.setColumnStretch(1, 1)
        v.addWidget(g)

        g = _group("Profile band"); gl = QGridLayout(g)
        self.band_chk = QCheckBox("Own band (green box on the image)")
        self.band_chk.setToolTip("Profiles average ALONG the streaks inside this box. A narrow box around\n"
                                 "the streaks (excluding glow / shadow) gives sharper peaks.\n"
                                 "Default = the analysis ROI (or the whole frame).")
        self.band_chk.toggled.connect(self._band_toggled)
        gl.addWidget(self.band_chk, 0, 0, 1, 2)
        self.reext_btn = _btn("▶ Re-extract profiles", BL); self.reext_btn.clicked.connect(self._reextract)
        gl.addWidget(self.reext_btn, 1, 0, 1, 2)
        self.band_lbl = QLabel(""); self.band_lbl.setWordWrap(True); self.band_lbl.setStyleSheet(f"color:{DIM};")
        gl.addWidget(self.band_lbl, 2, 0, 1, 2)
        v.addWidget(g)

        g = _group("Streak peaks"); gl = QGridLayout(g); r = 0
        gl.addWidget(QLabel("Background width (px):"), r, 0)
        self.bg_sp = QSpinBox(); self.bg_sp.setRange(10, 1000); self.bg_sp.setValue(100)
        self.bg_sp.setToolTip("Window of the broad background under the streaks; peaks wider than\n"
                              "this are treated as diffuse glow and ignored.")
        self.bg_sp.valueChanged.connect(self._changed); gl.addWidget(self.bg_sp, r, 1); r += 1
        gl.addWidget(QLabel("Threshold (σ):"), r, 0)
        self.sig_sp = _spin(1, 100, 5.0); self.sig_sp.valueChanged.connect(self._changed)
        gl.addWidget(self.sig_sp, r, 1); r += 1
        gl.addWidget(QLabel("Min. separation (px):"), r, 0)
        self.md_sp = QSpinBox(); self.md_sp.setRange(2, 500); self.md_sp.setValue(20)
        self.md_sp.valueChanged.connect(self._changed); gl.addWidget(self.md_sp, r, 1); r += 1
        self.peak_tbl = _table(["pos px", "order", "height", "FWHM", "SNR"])
        self.peak_tbl.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.peak_tbl.setMinimumHeight(140)
        self.peak_tbl.itemSelectionChanged.connect(self._peak_selected)
        self.peak_tbl.setToolTip("Click a peak to follow its intensity (also: click it on 📏 Profile).\n"
                                 "Select TWO peaks and press 'Seed Δ' if the automatic spacing is wrong.")
        gl.addWidget(self.peak_tbl, r, 0, 1, 2); r += 1
        h = QHBoxLayout()
        self.ref_btn = _btn("Set (00)", DIM); self.ref_btn.clicked.connect(self._set_ref)
        self.seed_btn = _btn("Seed Δ", DIM); self.seed_btn.clicked.connect(self._seed)
        self.seed_btn.setToolTip("Select TWO neighbouring peaks, then press: their distance seeds the spacing fit")
        self.ref_btn.setToolTip("Make the selected peak order 0 (e.g. the specular spot's streak)")
        self.auto_btn = _btn("Auto Δ", DIM); self.auto_btn.clicked.connect(self._auto_spacing)
        h.addWidget(self.ref_btn); h.addWidget(self.seed_btn); h.addWidget(self.auto_btn)
        gl.addLayout(h, r, 0, 1, 2); r += 1
        self.overlay_chk = QCheckBox("Show peaks on the image"); self.overlay_chk.setChecked(True)
        self.overlay_chk.toggled.connect(lambda _: self._draw_overlay())
        gl.addWidget(self.overlay_chk, r, 0, 1, 2); r += 1
        self.sp_lbl = QLabel("—"); self.sp_lbl.setWordWrap(True); self.sp_lbl.setTextFormat(Qt.RichText)
        gl.addWidget(self.sp_lbl, r, 0, 1, 2); r += 1
        gl.setColumnStretch(1, 1)
        v.addWidget(g)

        g = _group("Intensity vs time (selected peak)"); gl = QGridLayout(g); r = 0
        self.mode_cb = QComboBox(); self.mode_cb.addItems(INT_MODES)
        self.mode_cb.setToolTip("Azimuth-locked: one point per revolution at the chosen azimuth —\n"
                                "  the clean signal on a rotating substrate (≤ 1 sample / rev).\n"
                                "All frames: every frame — the RHEED-oscillation signal on a STATIC\n"
                                "  substrate; on a rotating one it is dominated by the rotation.\n"
                                "Normalised: all frames ÷ the mean azimuthal dependence → growth-only\n"
                                "  change (approximate; azimuths where the peak is weak are blanked).")
        self.mode_cb.currentIndexChanged.connect(self._changed)
        gl.addWidget(self.mode_cb, r, 0, 1, 2); r += 1
        gl.addWidget(QLabel("Window ± (px, 0=auto):"), r, 0)
        self.hw_sp = _spin(0, 200, 0, 0); self.hw_sp.valueChanged.connect(self._changed)
        gl.addWidget(self.hw_sp, r, 1); r += 1
        gl.addWidget(QLabel("FFT period range (s):"), r, 0)
        h = QHBoxLayout()
        self.pmin_sp = _spin(0.1, 1000, 1.0); self.pmax_sp = _spin(0.2, 5000, 60.0)
        for s_ in (self.pmin_sp, self.pmax_sp):
            s_.valueChanged.connect(self._changed); h.addWidget(s_)
        gl.addLayout(h, r, 1); r += 1
        self.int_lbl = QLabel("—"); self.int_lbl.setWordWrap(True); self.int_lbl.setTextFormat(Qt.RichText)
        gl.addWidget(self.int_lbl, r, 0, 1, 2); r += 1
        gl.setColumnStretch(1, 1)
        v.addWidget(g)

        g = _group("Calibration  px → Å"); gl = QGridLayout(g); r = 0
        self.cal_cb = QComboBox(); self.cal_cb.addItems(["Reference spacing (known surface)", "Beam energy + geometry"])
        self.cal_cb.currentIndexChanged.connect(self._cal_mode)
        gl.addWidget(self.cal_cb, r, 0, 1, 2); r += 1
        self.cal_stack = QStackedWidget()
        w1 = QWidget(); g1 = QGridLayout(w1); g1.setContentsMargins(0, 0, 0, 0)
        g1.addWidget(QLabel("Known row d (Å):"), 0, 0)
        self.dref_sp = _spin(0.1, 100, 4.121, 4, tip="Row spacing of the reference surface at this azimuth")
        self.dref_sp.valueChanged.connect(self._changed); g1.addWidget(self.dref_sp, 0, 1)
        self.ref_mat_cb = QComboBox(); self.ref_mat_cb.setToolTip("Fill d from the materials table")
        self.ref_mat_cb.activated.connect(self._ref_material)
        g1.addWidget(self.ref_mat_cb, 1, 0, 1, 2)
        g1.addWidget(QLabel("Measured Δ (px):"), 2, 0)
        self.dpx_sp = _spin(0, 5000, 0, 2); self.dpx_sp.valueChanged.connect(self._changed)
        g1.addWidget(self.dpx_sp, 2, 1)
        self.use_btn = _btn("Use current Δ", DIM); self.use_btn.clicked.connect(self._use_current)
        g1.addWidget(self.use_btn, 3, 0, 1, 2)
        self.cal_stack.addWidget(w1)
        w2 = QWidget(); g2 = QGridLayout(w2); g2.setContentsMargins(0, 0, 0, 0)
        g2.addWidget(QLabel("Beam energy (keV):"), 0, 0)
        self.kev_sp = _spin(1, 100, 15, 2); g2.addWidget(self.kev_sp, 0, 1)
        g2.addWidget(QLabel("Sample→screen (mm):"), 1, 0)
        self.L_sp = _spin(1, 2000, 300, 1); g2.addWidget(self.L_sp, 1, 1)
        g2.addWidget(QLabel("Screen scale (mm/px):"), 2, 0)
        self.mmpx_sp = _spin(0.0001, 10, 0.1, 4, tip="e.g. screen diameter (mm) / its diameter in the image (px)")
        g2.addWidget(self.mmpx_sp, 2, 1)
        for s_ in (self.kev_sp, self.L_sp, self.mmpx_sp):
            s_.valueChanged.connect(self._changed)
        self.cal_stack.addWidget(w2)
        gl.addWidget(self.cal_stack, r, 0, 1, 2); r += 1
        self.cal_lbl = QLabel("not calibrated — spacing in px only"); self.cal_lbl.setWordWrap(True)
        self.cal_lbl.setStyleSheet(f"color:{DIM};")
        gl.addWidget(self.cal_lbl, r, 0, 1, 2); r += 1
        gl.setColumnStretch(1, 1)
        v.addWidget(g)

        g = _group("Identify"); gl = QGridLayout(g); r = 0
        gl.addWidget(QLabel("Tolerance (%):"), r, 0)
        self.tolid_sp = _spin(0.5, 30, 5.0); self.tolid_sp.valueChanged.connect(self._changed)
        gl.addWidget(self.tolid_sp, r, 1); r += 1
        self.cand_tbl = _table(["err %", "material", "surface", "beam ∥", "d Å"])
        self.cand_tbl.setMinimumHeight(140)
        gl.addWidget(self.cand_tbl, r, 0, 1, 2); r += 1
        h = QHBoxLayout()
        self.cod_ed = QLineEdit(); self.cod_ed.setPlaceholderText("formula, e.g. In2Se3")
        self.cod_btn = _btn("🔎 COD", BL); self.cod_btn.clicked.connect(self._cod_search)
        self.cod_btn.setToolTip("Look the formula up in the Crystallography Open Database\n"
                                "(crystallography.net, no account) and add its surface nets\n"
                                "to the candidates for this session.")
        self.cod_ed.returnPressed.connect(self._cod_search)
        h.addWidget(self.cod_ed, 1); h.addWidget(self.cod_btn)
        gl.addLayout(h, r, 0, 1, 2); r += 1
        self.id_lbl = QLabel(""); self.id_lbl.setWordWrap(True); self.id_lbl.setTextFormat(Qt.RichText)
        gl.addWidget(self.id_lbl, r, 0, 1, 2); r += 1
        gl.setColumnStretch(1, 1)
        v.addWidget(g)
        v.addStretch(1)
        self._fill_ref_materials()

    def _build_plots(self):
        if not HAS_PG:
            return
        tabs = self.p.res_tabs
        self.prof_plot = pg.PlotWidget(); self.prof_plot.showGrid(x=True, y=True, alpha=0.25)
        self.prof_plot.setLabel("left", "Grey level"); self.prof_plot.addLegend(offset=(10, 10))
        self.prof_plot.scene().sigMouseClicked.connect(self._prof_clicked)
        tabs.addTab(self.prof_plot, "📏 Profile & peaks")
        self.kymo_plot = pg.PlotWidget(); self.kymo_plot.setLabel("bottom", "Video time (s)")
        self.kymo_img = pg.ImageItem(axisOrder="row-major")
        self.kymo_plot.addItem(self.kymo_img); self.kymo_plot.invertY(True)
        self.kymo_line = pg.InfiniteLine(0, angle=90, movable=True, pen=pg.mkPen(YL, width=2),
                                         hoverPen=pg.mkPen(YL, width=5))
        self.kymo_line.setZValue(20)
        self.kymo_line.sigDragged.connect(lambda ln: self.p._cursor_seek(ln.value()))
        tabs.addTab(self.kymo_plot, "🕸 Kymograph")
        self.int_plot = pg.PlotWidget(); self.int_plot.showGrid(x=True, y=True, alpha=0.25)
        self.int_plot.setLabel("bottom", "Video time (s)"); self.int_plot.setLabel("left", "Peak intensity (grey)")
        self.int_plot.addLegend(offset=(10, 10))
        self.int_line = pg.InfiniteLine(0, angle=90, movable=True, pen=pg.mkPen(YL, width=2),
                                        hoverPen=pg.mkPen(YL, width=5))
        self.int_line.setZValue(20)
        self.int_line.sigDragged.connect(lambda ln: self.p._cursor_seek(ln.value()))
        tabs.addTab(self.int_plot, "📈 Intensity vs time")
        self.fft_plot = pg.PlotWidget(); self.fft_plot.showGrid(x=True, y=True, alpha=0.25)
        self.fft_plot.setLabel("bottom", "Period (s)"); self.fft_plot.setLabel("left", "Power")
        self.fft_plot.setLogMode(x=True, y=False)
        tabs.addTab(self.fft_plot, "〰 FFT")
        self.p_orient = "horizontal"

    def plots(self):
        """(name, PlotWidget) pairs for the export screenshots."""
        return [("profile_peaks", self.prof_plot), ("kymograph", self.kymo_plot),
                ("intensity_vs_time", self.int_plot), ("fft", self.fft_plot)] if HAS_PG else []

    # ── data in ───────────────────────────────────────────────────────
    def set_summary(self, summary):
        """New ① analysis result (None = cleared)."""
        self._ref_pos = self._sel_pos = self._delta0 = None
        self._clear_overlay()
        if summary is None:
            self.series = None; self.setEnabled(False)
            self.hint_lbl.setText("Run ① Analyse first — this step works on its results.")
            return
        self.series = summary["profiles"]
        self.setEnabled(True)
        r = summary["result"]
        self.az_cb.blockSignals(True); self.az_cb.clear()
        for k, pk in enumerate(summary["picks"]):
            if pk["angle_deg"] is not None:
                self.az_cb.addItem(f"az{k + 1}  {pk['angle_deg']:.1f}°   q {pk['score']:.2f}", pk["angle_deg"])
        self.az_cb.addItem("Cursor azimuth (yellow line on 📈 Quality vs azimuth)", "cursor")
        # default: the best-scoring kept azimuth
        sc = [pk["score"] for pk in summary["picks"] if pk["angle_deg"] is not None]
        self.az_cb.setCurrentIndex(int(np.argmax(sc)) if sc else 0)
        self.az_cb.blockSignals(False)
        for w in (self.az_cb, self.tol_sp):
            w.setEnabled(r.period_s is not None)
        if r.period_s is None:
            self.hint_lbl.setText("No rotation found — the profile uses ALL frames (static substrate: "
                                  "'All frames' intensity is the RHEED-oscillation signal).")
            self.mode_cb.setCurrentIndex(1)
        else:
            self.hint_lbl.setText(f"T = {r.period_s:.3f} s. Pick an azimuth and a peak; everything below "
                                  "updates live from memory.")
        self.band_lbl.setText(f"band {self.series.band}  ·  {len(self.series.times)} profiles")
        self._kymo_set()
        self.recompute()

    def _summary(self):
        return self.p._summary

    def _T_t0(self):
        s = self._summary()
        return (s["result"].period_s, s["t0"]) if s else (None, 0.0)

    def _azimuth(self):
        d = self.az_cb.currentData()
        if d == "cursor":
            return float(self.p.az_line.value()) if HAS_PG else 0.0
        return float(d) if d is not None else 0.0

    def cursor_azimuth_moved(self):
        if self.az_cb.currentData() == "cursor":
            self._changed()

    def _changed(self, *_):
        self._recalc_t.start(80)

    # ── the computation ───────────────────────────────────────────────
    def recompute(self):
        ser = self.series
        if ser is None or not len(ser.times):
            return
        T, t0 = self._T_t0()
        az, tol = self._azimuth(), self.tol_sp.value()
        prof, n = S.azimuth_profile(ser, T, t0, az, tol)
        st = {"azimuth": az if T else None, "tol": tol, "n_frames": n}
        self.az_lbl.setText(f"{n} frames at {az:.1f}° ± {tol:g}°" if T else f"{n} frames (all)")
        if n == 0 or not np.isfinite(prof).all():
            self.sp_lbl.setText(f"<span style='color:{OR}'>no frames at this azimuth</span>")
            return
        peaks, hp, noise = S.find_peaks(prof, bg_width=self.bg_sp.value(), sigma_thresh=self.sig_sp.value(),
                                        min_dist=self.md_sp.value(), axis=ser.pos)
        st.update(profile=(ser.pos, prof, hp), peaks=peaks, noise=noise)
        ref = self._nearest(peaks, self._ref_pos)
        spc = S.fit_spacing(peaks, ref=ref, delta0=self._delta0)
        st["spacing"] = spc
        st["orders"] = spc.orders if spc else [None] * len(peaks)
        sel = self._nearest(peaks, self._sel_pos)
        if sel is None and peaks:
            sel = int(np.argmax([pk.height for pk in peaks]))
        st["sel"] = sel
        self._state = st
        self._fill_peaks(peaks, st["orders"], sel)
        self._spacing_text(spc, peaks, ref)
        self._intensity(st)
        self._calibrate_identify(st)
        self._plot_profile(st)
        self._draw_overlay()
        self._per_azimuth_spacing()

    @staticmethod
    def _nearest(peaks, pos):
        if pos is None or not peaks:
            return None
        d = [abs(pk.pos - pos) for pk in peaks]
        i = int(np.argmin(d))
        return i if d[i] <= max(10.0, 0.5 * peaks[i].fwhm) else None

    def _spacing_text(self, spc, peaks, ref):
        if spc is None:
            self.sp_lbl.setText(f"<span style='color:{OR}'>need ≥ 2 peaks for a spacing</span>")
            return
        col = GR if spc.n_used >= 3 else YL
        ref_i = ref if ref is not None else int(np.argmax([pk.height for pk in peaks]))
        txt = (f"<b style='color:{col}'>Δ = {spc.delta:.2f} ± {spc.err:.2f} px</b> &nbsp;({spc.n_used} orders"
               f"{', seed ' + format(self._delta0, '.1f') + ' px' if self._delta0 else ''})<br>"
               f"(00) at {peaks[ref_i].pos:.1f} px")
        if spc.half_orders:
            txt += f"<br><span style='color:{YL}'>⚠ {len(spc.half_orders)} peak(s) at half order — ×2 reconstruction?</span>"
        self.sp_lbl.setText(txt)

    def _intensity(self, st):
        ser = self.series
        T, t0 = self._T_t0()
        peaks, sel = st["peaks"], st["sel"]
        self._int_plot_clear()
        if sel is None:
            self.int_lbl.setText("—"); st["intensity"] = st["locked"] = None
            return
        pk = peaks[sel]
        hw = self.hw_sp.value() or max(3.0, pk.fwhm / 2)
        I = S.peak_intensity(ser, pk.pos, hw)
        az_all = ser.azimuths(T, t0) if T else None
        norm = None
        locked = None
        if T:
            locked = S.azimuth_locked(ser, I, T, t0, st["azimuth"], st["tol"])
            norm, _ = S.rotation_normalised(ser, I, T, t0)
        st["intensity"] = {"t": ser.times, "I": I, "az": az_all, "norm": norm, "pos": pk.pos, "half_w": hw}
        st["locked"] = locked
        mode = self.mode_cb.currentIndex()
        if mode == 0 and locked is not None and len(locked[0]):
            tt, yy = locked[0], locked[1]
        elif mode == 2 and norm is not None:
            tt, yy = ser.times, norm
        else:
            tt, yy = ser.times, I
        per, f, pw = S.oscillation_period(tt, yy, self.pmin_sp.value(), self.pmax_sp.value())
        st["oscillation"] = {"mode": INT_MODES[mode], "period_s": per}
        lines = [f"peak at {pk.pos:.1f} px, window ±{hw:.1f} px"]
        if locked is not None and len(locked[0]):
            lines.append(f"azimuth-locked: {len(locked[0])} revolutions, "
                         f"I {np.nanmin(locked[1]):.2f} → {np.nanmax(locked[1]):.2f}")
        if per:
            near_rot = T and any(abs(per - T / k) / (T / k) < 0.05 for k in (1, 2, 3, 4, 5, 6))
            lines.append(f"<b>FFT period {per:.2f} s</b> → {1 / per:.3f} ML/s if 1 osc. = 1 ML"
                         + (f" <span style='color:{OR}'>(≈ rotation T/k — likely rotation, not growth)</span>"
                            if near_rot else ""))
            if mode == 0:
                lines.append(f"<span style='color:{DIM}'>1 sample/rev → only periods > 2T "
                             f"({2 * T:.0f} s) are resolvable</span>")
        self.int_lbl.setText("<br>".join(lines))
        if HAS_PG:
            self.int_plot.plot(ser.times, I, pen=pg.mkPen(BL + "90", width=1), name="all frames")
            if norm is not None and mode == 2:
                sc = np.nanmax(I) / max(np.nanmax(norm), 1e-9)
                self.int_plot.plot(ser.times, norm * sc, pen=pg.mkPen(PU, width=1), name="normalised (scaled)")
            if locked is not None and len(locked[0]):
                self.int_plot.plot(locked[0], locked[1], pen=pg.mkPen(OR, width=2), symbol="o",
                                   symbolSize=8, symbolBrush=OR, name="azimuth-locked")
            self.int_plot.addItem(self.int_line)
            if len(f):
                ok = f > 0
                self.fft_plot.plot(1.0 / f[ok], pw[ok], pen=pg.mkPen(GR, width=1.5))
                if per:
                    self.fft_plot.addItem(pg.InfiniteLine(math.log10(per), angle=90,
                                                          pen=pg.mkPen(YL, style=Qt.DashLine)))

    def _int_plot_clear(self):
        if HAS_PG:
            self.int_plot.clear(); self.fft_plot.clear()

    def _calibration(self):
        if self.cal_cb.currentIndex() == 0:
            if self.dpx_sp.value() > 0:
                return S.Calibration.from_reference(self.dpx_sp.value(), self.dref_sp.value(),
                                                    self.ref_mat_cb.currentText() if self.ref_mat_cb.currentIndex() > 0 else "")
            return S.Calibration()
        return S.Calibration.from_geometry(self.kev_sp.value(), self.L_sp.value(), self.mmpx_sp.value())

    def _calibrate_identify(self, st):
        cal = self._calibration()
        spc = st.get("spacing")
        st["calibration"] = cal
        st["candidates"] = []
        if not cal.k:
            self.cal_lbl.setText("not calibrated — spacing in px only")
            self.cand_tbl.setRowCount(0); self.id_lbl.setText("")
            return
        if spc is None:
            self.cal_lbl.setText(f"k = {cal.k:.2f} px·Å ({cal.source})"); return
        d = cal.d(spc.delta)
        st["d_A"] = d
        de = d * spc.err / spc.delta if np.isfinite(spc.err) else float("nan")
        self.cal_lbl.setText(f"k = {cal.k:.2f} px·Å<br><b style='color:{GR}'>row spacing d = {d:.4f}"
                             f"{'' if not np.isfinite(de) else f' ± {de:.4f}'} Å</b><br>"
                             f"<span style='color:{DIM}'>{cal.source}</span>")
        self.cal_lbl.setTextFormat(Qt.RichText)
        cands = S.identify(d, self.materials + self.cod_materials, self.tolid_sp.value() / 100.0)
        st["candidates"] = cands
        self.cand_tbl.setRowCount(len(cands))
        for i, (e, m, lab, dr) in enumerate(cands):
            for j, val in enumerate([f"{e * 100:+.1f}", m.name, m.surface, lab, f"{dr:.3f}"]):
                it = QTableWidgetItem(val); it.setToolTip(m.source)
                self.cand_tbl.setItem(i, j, it)
        self.id_lbl.setText("" if cands else f"<span style='color:{OR}'>no match within "
                            f"{self.tolid_sp.value():g} % — check calibration / add materials</span>")

    def _per_azimuth_spacing(self):
        """Δ at every kept azimuth → symmetry hint from the ratios."""
        s = self._summary()
        T, t0 = self._T_t0()
        if not s or not T:
            return
        out = []
        for pk in s["picks"]:
            if pk["angle_deg"] is None:
                continue
            prof, n = S.azimuth_profile(self.series, T, t0, pk["angle_deg"], self.tol_sp.value())
            if not n:
                continue
            peaks, _, _ = S.find_peaks(prof, bg_width=self.bg_sp.value(), sigma_thresh=self.sig_sp.value(),
                                       min_dist=self.md_sp.value(), axis=self.series.pos)
            spc = S.fit_spacing(peaks)
            if spc and spc.n_used >= 2:
                out.append((pk["angle_deg"], spc.delta, spc.n_used))
        self._state["per_azimuth"] = out
        if not out:
            return
        txt = "Δ per kept azimuth: " + ", ".join(f"{a:.0f}° {d:.1f}px" for a, d, _ in out)
        ds = [d for _, d, _ in out]
        if len(ds) >= 2 and max(ds) / min(ds) > 1.08:
            txt += "<br>" + S.ratio_hint(min(ds), max(ds))
        elif len(ds) >= 2:
            txt += "<br><span style='color:" + DIM + "'>all kept azimuths show the same Δ (one family)</span>"
        cur = self.id_lbl.text()
        self.id_lbl.setText((cur + "<br>" if cur else "") + txt)

    # ── tables / selection ────────────────────────────────────────────
    def _fill_peaks(self, peaks, orders, sel):
        t = self.peak_tbl
        t.blockSignals(True)
        t.setRowCount(len(peaks))
        for i, (pk, o) in enumerate(zip(peaks, orders)):
            for j, val in enumerate([f"{pk.pos:.1f}", "" if o is None else f"{o:+d}", f"{pk.height:.2f}",
                                     f"{pk.fwhm:.1f}", f"{pk.snr:.0f}"]):
                t.setItem(i, j, QTableWidgetItem(val))
        t.clearSelection()
        if sel is not None:
            t.selectRow(sel)
        t.blockSignals(False)

    def _peak_selected(self):
        rows = sorted({i.row() for i in self.peak_tbl.selectedIndexes()})
        peaks = self._state.get("peaks") or []
        if len(rows) == 1 and rows[0] < len(peaks):
            self._sel_pos = peaks[rows[0]].pos
            self._changed()

    def _set_ref(self):
        rows = sorted({i.row() for i in self.peak_tbl.selectedIndexes()})
        peaks = self._state.get("peaks") or []
        if rows and rows[0] < len(peaks):
            self._ref_pos = peaks[rows[0]].pos
            self._changed()

    def _seed(self):
        rows = sorted({i.row() for i in self.peak_tbl.selectedIndexes()})
        peaks = self._state.get("peaks") or []
        if len(rows) == 2 and rows[1] < len(peaks):
            self._delta0 = abs(peaks[rows[1]].pos - peaks[rows[0]].pos)
            self._ref_pos = peaks[rows[0]].pos
            self._changed()
        else:
            self.p._log("Select exactly two neighbouring peaks to seed the spacing", OR)

    def _auto_spacing(self):
        self._delta0 = self._ref_pos = None
        self._changed()

    def _prof_clicked(self, ev):
        if ev.button() != Qt.LeftButton or not self._state.get("peaks"):
            return
        vb = self.prof_plot.getPlotItem().vb
        if not vb.sceneBoundingRect().contains(ev.scenePos()):
            return
        x = vb.mapSceneToView(ev.scenePos()).x()
        i = self._nearest(self._state["peaks"], x)
        if i is not None:
            self._sel_pos = self._state["peaks"][i].pos
            self._changed()

    # ── calibration helpers ───────────────────────────────────────────
    def _fill_ref_materials(self):
        self.ref_mat_cb.clear(); self.ref_mat_cb.addItem("— fill d from a known surface —", None)
        for m in self.materials + self.cod_materials:
            for lab, dr in S.row_spacings(m):
                self.ref_mat_cb.addItem(f"{m.name} {m.surface} beam ∥ {lab}: {dr:.3f} Å", dr)

    def _ref_material(self, i):
        d = self.ref_mat_cb.itemData(i)
        if d:
            self.dref_sp.setValue(d)

    def _use_current(self):
        spc = self._state.get("spacing")
        if spc:
            self.dpx_sp.setValue(spc.delta)
        else:
            self.p._log("No spacing measured at this azimuth yet", OR)

    def _cal_mode(self, i):
        self.cal_stack.setCurrentIndex(i); self._changed()

    # ── COD lookup (worker thread) ────────────────────────────────────
    def _cod_search(self):
        f = self.cod_ed.text().strip()
        if not f:
            return
        self.cod_btn.setEnabled(False); self.id_lbl.setText(f"querying COD for {f}…")

        def work():
            try:
                ms = S.cod_lookup(f)
                self._b.cod_done.emit(ms, f"COD: {len(ms)} surface net(s) for {f}")
            except Exception as e:
                self._b.cod_done.emit(None, f"COD lookup failed: {e}")
        threading.Thread(target=work, name="RHEED-cod", daemon=True).start()

    def _on_cod(self, ms, msg):
        self.cod_btn.setEnabled(True)
        if ms:
            keys = {(m.name, m.surface, round(m.a, 3)) for m in self.cod_materials}
            self.cod_materials += [m for m in ms if (m.name, m.surface, round(m.a, 3)) not in keys]
            self._fill_ref_materials()
        self.p._log(msg, GR if ms else OR)
        self.recompute()
        self.id_lbl.setText(msg + ("<br>" + self.id_lbl.text() if self.id_lbl.text() else ""))

    # ── profile band / re-extraction ──────────────────────────────────
    def _band_toggled(self, on):
        if not HAS_PG:
            return
        if not hasattr(self, "band_roi"):
            self.band_roi = pg.RectROI([0, 0], [10, 10], pen=pg.mkPen(GR, width=2))
            self.p.vb.addItem(self.band_roi)
            b = self.series.band if self.series else (0, 0, 200, 200)
            self.band_roi.setPos([b[0], b[1]]); self.band_roi.setSize([b[2], b[3]])
        self.band_roi.setVisible(on)

    def band(self):
        """User band (x, y, w, h) or None."""
        if not (self.band_chk.isChecked() and hasattr(self, "band_roi")):
            return None
        (x, y), (w, h) = self.band_roi.pos(), self.band_roi.size()
        return (int(x), int(y), int(w), int(h))

    def _reextract(self):
        s = self._summary()
        if self.pe.busy:
            self.pe.abort(); return
        if not s:
            return
        band = self.band() or s["profiles"].band
        st = s["settings"]
        fps = s["info"].fps
        if self.pe.start(s["info"].path, int(round(st["start_s"] * fps)), int(round(st["end_s"] * fps)),
                         band, s["result"].orientation):
            self.reext_btn.setText("✕ Abort")

    def _on_profiles(self, ser, msg):
        self.reext_btn.setText("▶ Re-extract profiles")
        if ser is None:
            self.band_lbl.setText(msg); self.p._log(msg, OR); return
        self.series = ser
        s = self._summary()
        if s:
            s["profiles"] = ser
            s["settings"]["profile_band"] = list(ser.band)
        self.band_lbl.setText(f"band {ser.band}  ·  {len(ser.times)} profiles")
        self.p._log(f"Profiles re-extracted: {msg}", GR)
        self._ref_pos = self._sel_pos = self._delta0 = None
        self._kymo_set()
        self.recompute()

    # ── plots & overlay ───────────────────────────────────────────────
    def _plot_profile(self, st):
        if not HAS_PG:
            return
        pp = self.prof_plot; pp.clear()
        pos, prof, hp = st["profile"]
        pp.setLabel("bottom", f"Position across the streaks (frame px, {'y' if self.series.orientation == 'horizontal' else 'x'})")
        pp.plot(pos, prof, pen=pg.mkPen(DIM, width=1), name="profile")
        pp.plot(pos, hp, pen=pg.mkPen(BL, width=2), name="minus background")
        peaks, orders, sel = st["peaks"], st["orders"], st["sel"]
        if peaks:
            pp.plot([pk.pos for pk in peaks], [pk.height for pk in peaks], pen=None, symbol="t",
                    symbolSize=11, symbolBrush=OR, name="peaks")
            for i, (pk, o) in enumerate(zip(peaks, orders)):
                if o is not None:
                    t = pg.TextItem(f"{o:+d}", color=YL if i == sel else FG, anchor=(0.5, 1.2))
                    t.setPos(pk.pos, pk.height); pp.addItem(t)
            if sel is not None:
                pk = peaks[sel]
                hw = st["intensity"]["half_w"] if st.get("intensity") else pk.fwhm / 2
                pp.addItem(pg.LinearRegionItem([pk.pos - hw, pk.pos + hw], movable=False,
                                               brush=pg.mkBrush(YL + "30")))
        spc = st.get("spacing")
        if spc:
            lo, hi = pos[0], pos[-1]
            n0, n1 = math.floor((lo - spc.x0) / spc.delta), math.ceil((hi - spc.x0) / spc.delta)
            for n in range(n0, n1 + 1):
                x = spc.x0 + n * spc.delta
                if lo <= x <= hi:
                    pp.addItem(pg.InfiniteLine(x, angle=90, pen=pg.mkPen(GR + "80", style=Qt.DashLine)))

    def _kymo_set(self):
        if not HAS_PG or self.series is None or not len(self.series.times):
            return
        ser = self.series
        img = ser.P.T                                      # rows = position, cols = time
        lo, hi = np.percentile(img[::4, ::4], [0.5, 99.8])
        self.kymo_img.setImage(img, autoLevels=False, levels=(lo, max(hi, lo + 1)))
        t0, t1 = ser.times[0], ser.times[-1]
        self.kymo_img.setRect(t0, ser.pos[0], max(t1 - t0, 1e-3), len(ser.pos))
        self.kymo_plot.setLabel("left", f"Position ({'y' if ser.orientation == 'horizontal' else 'x'}, frame px)")
        if self.kymo_line.scene() is None:
            self.kymo_plot.addItem(self.kymo_line)
        self.kymo_plot.autoRange()

    def _clear_overlay(self):
        if HAS_PG:
            for it in self._overlay:
                self.p.vb.removeItem(it)
        self._overlay = []

    def _draw_overlay(self):
        self._clear_overlay()
        if not HAS_PG or not self.overlay_chk.isChecked() or not self._state.get("peaks"):
            return
        horiz = self.series.orientation == "horizontal"
        for i, (pk, o) in enumerate(zip(self._state["peaks"], self._state["orders"])):
            col = YL if i == self._state["sel"] else (GR if o is not None else DIM)
            ln = pg.InfiniteLine(pk.pos, angle=0 if horiz else 90,
                                 pen=pg.mkPen(col, width=2 if i == self._state["sel"] else 1,
                                              style=Qt.SolidLine if o is not None else Qt.DotLine))
            if o is not None:
                ln.label = pg.InfLineLabel(ln, text=f"{o:+d}", position=0.02, color=col)
            self.p.vb.addItem(ln); self._overlay.append(ln)
        # kymograph: the followed peak's window
        for ln in self._kymo_lines:
            self.kymo_plot.removeItem(ln)
        self._kymo_lines = []
        if self._state.get("intensity"):
            it = self._state["intensity"]
            for y in (it["pos"] - it["half_w"], it["pos"] + it["half_w"]):
                ln = pg.InfiniteLine(y, angle=0, pen=pg.mkPen(YL + "a0", width=1))
                self.kymo_plot.addItem(ln); self._kymo_lines.append(ln)

    def on_frame(self, t):
        """Viewer moved — keep the time cursors in sync."""
        if HAS_PG:
            self.kymo_line.setValue(t); self.int_line.setValue(t)

    # ── export ────────────────────────────────────────────────────────
    def report(self):
        st = self._state
        if not st or self.series is None:
            return None
        spc = st.get("spacing")
        cal = st.get("calibration")
        osc = st.get("oscillation") or {}
        summ = {"azimuth_deg": st.get("azimuth"), "azimuth_tol_deg": st.get("tol"),
                "n_frames_at_azimuth": st.get("n_frames"), "profile_band": list(self.series.band),
                "orientation": self.series.orientation,
                "background_width_px": self.bg_sp.value(), "threshold_sigma": self.sig_sp.value(),
                "spacing_px": spc.delta if spc else None, "spacing_err_px": spc.err if spc else None,
                "spacing_orders_used": spc.n_used if spc else None,
                "half_order_peaks": len(spc.half_orders) if spc else None,
                "followed_peak_px": st["intensity"]["pos"] if st.get("intensity") else None,
                "intensity_mode": osc.get("mode"), "oscillation_period_s": osc.get("period_s"),
                "calibration_k_px_A": cal.k if cal else None, "calibration": cal.source if cal else "",
                "row_spacing_A": st.get("d_A"),
                "per_azimuth_spacing": [{"azimuth_deg": a, "spacing_px": d, "orders": n}
                                        for a, d, n in st.get("per_azimuth", [])]}
        return {**st, "summary": summ, "kymograph": self.series.P}


# ══════════════════════════════════════════════════════════════════════
# ③ Export
# ══════════════════════════════════════════════════════════════════════

class ExportStep(QWidget):
    OPTIONS = [("images", "Azimuth images (best + avg PNG)", True),
               ("avg16", "Averages as 16-bit PNG", False),
               ("enhanced", "Contrast-enhanced copies (_enh)", True),
               ("tables", "Frame table (frames.csv)", True),
               ("summary", "Summary JSON", True),
               ("spatial", "Spatial results (CSVs + kymograph)", True),
               ("plots", "Plot screenshots (PNG)", True)]

    TIPS = {"avg16": "Averaged images as 16-bit PNG (grey ×256) — keeps the sub-grey-level\n"
                     "precision that averaging dozens of frames buys (for quantitative work)",
            "enhanced": "Percentile contrast-stretched copies — for viewing only, not for analysis",
            "spatial": "profile_az*.csv, peaks_az*.csv, intensity_all_frames_az*.csv,\n"
                       "intensity_azimuth_locked_az*.csv, identification.csv,\n"
                       "kymograph_profile_vs_time.png",
            "plots": "plot_*.png of every result tab"}

    def __init__(self, panel):
        super().__init__()
        self.p = panel
        self._last = ""
        v = QVBoxLayout(self); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(8)
        g = _group("Where"); gl = QGridLayout(g)
        gl.addWidget(QLabel("Base folder:"), 0, 0)
        self.base_ed = QLineEdit(panel.out_root); gl.addWidget(self.base_ed, 0, 1)
        b = _btn("…", DIM, 30); b.clicked.connect(self._browse); gl.addWidget(b, 0, 2)
        gl.addWidget(QLabel("Folder name:"), 1, 0)
        self.name_ed = QLineEdit(); self.name_ed.setPlaceholderText("type a name for this export")
        gl.addWidget(self.name_ed, 1, 1)
        b = _btn("↻", DIM, 30); b.setToolTip("Suggest <date_time>_<sample>"); b.clicked.connect(self.suggest)
        gl.addWidget(b, 1, 2)
        self.path_lbl = QLabel(""); self.path_lbl.setWordWrap(True); self.path_lbl.setStyleSheet(f"color:{DIM};")
        gl.addWidget(self.path_lbl, 2, 0, 1, 3)
        self.base_ed.textChanged.connect(self._path_preview); self.name_ed.textChanged.connect(self._path_preview)
        gl.setColumnStretch(1, 1)
        v.addWidget(g)
        g = _group("What"); gv = QVBoxLayout(g)
        self.chk = {}
        for key, text, on in self.OPTIONS:
            c = QCheckBox(text); c.setChecked(on); c.setToolTip(self.TIPS.get(key, ""))
            self.chk[key] = c; gv.addWidget(c)
        v.addWidget(g)
        self.exp_btn = _btn("💾 Export", GR); self.exp_btn.clicked.connect(self.export)
        v.addWidget(self.exp_btn)
        self.status = QLabel("Nothing has been written — results live in memory until you export.")
        self.status.setWordWrap(True); self.status.setStyleSheet(f"color:{DIM};")
        v.addWidget(self.status)
        self.open_btn = _btn("📂 Open export folder", DIM); self.open_btn.clicked.connect(self._open)
        v.addWidget(self.open_btn)
        v.addStretch(1)
        self.set_enabled(False)

    def set_enabled(self, on):
        self.exp_btn.setEnabled(on)
        if on and not self.name_ed.text().strip():
            self.suggest()

    def suggest(self):
        self.name_ed.setText(default_folder_name(self.p.sample_name()))

    def _path_preview(self):
        n = safe_name(self.name_ed.text())
        full = os.path.join(self.base_ed.text().strip(), n) if n else ""
        self.path_lbl.setText(f"→ …{os.sep}{n}" if n else "← type a folder name")
        self.path_lbl.setToolTip(full)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Export base folder", self.base_ed.text())
        if d:
            self.base_ed.setText(d)

    def export(self):
        s = self.p._summary
        if s is None:
            return
        name = safe_name(self.name_ed.text())
        base = self.base_ed.text().strip()
        if not name or not base:
            self.status.setText(f"<span style='color:{OR}'>Type a folder name first.</span>"); return
        out = os.path.join(base, name)
        if os.path.isdir(out) and os.listdir(out):
            if QMessageBox.question(self, "Folder exists",
                                    f"{out}\nalready contains files. Write into it (same-named files are replaced)?",
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
        opts = {k: c.isChecked() for k, c in self.chk.items()}
        spatial = self.p.spatial.report() if opts.get("spatial") or opts.get("summary") else None
        extra = [self._plot_writer()] if opts.get("plots") else []
        try:
            files = export_analysis(out, s, opts, spatial=spatial, extra=extra)
        except Exception as e:
            self.status.setText(f"<span style='color:{RD}'>Export failed: {e}</span>")
            self.p._log(f"Export failed: {e}", RD)
            return
        self._last = out
        self.status.setText(f"<span style='color:{GR}'>✓ {len(files)} file(s) → {name}</span>")
        self.status.setToolTip(out)
        self.p._log(f"💾 Exported {len(files)} file(s) → {out}", GR)

    def _plot_writer(self):
        plots = self.p.export_plots()

        def write(out_dir):
            paths = []
            for name, w in plots:
                try:
                    ex = pg.exporters.ImageExporter(w.getPlotItem())
                    ex.parameters()["width"] = 1400
                    f = os.path.join(out_dir, f"plot_{name}.png")
                    ex.export(f)
                    paths.append(f)
                except Exception:
                    continue
            return paths
        return write

    def _open(self):
        d = self._last if self._last and os.path.isdir(self._last) else self.base_ed.text().strip()
        if not d or not os.path.isdir(d):
            self.status.setText("Nothing exported yet."); return
        if sys.platform.startswith("win"):
            os.startfile(d)
        else:
            subprocess.Popen(["xdg-open", d])
