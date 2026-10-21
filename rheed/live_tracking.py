"""
TrackingGroup — the live panel's "Intensity tracking (RHEED oscillations)" controls,
its 📈 Intensity (live) plot and the cyan INTENSITY BOX on the live image.
Drives tracking.RotationLock / IntensityTracker (worker threads):

  Rotating  1. type the approximate rpm → 🔒 Lock rotation (2.6 revolutions of
               thumbnails, nothing saved) → T, phase, kept directions.
            2. drag the cyan box onto the specular spot (or a streak), pick the
               direction, ±window → ▶ Start. Only frames inside the window are
               processed; one point per revolution; the lock follows rpm drift.
  Static    rotation off: every Nth frame's box mean (classic RHEED oscillations).

  ⏱ t = 0 marks growth start (shutter open) — the plot's time origin (and, on the
  simulated source, restarts its test oscillation). FFT → oscillation period → ML/s.
  Nothing is written until 💾 Save CSV….

Threading: worker callbacks → _Bridge pyqtSignals; points are POLLED from the tracker
(snapshot) by a 300 ms QTimer — no per-frame signals.
"""

import csv
import os
import time
from datetime import datetime

import numpy as np
from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QGridLayout,
                             QHBoxLayout, QLabel, QSpinBox)

try:
    import pyqtgraph as pg
    HAS_PG = True
except ImportError:
    HAS_PG = False

from .camera import SimRHEEDCamera
from .spatial import oscillation_period
from .tracking import IntensityTracker, RotationLock
from .ui_common import BG, BL, CY, DIM, FG, GR, OR, RD, YL, _btn, _group

MODES = ["Rotating — lock to one direction", "Static substrate — every frame"]


class _Bridge(QObject):
    lock_done = pyqtSignal(object, str)
    lock_progress = pyqtSignal(float, str)


