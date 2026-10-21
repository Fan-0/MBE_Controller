"""
RHEEDTab — self-contained QWidget for the RHEED camera. Two modes (inner tabs):
  📡 Live camera     RHEEDLivePanel (this file)
  🎞 Video analysis  RHEEDVideoPanel (video_tab.py) — offline analysis of video files

Runs standalone today (rheed_app.py) and is built to drop into the main controller:

    from rheed.rheed_tab import RHEEDTab
    self.rheed_tab = RHEEDTab(log_fn=self._log, out_root=os.path.join(_LOG_DIR, "rheed"))
    tabs.addTab(self.rheed_tab, "📡 RHEED")
    ...
    closeEvent:  self.rheed_tab.shutdown()

Threading (same rules as gui_app — CONTEXT Section 12):
  * The camera's capture thread never touches Qt. The tab POLLS it from QTimers:
      _view_t  (50 ms)  latest frame → ImageItem
      _stat_t  (250 ms) state / fps / resolution → status bar
  * Intensity tracking (RHEED oscillations, phase-locked to the rotation) lives in
    live_tracking.TrackingGroup — its controls, 📈 Intensity (live) plot and the cyan
    intensity box on the image.
  * Worker callbacks (RotationCapture / VideoRecorder) arrive on worker threads and
    are re-emitted through _Bridge pyqtSignals → queued onto the main thread.
    Never QTimer.singleShot from a plain thread (BUG 7 / BUG 20).
"""

import os
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

import numpy as np
from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont, QPixmap
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFrame, QGridLayout,
                             QHBoxLayout, QLabel, QLineEdit, QProgressBar,
                             QSizePolicy, QSpinBox, QSplitter, QTabWidget,
                             QVBoxLayout, QWidget, QScrollArea)

try:
    import pyqtgraph as pg
    HAS_PG = True
except ImportError:
    HAS_PG = False

from .camera import (RHEEDCamera, SimRHEEDCamera, VideoReplayCamera, list_cameras, HAS_CV2,
                     STATE_CONNECTED, STATE_CONNECTING, STATE_DISCONNECTED, STATE_IDLE)
from .capture import RotationCapture, VideoRecorder, save_snapshot
from .live_tracking import TrackingGroup
# palette re-exported here too: rheed_app imports BG/PBG/FG/BL from this module
from .ui_common import (BG, PBG, BDR, BL, GR, RD, OR, PU, FG, DIM, YL, TABS_CSS,
                        _Dot, _btn, _group, display_levels)

_DEFAULT_OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "rheed")
SIM_LABEL = "Simulated (test)"
REPLAY_LABEL = "Replay a video file (test)"


class _Bridge(QObject):
    progress = pyqtSignal(float, str)
    rot_done = pyqtSignal(object, str)
    rec_done = pyqtSignal(object, str)
    cams = pyqtSignal(object)


class RHEEDTab(QWidget):
    """The RHEED tab: two modes — 📡 Live camera and 🎞 Video analysis (offline,
    for recorded/older videos). Same constructor / shutdown() API as before, so the
    gui_app integration snippet (CONTEXT Section 20) is unchanged."""

    def __init__(self, log_fn: Optional[Callable[[str], None]] = None,
                 out_root: str = _DEFAULT_OUT, start_sim: bool = False,
                 video_path: Optional[str] = None, video_root: Optional[str] = None, parent=None):
        """out_root: snapshots, rotation keyframes, tracking CSVs, video-analysis exports
        (Box when hosted by the controller). video_root: raw ⏺ recordings (~12 MB/s MJPG)
        — keep these LOCAL, not on Box; default <out_root>/videos."""
        super().__init__(parent)
        from .video_tab import RHEEDVideoPanel
        v = QVBoxLayout(self); v.setContentsMargins(0, 0, 0, 0)
        self.modes = QTabWidget(); self.modes.setStyleSheet(TABS_CSS)
        self.live = RHEEDLivePanel(log_fn=log_fn, out_root=out_root, start_sim=start_sim,
                                   video_root=video_root)
        self.video = RHEEDVideoPanel(log_fn=log_fn, out_root=os.path.join(out_root, "video"))
        self.modes.addTab(self.live, "📡 Live camera")
        self.modes.addTab(self.video, "🎞 Video analysis")
        v.addWidget(self.modes)
        if video_path:
            self.modes.setCurrentWidget(self.video)
            self.video.load(video_path)

    def shutdown(self):
        """Call from the host window's closeEvent."""
        self.live.shutdown()
        self.video.shutdown()


class RHEEDLivePanel(QWidget):
    def __init__(self, log_fn: Optional[Callable[[str], None]] = None,
                 out_root: str = _DEFAULT_OUT, start_sim: bool = False,
                 video_root: Optional[str] = None, parent=None):
        super().__init__(parent)
        self._log_fn = log_fn
        self.out_root = out_root
        self.video_root = video_root or os.path.join(out_root, "videos")
        self._scanned = False               # camera scan runs on first show (see showEvent)
        self.source = None                  # RHEEDCamera | SimRHEEDCamera | None
        self.rot: Optional[RotationCapture] = None
        self.rec: Optional[VideoRecorder] = None
        self._last_idx = -1
        self._cams = []                     # scan results
        self._b = _Bridge()
        self._b.progress.connect(self._on_progress)
        self._b.rot_done.connect(self._on_rot_done)
        self._b.rec_done.connect(self._on_rec_done)
        self._b.cams.connect(self._on_cams)
        self._build()
        self._view_t = QTimer(self); self._view_t.timeout.connect(self._refresh_view); self._view_t.start(50)
        self._stat_t = QTimer(self); self._stat_t.timeout.connect(self._refresh_status); self._stat_t.start(250)
        if start_sim:
            self.src_cb.setCurrentText(SIM_LABEL)
            self._connect()

    def showEvent(self, e):
        # Scan on FIRST SHOW, not at construction: inside the main controller the tab is
        # built at startup, and the scan opens every DirectShow device (~1 s each) —
        # nothing camera-related should happen until the user opens the RHEED tab.
        super().showEvent(e)
        if not self._scanned:
            self._scanned = True
            self._scan()

    # ── logging ──
    def _log(self, msg: str, color: str = FG):
        if self._log_fn:
            self._log_fn(msg)
        self.log_lbl.setText(f"<span style='color:{color}'>{time.strftime('%H:%M:%S')}  {msg}</span>")

    # ══════════════════════════════════════════════════════════════════
    # UI
    # ══════════════════════════════════════════════════════════════════
    def _build(self):
        self.setStyleSheet(f"QLabel{{color:{FG};}} QCheckBox{{color:{FG};}}")
        root = QVBoxLayout(self); root.setContentsMargins(6, 6, 6, 6); root.setSpacing(6)
        root.addWidget(self._source_bar())

        split = QSplitter(Qt.Horizontal)
        left = QSplitter(Qt.Vertical)
        left.addWidget(self._view_panel())
        left.addWidget(self._results_panel())
        left.setStretchFactor(0, 3); left.setStretchFactor(1, 2)
        split.addWidget(left)
        ctrl = QScrollArea(); ctrl.setWidgetResizable(True); ctrl.setFrameShape(QFrame.NoFrame)
        ctrl.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        ctrl.setMinimumWidth(300); ctrl.setMaximumWidth(400)
        ctrl.setWidget(self._control_panel())
        split.addWidget(ctrl)
        split.setStretchFactor(0, 4); split.setStretchFactor(1, 1)
        root.addWidget(split, 1)

        self.log_lbl = QLabel(""); self.log_lbl.setStyleSheet(f"color:{DIM};font-family:'Courier New';")
        self.log_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self.log_lbl)

    def _source_bar(self):
        f = QFrame(); f.setStyleSheet(f"QFrame{{background:{PBG};border:1px solid {BDR};border-radius:5px;}}"
                                      f"QLabel{{border:none;}}")
        h = QHBoxLayout(f); h.setContentsMargins(8, 4, 8, 4); h.setSpacing(8)
        h.addWidget(QLabel("Source:"))
        self.src_cb = QComboBox(); self.src_cb.setMinimumWidth(220)
        self.src_cb.setToolTip("Device names need the optional 'pygrabber' package; without it devices\n"
                               "are 'Camera N'. Lab laptop (2026-10-07): [0] laptop webcam,\n"
                               "[1] EOS Webcam Utility (Canon over USB, ≤1280×720), [2] Guermok USB3\n"
                               "HDMI grabber = the RHEED path (1920×1080 @ 30 fps). Identify with\n"
                               "python diagnostics/rheed_camera_probe.py --index N (saves test frames).")
        self.src_cb.addItem(SIM_LABEL); self.src_cb.addItem(REPLAY_LABEL)
        h.addWidget(self.src_cb)
        self.scan_btn = _btn("🔍 Scan", DIM); self.scan_btn.clicked.connect(self._scan); h.addWidget(self.scan_btn)
        h.addWidget(QLabel("Res:"))
        self.res_cb = QComboBox(); self.res_cb.addItems(["1920×1080", "1280×720", "3840×2160"])
        h.addWidget(self.res_cb)
        h.addWidget(QLabel("Format:"))
        self.fmt_cb = QComboBox(); self.fmt_cb.addItems(["Auto", "MJPG", "YUY2"])
        self.fmt_cb.setToolTip("USB-2 capture dongles usually need MJPG for 1080p30;\n"
                               "USB-3 boxes (INOGENI) work with Auto/YUY2.")
        h.addWidget(self.fmt_cb)
        self.conn_btn = _btn("Connect", GR, 100); self.conn_btn.clicked.connect(self._toggle_connect)
        h.addWidget(self.conn_btn)
        self.auto_chk = QCheckBox("Auto contrast")
        self.auto_chk.setToolTip("Stretch the DISPLAY to the frame's 0.5–99.9 % grey levels —\n"
                                 "for a dim screen. Snapshots/captures are never altered.")
        h.addWidget(self.auto_chk)
        h.addSpacing(10)
        self.led = _Dot(); h.addWidget(self.led)
        self.state_lbl = QLabel("Idle"); self.state_lbl.setFont(QFont("Arial", 9, QFont.Bold))
        h.addWidget(self.state_lbl, 1)
        self.fps_lbl = QLabel(""); self.fps_lbl.setStyleSheet(f"color:{DIM};")
        h.addWidget(self.fps_lbl)
        return f

    def _view_panel(self):
        w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(0, 0, 0, 0)
        if not HAS_PG:
            self.view_lbl = QLabel("pyqtgraph not installed"); v.addWidget(self.view_lbl)
            self.img_item = None; self.roi = None
            return w
        glw = pg.GraphicsLayoutWidget(); glw.setBackground(BG)
        self.vb = glw.addViewBox(lockAspect=True, invertY=True, enableMenu=False)
        self.img_item = pg.ImageItem(axisOrder="row-major")
        self.vb.addItem(self.img_item)
        self.roi = pg.RectROI([100, 100], [400, 300], pen=pg.mkPen(YL, width=2))
        self.roi.setVisible(False)
        self.vb.addItem(self.roi)
        self._roi_placed = False
        v.addWidget(glw)
        return w

    def _results_panel(self):
        tabs = QTabWidget()
        tabs.setStyleSheet(TABS_CSS)
        # saved azimuth thumbnails
        sc = QScrollArea(); sc.setWidgetResizable(True); sc.setStyleSheet(f"background:{PBG};border:none;")
        self.thumb_w = QWidget(); self.thumb_l = QHBoxLayout(self.thumb_w); self.thumb_l.setAlignment(Qt.AlignLeft)
        sc.setWidget(self.thumb_w)
        tabs.addTab(sc, "🖼 Saved azimuths")
        if HAS_PG:
            pg.setConfigOption("background", BG); pg.setConfigOption("foreground", FG)
            self.q_plot = pg.PlotWidget(); self.q_plot.showGrid(x=True, y=True, alpha=0.25)
            self.q_plot.setLabel("bottom", "Azimuth (° from clip start)"); self.q_plot.setLabel("left", "Streak contrast")
            self.q_plot.setXRange(0, 360)
            tabs.addTab(self.q_plot, "📈 Quality vs azimuth")
            self.l_plot = pg.PlotWidget(); self.l_plot.showGrid(x=True, y=True, alpha=0.25)
            self.l_plot.setLabel("bottom", "Time lag (s)"); self.l_plot.setLabel("left", "Self-similarity")
            tabs.addTab(self.l_plot, "🔁 Self-similarity vs lag")
        self.res_tabs = tabs
        return tabs

    def _control_panel(self):
        w = QWidget()
        v = QVBoxLayout(w); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(8)

        g = _group("Capture"); gl = QGridLayout(g)
        gl.addWidget(QLabel("Sample:"), 0, 0)
        self.sample_ed = QLineEdit(); self.sample_ed.setPlaceholderText("e.g. BDEP170")
        gl.addWidget(self.sample_ed, 0, 1, 1, 2)
        self.snap_btn = _btn("📷 Snapshot", BL); self.snap_btn.clicked.connect(self._snapshot)
        self.rec_btn = _btn("⏺ Record", RD); self.rec_btn.clicked.connect(self._toggle_record)
        gl.addWidget(self.snap_btn, 1, 0, 1, 2); gl.addWidget(self.rec_btn, 1, 2)
        self.rec_lbl = QLabel(""); self.rec_lbl.setStyleSheet(f"color:{DIM};")
        gl.addWidget(self.rec_lbl, 2, 0, 1, 3)
        gl.setColumnStretch(1, 1)
        v.addWidget(g)

        g = _group("Rotation sync — keep best azimuths"); gl = QGridLayout(g)
        r = 0
        gl.addWidget(QLabel("Clip length (s):"), r, 0)
        self.dur_sp = QDoubleSpinBox(); self.dur_sp.setRange(3, 180); self.dur_sp.setValue(20); self.dur_sp.setDecimals(0)
        gl.addWidget(self.dur_sp, r, 1); r += 1
        gl.addWidget(QLabel("Rotation (rpm):"), r, 0)
        self.rpm_sp = QDoubleSpinBox(); self.rpm_sp.setRange(0, 120); self.rpm_sp.setDecimals(1)
        self.rpm_sp.setSpecialValueText("auto")
        self.rpm_sp.setToolTip("0 = detect automatically. Entering it narrows the search and\n"
                               "avoids mistaking a 90° pattern repeat for a full revolution.")
        self.rpm_sp.valueChanged.connect(self._rpm_changed)
        gl.addWidget(self.rpm_sp, r, 1); r += 1
        gl.addWidget(QLabel("Azimuths to keep:"), r, 0)
        self.npk_sp = QSpinBox(); self.npk_sp.setRange(1, 12); self.npk_sp.setValue(4)
        gl.addWidget(self.npk_sp, r, 1); r += 1
        self.roi_chk = QCheckBox("Analyse ROI only (yellow box)")
        self.roi_chk.setToolTip("Restrict the analysis to the box — e.g. exclude the\n"
                                "shadow edge, screen frame or a reflection.")
        self.roi_chk.toggled.connect(self._roi_toggled)
        gl.addWidget(self.roi_chk, r, 0, 1, 2); r += 1
        self.keep_chk = QCheckBox("Keep the raw clip")
        gl.addWidget(self.keep_chk, r, 0, 1, 2); r += 1
        self.lossless_chk = QCheckBox("Lossless clip (FFV1 — slow at 1080p)")
        gl.addWidget(self.lossless_chk, r, 0, 1, 2); r += 1
        self.rot_btn = _btn("▶ Capture && Analyse", GR); self.rot_btn.clicked.connect(self._toggle_rot)
        gl.addWidget(self.rot_btn, r, 0, 1, 2); r += 1
        self.prog = QProgressBar(); self.prog.setRange(0, 1000); self.prog.setTextVisible(False); self.prog.setFixedHeight(8)
        self.prog.setStyleSheet(f"QProgressBar{{background:{BG};border:1px solid {BDR};border-radius:3px;}}"
                                f"QProgressBar::chunk{{background:{GR};}}")
        gl.addWidget(self.prog, r, 0, 1, 2); r += 1
        self.prog_lbl = QLabel(""); self.prog_lbl.setStyleSheet(f"color:{DIM};"); self.prog_lbl.setWordWrap(True)
        gl.addWidget(self.prog_lbl, r, 0, 1, 2); r += 1
        gl.setColumnStretch(1, 1)
        v.addWidget(g)

        g = _group("Last result"); gv = QVBoxLayout(g)
        self.res_lbl = QLabel("—"); self.res_lbl.setWordWrap(True); self.res_lbl.setTextFormat(Qt.RichText)
        gv.addWidget(self.res_lbl)
        self.open_btn = _btn("📂 Open folder", DIM); self.open_btn.clicked.connect(self._open_folder)
        gv.addWidget(self.open_btn)
        v.addWidget(g)
        self.track = TrackingGroup(self)              # RHEED oscillations (live_tracking.py)
        v.addWidget(self.track.group)
        if self.track.plot is not None:
            self.res_tabs.addTab(self.track.plot, "📈 Intensity (live)")
        v.addStretch(1)
        self._last_dir = self.out_root
        self._update_buttons()
        return w

    # ══════════════════════════════════════════════════════════════════
    # Source management
    # ══════════════════════════════════════════════════════════════════
    def _scan(self):
        if not HAS_CV2:
            self._log("OpenCV not installed — only the simulated source is available", OR)
            return
        self.scan_btn.setEnabled(False); self.scan_btn.setText("Scanning…")
        busy_idx = self.source.index if isinstance(self.source, RHEEDCamera) else None

        def work():
            try:
                cams = list_cameras(skip=(busy_idx,) if busy_idx is not None else ())
            except Exception:
                cams = []
            self._b.cams.emit(cams)
        threading.Thread(target=work, name="RHEED-scan", daemon=True).start()

    def _on_cams(self, cams):
        self.scan_btn.setEnabled(True); self.scan_btn.setText("🔍 Scan")
        cur = self.src_cb.currentText()
        self.src_cb.blockSignals(True)
        self.src_cb.clear()
        for c in cams:
            # No size in the label: the scan only sees the device's IDLE size (often
            # 640×480), which users read as the capture quality (2026-10-07). The
            # delivered size is shown in the status bar after Connect.
            self.src_cb.addItem(f"[{c['index']}] {c['name']}", c["index"])
        if isinstance(self.source, RHEEDCamera) and all(c["index"] != self.source.index for c in cams):
            self.src_cb.addItem(f"[{self.source.index}] Camera {self.source.index} (in use)", self.source.index)
        self.src_cb.addItem(SIM_LABEL, None)
        self.src_cb.addItem(REPLAY_LABEL, None)
        i = self.src_cb.findText(cur)
        self.src_cb.setCurrentIndex(i if i >= 0 else 0)
        self.src_cb.blockSignals(False)
        self._log(f"Camera scan: {len(cams)} device(s) found" +
                  ("" if cams else " — is the capture device plugged in and the 90D on?"),
                  FG if cams else OR)

    def _toggle_connect(self):
        if self.source is not None:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        if self.src_cb.currentText() == SIM_LABEL:
            # gentle 20 s "growth oscillation" so intensity tracking has something to show
            self.source = SimRHEEDCamera(rpm=self.rpm_sp.value() or 10.0, osc_period=20.0, osc_amp=0.2)
            desc = "simulated RHEED source (20 s test oscillation; ⏱ t = 0 restarts it)"
        elif self.src_cb.currentText() == REPLAY_LABEL:
            path, _ = QFileDialog.getOpenFileName(self, "Replay a RHEED video as the live source", "",
                                                  "Videos (*.mp4 *.MP4 *.mov *.MOV *.avi *.AVI *.mkv);;All files (*)")
            if not path:
                return
            self.source = VideoReplayCamera(path, speed=1.0, loop=True)
            desc = f"replay of {os.path.basename(path)} (real time, loops)"
        else:
            idx = self.src_cb.currentData()
            if idx is None:
                self._log("Pick a camera (🔍 Scan) or the simulated source", OR); return
            w, h = (int(x) for x in self.res_cb.currentText().split("×"))
            fmt = None if self.fmt_cb.currentText() == "Auto" else self.fmt_cb.currentText()
            self.source = RHEEDCamera(index=idx, width=w, height=h, fourcc=fmt)
            desc = f"camera {idx} ({w}×{h}, {self.fmt_cb.currentText()})"
        self.source.start()
        self.track.set_source(self.source)
        self.rot = RotationCapture(self.source)
        self.rot.on_progress = lambda f, m: self._b.progress.emit(f, m)
        self.rot.on_done = lambda s, m: self._b.rot_done.emit(s, m)
        self.rec = VideoRecorder(self.source)
        self.rec.on_done = lambda p, m: self._b.rec_done.emit(p, m)
        self._last_idx = -1
        self._roi_placed = False
        self._log(f"Started {desc} — reconnects automatically if unplugged")
        self._update_buttons()

    def _disconnect(self):
        if self.rot and self.rot.busy:
            self.rot.abort()
        if self.rec and self.rec.busy:
            self.rec.stop()
        self.track.set_source(None)
        src, self.source = self.source, None
        if src:
            threading.Thread(target=src.stop, name="RHEED-stop", daemon=True).start()
        self._log("Camera released")
        self._update_buttons()

    def shutdown(self):
        """Call from the host window's closeEvent."""
        self._view_t.stop(); self._stat_t.stop()
        self.track.shutdown()
        if self.rot and self.rot.busy:
            self.rot.abort()
        if self.rec and self.rec.busy:
            self.rec.stop()
        if self.source:
            self.source.stop()
            self.source = None

    # ══════════════════════════════════════════════════════════════════
    # Timers (main thread)
    # ══════════════════════════════════════════════════════════════════
    def _refresh_view(self):
        # Hidden tab (another controller tab is in front) → don't render. Capture,
        # recording, rotation capture and intensity tracking keep running on their
        # threads; only the display work (BGR→RGB copy + ImageItem upload at 20 Hz)
        # is skipped, so the controller's GUI thread stays free for the hardware UI.
        if self.source is None or self.img_item is None or not self.isVisible():
            return
        fr = self.source.latest()
        if fr is None or fr.index == self._last_idx:
            return
        self._last_idx = fr.index
        img = fr.image
        step = 2 if img.shape[1] > 1600 else 1          # display only; captures stay full-res
        disp = img[::step, ::step, ::-1]                 # BGR → RGB
        lv = display_levels(disp) if self.auto_chk.isChecked() else (0, 255)
        self.img_item.setImage(np.ascontiguousarray(disp), autoLevels=False, levels=lv)
        self.img_item.setRect(0, 0, img.shape[1], img.shape[0])   # keep full-res pixel coords
        if not self._roi_placed:
            W, H = img.shape[1], img.shape[0]
            src = self.source
            if isinstance(src, RHEEDCamera) and (W, H) != (src.req_w, src.req_h):
                self._log(f"⚠ Asked camera {src.index} for {src.req_w}×{src.req_h}, it delivers {W}×{H} — "
                          f"wrong device (laptop webcam / EOS Webcam Utility?) or try Format MJPG", OR)
            self.track.place_box(W, H)
            self.roi.setPos([W * 0.15, H * 0.2]); self.roi.setSize([W * 0.7, H * 0.7])
            self.vb.autoRange(padding=0)
            self._roi_placed = True

    def _refresh_status(self):
        s = self.source
        if s is None:
            self.led.set_color(DIM); self.state_lbl.setText("Idle — not connected")
            self.state_lbl.setStyleSheet(f"color:{DIM};"); self.fps_lbl.setText("")
        else:
            if s.state == STATE_CONNECTED and s.dark:
                c, txt = OR, "Connected — image dark (no HDMI signal, camera asleep, or beam off)"
            elif s.state == STATE_CONNECTED:
                c, txt = GR, "Connected"
            elif s.state in (STATE_CONNECTING, STATE_IDLE):
                c, txt = YL, "Connecting…"
            else:
                c, txt = RD, f"Disconnected — {s.message}"
            self.led.set_color(c); self.state_lbl.setText(txt); self.state_lbl.setStyleSheet(f"color:{c};")
            w, h = s.resolution
            self.fps_lbl.setText(f"{w}×{h}  {s.fps:4.1f} fps" if s.connected else "")
            short = isinstance(s, RHEEDCamera) and s.connected and w and (w, h) != (s.req_w, s.req_h)
            self.fps_lbl.setStyleSheet(f"color:{OR if short else DIM};")
            self.fps_lbl.setToolTip(f"requested {s.req_w}×{s.req_h}" if isinstance(s, RHEEDCamera) else "")
        if self.rec and self.rec.busy:
            self.rec_lbl.setText(f"● REC  {time.time() - self.rec.t_start:5.1f} s  ({self.rec.frames} frames)")
            self.rec_lbl.setStyleSheet(f"color:{RD};")
        self._update_buttons()

    def _update_buttons(self):
        live = self.source is not None and self.source.connected
        self.conn_btn.setText("Disconnect" if self.source is not None else "Connect")
        for wdg in (self.src_cb, self.res_cb, self.fmt_cb):
            wdg.setEnabled(self.source is None)
        rot_busy = bool(self.rot and self.rot.busy)
        rec_busy = bool(self.rec and self.rec.busy)
        self.snap_btn.setEnabled(live)
        self.rec_btn.setEnabled(live or rec_busy)
        self.rec_btn.setText("⏹ Stop" if rec_busy else "⏺ Record")
        self.rot_btn.setEnabled(live or rot_busy)
        self.rot_btn.setText("✕ Abort" if rot_busy else "▶ Capture && Analyse")
        if hasattr(self, "track"):
            self.track._update()

    # ══════════════════════════════════════════════════════════════════
    # Actions
    # ══════════════════════════════════════════════════════════════════
    def _snapshot(self):
        p = save_snapshot(self.source, os.path.join(self.out_root, "snapshots"), self.sample_ed.text())
        if p:
            self._last_dir = os.path.dirname(p)
            self._log(f"📷 Snapshot → {p}", GR)
        else:
            self._log("Snapshot failed — no live frame", RD)

    def _toggle_record(self):
        if self.rec.busy:
            self.rec.stop(); return
        if self.rec.start(self.video_root, self.sample_ed.text()):
            self._log(f"⏺ Recording → {self.rec.path}", RD)
        else:
            self._log("Could not start recording", RD)
        self._update_buttons()

    def _on_rec_done(self, path, msg):
        self.rec_lbl.setText(""); self._update_buttons()
        if path:
            self._last_dir = os.path.dirname(path)
            self._log(f"⏹ Recording saved → {path}  ({msg})", GR)
        else:
            self._log(f"Recording failed: {msg}", RD)

    def _rpm_changed(self, rpm):
        if rpm > 0:                                   # suggest ≥ 2.5 revolutions
            need = 2.5 * 60.0 / rpm
            if self.dur_sp.value() < need:
                self.dur_sp.setValue(min(self.dur_sp.maximum(), round(need + 0.5)))
        if isinstance(self.source, SimRHEEDCamera) and rpm > 0:
            self.source.rpm = rpm

    def _roi_toggled(self, on):
        if self.roi is not None:
            self.roi.setVisible(on)

    def _current_roi(self):
        if not (self.roi_chk.isChecked() and self.roi is not None):
            return None
        (x, y), (w, h) = self.roi.pos(), self.roi.size()
        W, H = self.source.resolution if self.source else (0, 0)
        x0, y0 = max(0, int(x)), max(0, int(y))
        x1, y1 = min(W, int(x + w)), min(H, int(y + h))
        if x1 - x0 < 16 or y1 - y0 < 16:            # box dragged off the image
            self._log("ROI is outside the image — analysing the full frame", OR)
            return None
        return (x0, y0, x1 - x0, y1 - y0)

    def _toggle_rot(self):
        if self.rot.busy:
            self.rot.abort(); self._log("Aborting capture…", OR); return
        rpm = self.rpm_sp.value()
        ok = self.rot.start(self.out_root, duration_s=self.dur_sp.value(), sample=self.sample_ed.text(),
                            roi=self._current_roi(), expected_rpm=rpm, n_picks=self.npk_sp.value(),
                            keep_video=self.keep_chk.isChecked(), lossless=self.lossless_chk.isChecked())
        if ok:
            self.prog.setValue(0)
            self._log(f"▶ Rotation capture: {self.dur_sp.value():.0f} s clip, "
                      f"rpm={'auto' if rpm == 0 else rpm}")
        else:
            self._log("Rotation capture not started — camera not connected", RD)
        self._update_buttons()

    def _on_progress(self, f, msg):
        self.prog.setValue(int(f * 1000)); self.prog_lbl.setText(msg)

    def _on_rot_done(self, summary, msg):
        self._update_buttons()
        if summary is None:
            self.prog.setValue(0); self.prog_lbl.setText(msg)
            self._log(f"Rotation capture: {msg}", RD)
            return
        self.prog_lbl.setText("done")
        r = summary["result"]
        self._last_dir = summary["out_dir"]
        ok = r.period_s is not None
        col = GR if ok else OR
        lines = [f"<b style='color:{col}'>{'Rotation locked' if ok else 'No rotation found'}</b>"]
        if ok:
            lines.append(f"Period <b>{r.period_s:.3f} s</b> &nbsp;({r.rpm:.2f} rpm)")
            lines.append(f"Self-similarity {r.confidence:.2f} · {r.duration_s / r.period_s:.1f} revs"
                         + (f" · {r.fold}-fold" if r.fold > 1 else ""))
        lines.append(f"{r.n_frames} frames @ {r.fps:.1f} fps"
                     + (f", <span style='color:{OR}'>{summary['dropped']} dropped</span>" if summary["dropped"] else ""))
        lines.append(f"Saved {len(summary['files'])} azimuth(s) → <span style='color:{DIM}'>"
                     f"{os.path.basename(summary['out_dir'])}</span>")
        if r.warning:
            lines.append(f"<span style='color:{YL}'>⚠ {r.warning}</span>")
        self.res_lbl.setText("<br>".join(lines))
        self._log(f"✓ Rotation capture: {msg} → {summary['out_dir']}", col)
        self._show_thumbs(summary["files"])
        self._plot_result(r)

    def _show_thumbs(self, files):
        while self.thumb_l.count():
            it = self.thumb_l.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for f in files:
            path = f.get("avg") or f.get("best")
            if not path:
                continue
            box = QWidget(); bv = QVBoxLayout(box); bv.setContentsMargins(2, 2, 2, 2)
            pic = QLabel(); pic.setPixmap(QPixmap(path).scaledToHeight(150, Qt.SmoothTransformation))
            pic.setToolTip(path)
            ang = f["angle_deg"]
            cap = QLabel((f"{ang:.1f}°" if ang is not None else "static") +
                         (f"  (avg of {f['n_avg']})" if f.get("n_avg") else "") + f"  q={f['score']:.2f}")
            cap.setStyleSheet(f"color:{DIM};"); cap.setAlignment(Qt.AlignCenter)
            bv.addWidget(pic); bv.addWidget(cap); bv.addStretch(1)
            box.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
            self.thumb_l.addWidget(box)
        self.thumb_l.addStretch(1)

    def _plot_result(self, r):
        if not HAS_PG:
            return
        self.q_plot.clear(); self.l_plot.clear()
        if len(r.angles):
            self.q_plot.plot(r.angles, r.quality, pen=None, symbol="o", symbolSize=3,
                             symbolBrush=pg.mkColor(BL + "80"), symbolPen=None)
            self.q_plot.plot(r.bin_centers, r.bin_quality, pen=pg.mkPen(OR, width=2))
            for p in r.picks:
                self.q_plot.addItem(pg.InfiniteLine(p.angle_deg, angle=90, pen=pg.mkPen(GR, width=1, style=Qt.DashLine)))
        if len(r.lags):
            ok = ~np.isnan(r.lag_corr)
            self.l_plot.plot(r.lags[ok], r.lag_corr[ok], pen=pg.mkPen(PU, width=2))
            if r.period_s:
                self.l_plot.addItem(pg.InfiniteLine(r.period_s, angle=90, pen=pg.mkPen(GR, width=1, style=Qt.DashLine),
                                                    label=f"T = {r.period_s:.3f} s",
                                                    labelOpts={"color": GR, "position": 0.9}))

    def _open_folder(self):
        d = self._last_dir if os.path.isdir(self._last_dir) else self.out_root
        os.makedirs(d, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(d)
        else:
            subprocess.Popen(["xdg-open", d])