class TrackingGroup:
    def __init__(self, panel):
        self.p = panel                       # RHEEDLivePanel
        self.source = None
        self.lock = None                     # LockResult
        self.locker = None
        self.tracker = None
        self._n = 0                          # points already pulled from the tracker
        self.t_pts, self.v_pts, self.err_pts, self.clean_pts = [], [], [], []
        self.t_zero = None                   # absolute time of t = 0 (growth start)
        self._b = _Bridge()
        self._b.lock_done.connect(self._on_lock)
        self._b.lock_progress.connect(lambda f, m: self.lock_lbl.setText(m))
        self._poll = QTimer(); self._poll.timeout.connect(self._pull); self._poll.start(300)
        self._build()
        self._build_plot()
        self._build_box()
        self._update()

    # ── UI ────────────────────────────────────────────────────────────
    def _build(self):
        g = self.group = _group("Intensity tracking — RHEED oscillations")
        gl = QGridLayout(g); r = 0
        self.mode_cb = QComboBox(); self.mode_cb.addItems(MODES)
        self.mode_cb.currentIndexChanged.connect(lambda _: self._update())
        gl.addWidget(self.mode_cb, r, 0, 1, 2); r += 1
        gl.addWidget(QLabel("Approx. rotation (rpm):"), r, 0)
        self.rpm_sp = QDoubleSpinBox(); self.rpm_sp.setRange(0.1, 120); self.rpm_sp.setDecimals(2)
        self.rpm_sp.setValue(3.1)
        self.rpm_sp.setToolTip("Rough rpm of the substrate rotation — the lock searches ±25 % around it\n"
                               "and records ~2.6 revolutions to measure the exact period and phase.")
        gl.addWidget(self.rpm_sp, r, 1); r += 1
        gl.addWidget(QLabel("Lock length (rev):"), r, 0)
        self.revs_sp = QDoubleSpinBox(); self.revs_sp.setRange(2, 10); self.revs_sp.setDecimals(1); self.revs_sp.setValue(3)
        self.revs_sp.setToolTip("Revolutions recorded (as thumbnails only) to measure the period.\n"
                                "On MVI_2527 a 2.3–5 rev lock scattered ±3 % (non-uniform rotation);\n"
                                "the tracker's phase-locked loop absorbs that within a few passes.")
        gl.addWidget(self.revs_sp, r, 1); r += 1
        self.lock_btn = _btn("🔒 Lock rotation", BL); self.lock_btn.clicked.connect(self._toggle_lock)
        gl.addWidget(self.lock_btn, r, 0, 1, 2); r += 1
        self.lock_lbl = QLabel("not locked"); self.lock_lbl.setWordWrap(True)
        self.lock_lbl.setStyleSheet(f"color:{DIM};"); self.lock_lbl.setTextFormat(Qt.RichText)
        gl.addWidget(self.lock_lbl, r, 0, 1, 2); r += 1
        gl.addWidget(QLabel("Track direction:"), r, 0)
        self.az_cb = QComboBox(); self.az_cb.setToolTip("Kept directions of the lock (° from the lock start)")
        gl.addWidget(self.az_cb, r, 1); r += 1
        gl.addWidget(QLabel("Window ± (°):"), r, 0)
        self.win_sp = QDoubleSpinBox(); self.win_sp.setRange(1, 20); self.win_sp.setDecimals(1); self.win_sp.setValue(6)
        self.win_sp.setToolTip("Only frames within ± this of the direction are processed (~2·w/360 of\n"
                               "all frames). Widens automatically while re-acquiring after a slip.")
        gl.addWidget(self.win_sp, r, 1); r += 1
        gl.addWidget(QLabel("Value per pass:"), r, 0)
        self.val_cb = QComboBox(); self.val_cb.addItems(["Peak (max)", "Mean of window"])
        gl.addWidget(self.val_cb, r, 1); r += 1
        gl.addWidget(QLabel("Static: every Nth frame:"), r, 0)
        self.every_sp = QSpinBox(); self.every_sp.setRange(1, 100); self.every_sp.setValue(1)
        gl.addWidget(self.every_sp, r, 1); r += 1
        self.box_chk = QCheckBox("Show intensity box (cyan) — drag onto the spot")
        self.box_chk.setChecked(True)
        self.box_chk.toggled.connect(lambda on: self.box is not None and self.box.setVisible(on))
        gl.addWidget(self.box_chk, r, 0, 1, 2); r += 1
        h = QHBoxLayout()
        self.run_btn = _btn("▶ Start", GR); self.run_btn.clicked.connect(self._toggle_track)
        self.zero_btn = _btn("⏱ t = 0", YL); self.zero_btn.clicked.connect(self._mark_zero)
        self.zero_btn.setToolTip("Mark growth start (shutter open) — the plot's time origin")
        self.clear_btn = _btn("Clear", DIM); self.clear_btn.clicked.connect(self._clear)
        h.addWidget(self.run_btn); h.addWidget(self.zero_btn); h.addWidget(self.clear_btn)
        gl.addLayout(h, r, 0, 1, 2); r += 1
        h = QHBoxLayout()
        h.addWidget(QLabel("FFT period (s):"))
        self.pmin_sp = QDoubleSpinBox(); self.pmin_sp.setRange(0.1, 1000); self.pmin_sp.setValue(0.5)
        self.pmax_sp = QDoubleSpinBox(); self.pmax_sp.setRange(0.2, 5000); self.pmax_sp.setValue(60)
        h.addWidget(self.pmin_sp); h.addWidget(QLabel("–")); h.addWidget(self.pmax_sp)
        gl.addLayout(h, r, 0, 1, 2); r += 1
        self.save_btn = _btn("💾 Save CSV…", DIM); self.save_btn.clicked.connect(self._save)
        gl.addWidget(self.save_btn, r, 0, 1, 2); r += 1
        self.stat_lbl = QLabel(""); self.stat_lbl.setWordWrap(True); self.stat_lbl.setTextFormat(Qt.RichText)
        gl.addWidget(self.stat_lbl, r, 0, 1, 2); r += 1
        gl.setColumnStretch(1, 1)

    def _build_plot(self):
        self.plot = None
        if not HAS_PG:
            return
        self.plot = pg.PlotWidget(); self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setLabel("bottom", "Time since t = 0 (s)"); self.plot.setLabel("left", "Box intensity (grey)")
        self.curve = self.plot.plot([], [], pen=pg.mkPen(BL, width=1.5), symbol="o", symbolSize=6,
                                    symbolBrush=OR, symbolPen=None)
        self.zero_line = pg.InfiniteLine(0, angle=90, pen=pg.mkPen(YL, style=Qt.DashLine),
                                         label="t = 0", labelOpts={"color": YL, "position": 0.95})
        self.plot.addItem(self.zero_line)

    def _build_box(self):
        self.box = None
        self._box_placed = False
        if HAS_PG and getattr(self.p, "vb", None) is not None:
            self.box = pg.RectROI([0, 0], [60, 60], pen=pg.mkPen(CY, width=2))
            self.box.sigRegionChangeFinished.connect(self._box_moved)
            self.p.vb.addItem(self.box)

    def place_box(self, w, h):
        """First frame of a new source: put the box in the middle until the user moves it."""
        if self.box is not None and not self._box_placed:
            s = max(20, int(min(w, h) * 0.05))
            self.box.setPos([w / 2 - s / 2, h / 2 - s / 2]); self.box.setSize([s, s])
            self._box_placed = True

    def box_rect(self):
        if self.box is None:
            return (0, 0, 1, 1)
        (x, y), (w, h) = self.box.pos(), self.box.size()
        return (int(x), int(y), max(1, int(w)), max(1, int(h)))

    def _box_moved(self, *_):
        if self.tracker is not None:
            self.tracker.set_box(self.box_rect())

    # ── source ────────────────────────────────────────────────────────
    def set_source(self, src):
        self.stop()
        if self.locker and self.locker.busy:
            self.locker.stop()
        self.source = src
        self._box_placed = False
        self.lock = None
        self.az_cb.clear()
        self.lock_lbl.setText("not locked"); self.lock_lbl.setStyleSheet(f"color:{DIM};")
        self._update()

    # ── lock ──────────────────────────────────────────────────────────
    def _toggle_lock(self):
        if self.locker and self.locker.busy:
            self.locker.stop(); return
        if self.source is None or not self.source.connected:
            self.p._log("Connect a source before locking", OR); return
        rpm = self.rpm_sp.value()
        dur = min(600.0, max(6.0, self.revs_sp.value() * 60.0 / rpm))
        self.locker = RotationLock(self.source)
        self.locker.on_done = lambda r, m: self._b.lock_done.emit(r, m)
        self.locker.on_progress = lambda f, m: self._b.lock_progress.emit(f, m)
        if self.locker.start(dur, expected_rpm=rpm, roi=self.p._current_roi(), n_picks=4):
            self.p._log(f"🔒 Locking: {dur:.0f} s of thumbnails at ~{rpm:g} rpm (nothing saved)")
        self._update()

    def _on_lock(self, lk, msg):
        self._update()
        if lk is None or not lk.period_s:
            self.lock = None
            self.lock_lbl.setText(f"<span style='color:{OR}'>no lock: {msg}</span><br>"
                                  f"<span style='color:{DIM}'>static substrate? → Static mode</span>")
            self.p._log(f"Rotation lock failed: {msg}", OR)
            return
        self.lock = lk
        self.az_cb.clear()
        best = int(np.argmax([s for _, s in lk.picks])) if lk.picks else 0
        for a, s in lk.picks:
            self.az_cb.addItem(f"{a:6.1f}°   q {s:.2f}", a)
        self.az_cb.setCurrentIndex(best)
        self.lock_lbl.setText(f"<b style='color:{GR}'>T = {lk.period_s:.3f} s ({lk.rpm:.3f} rpm)</b><br>"
                              f"<span style='color:{DIM}'>{msg}</span>")
        self.p._log(f"🔒 Locked: {msg}", GR)

    # ── tracking ──────────────────────────────────────────────────────
    def _toggle_track(self):
        if self.tracker is not None and self.tracker.busy:
            self.stop(); return
        if self.source is None or not self.source.connected:
            self.p._log("Connect a source first", OR); return
        tr = IntensityTracker(self.source)
        static = self.mode_cb.currentIndex() == 1
        if static:
            ok = tr.start_static(self.box_rect(), every=self.every_sp.value())
        else:
            if self.lock is None:
                self.p._log("Lock the rotation first (🔒), or use Static mode", OR); return
            ok = tr.start_rotating(self.box_rect(), self.lock.period_s, self.lock.t_ref,
                                   float(self.az_cb.currentData()), window_deg=self.win_sp.value(),
                                   value="peak" if self.val_cb.currentIndex() == 0 else "mean")
        if not ok:
            self.p._log("Tracking not started", RD); return
        self.tracker, self._n = tr, 0
        if self.t_zero is None:
            self.t_zero = time.time()
        self.p._log("▶ Intensity tracking started (" + ("static, every frame" if static else
                    f"rotating, direction {self.az_cb.currentText().split()[0]} ± {self.win_sp.value():g}°") + ")")
        self._update()

    def stop(self):
        if self.tracker is not None and self.tracker.busy:
            self.tracker.stop()
            self._pull()
            self.p._log(f"⏹ Tracking stopped — {len(self.t_pts)} points (not saved — 💾 Save CSV…)")
        self._update()

    def _mark_zero(self):
        self.t_zero = time.time()
        if isinstance(self.source, SimRHEEDCamera):
            self.source.start_growth()                   # test oscillation restarts at t = 0
        self.p._log("⏱ t = 0 marked (growth start)", YL)
        self._redraw()

    def _clear(self):
        self.t_pts, self.v_pts, self.err_pts, self.clean_pts = [], [], [], []
        self._n = 0
        if self.tracker is not None:
            self.tracker.clear()
        self._redraw()

    def _pull(self):
        tr = self.tracker
        if tr is None:
            return
        new = tr.snapshot(self._n)
        if new:
            self._n += len(new)
            for p in new:
                self.t_pts.append(p.t); self.v_pts.append(p.value)
                self.err_pts.append(p.phase_err_s / p.period_s * 360.0 if p.period_s == p.period_s else np.nan)
                self.clean_pts.append(p.locked)
            self._redraw()
        self._status()
        if not tr.busy and self.run_btn.text() != "▶ Start":
            self._update()

    def _status(self):
        tr = self.tracker
        if tr is None:
            return
        lines = []
        if tr.frames_seen:
            lines.append(f"{len(self.t_pts)} points · processing <b>{100 * tr.frames_processed / tr.frames_seen:.1f} %</b> "
                         f"of {tr.frames_seen} frames")
        if tr.period_s:
            last_err = next((e for e in reversed(self.err_pts) if e == e), None)
            lines.append(f"T {tr.period_s:.3f} s ({60 / tr.period_s:.3f} rpm) · window ±{tr.window_eff:.0f}°"
                         + (f" · phase err {last_err:+.1f}°" if last_err is not None else ""))
            if tr.window_eff > self.win_sp.value() + 0.5:
                lines.append(f"<span style='color:{OR}'>re-acquiring (window widened)</span>")
        if len(self.t_pts) >= 16:
            per, _, _ = oscillation_period(np.array(self.t_pts), np.array(self.v_pts),
                                           self.pmin_sp.value(), self.pmax_sp.value())
            if per:
                lines.append(f"FFT period <b>{per:.2f} s</b> → {1 / per:.3f} ML/s if 1 osc = 1 ML")
                if tr.period_s:
                    lines.append(f"<span style='color:{DIM}'>1 point/rev → only periods > 2T "
                                 f"({2 * tr.period_s:.0f} s) are real</span>")
        if tr.message and not tr.busy:
            lines.append(f"<span style='color:{DIM}'>{tr.message}</span>")
        self.stat_lbl.setText("<br>".join(lines))

    def _redraw(self):
        if self.plot is None:
            return
        t0 = self.t_zero or (self.t_pts[0] if self.t_pts else 0.0)
        t = np.array(self.t_pts) - t0
        self.curve.setData(t, np.array(self.v_pts))
        static = self.tracker is not None and self.tracker.period_s is None
        self.curve.setSymbol(None if static and len(t) > 200 else "o")

    def _update(self):
        locking = bool(self.locker and self.locker.busy)
        tracking = bool(self.tracker and self.tracker.busy)
        static = self.mode_cb.currentIndex() == 1
        live = self.source is not None and self.source.connected
        self.lock_btn.setText("✕ Abort lock" if locking else "🔒 Lock rotation")
        self.lock_btn.setEnabled((live and not static and not tracking) or locking)
        for w in (self.rpm_sp, self.revs_sp, self.az_cb, self.win_sp, self.val_cb):
            w.setEnabled(not static and not tracking)
        self.every_sp.setEnabled(static and not tracking)
        self.mode_cb.setEnabled(not tracking and not locking)
        self.run_btn.setText("⏹ Stop" if tracking else "▶ Start")
        self.run_btn.setEnabled(tracking or (live and not locking and (static or self.lock is not None)))
        self.save_btn.setEnabled(bool(self.t_pts))

    # ── save (only on request) ────────────────────────────────────────
    def _save(self):
        if not self.t_pts:
            return
        d = os.path.join(self.p.out_root, "tracking")
        sample = self.p.sample_ed.text().strip()
        name = datetime.now().strftime("%Y%m%d_%H%M%S") + (f"_{sample}" if sample else "") + "_intensity.csv"
        path, _ = QFileDialog.getSaveFileName(self.p, "Save intensity trace", os.path.join(d, name), "CSV (*.csv)")
        if not path:
            return
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tr = self.tracker
        t0 = self.t_zero or self.t_pts[0]
        with open(path, "w", newline="", encoding="utf-8") as fh:
            fh.write(f"# RHEED intensity trace — {datetime.now().isoformat(timespec='seconds')}\n")
            fh.write(f"# mode: {self.mode_cb.currentText()}\n")
            fh.write(f"# intensity box (x,y,w,h): {self.box_rect()}\n")
            fh.write(f"# t=0 (unix): {t0:.3f}\n")
            if self.lock is not None and self.mode_cb.currentIndex() == 0:
                fh.write(f"# lock: T={self.lock.period_s:.4f} s, direction {self.az_cb.currentText()}, "
                         f"window ±{self.win_sp.value():g} deg, value {self.val_cb.currentText()}\n")
                if tr is not None and tr.period_s:
                    fh.write(f"# tracked T at end: {tr.period_s:.4f} s\n")
            w = csv.writer(fh)
            w.writerow(["Unix_Time", "Time_s", "Intensity", "Phase_err_deg", "Clean_peak"])
            for t, v, e, c in zip(self.t_pts, self.v_pts, self.err_pts, self.clean_pts):
                w.writerow([f"{t:.4f}", f"{t - t0:.4f}", f"{v:.4f}", "" if e != e else f"{e:.3f}", int(bool(c))])
        self.p._log(f"💾 Intensity trace saved → {path}", GR)

    def shutdown(self):
        self._poll.stop()
        if self.locker and self.locker.busy:
            self.locker.stop()
        if self.tracker and self.tracker.busy:
            self.tracker.stop()
