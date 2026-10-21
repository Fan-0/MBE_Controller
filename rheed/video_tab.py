"""
RHEEDVideoPanel — the "🎞 Video analysis" mode of the RHEED tab: open an older /
recorded RHEED video (Canon .MP4/.MOV, our .avi recordings, …), scrub through it, pick
the analysis range and ROI, and run the rotation-sync analysis offline
(video_analysis.VideoAnalysis). No camera needed.

Threading: the viewer decodes on the MAIN thread with its own cv2.VideoCapture
(one frame per timer tick / debounced seek — a 1080p H.264 frame is a few ms). The
analysis runs on the worker thread; its callbacks come back through _Bridge signals.

VIEW modes ("View:" combo) — show the video the way the algorithm sees it, using the
SAME functions as rotation.py (make_thumb / streak_profile / correlation_input):
  Original          the decoded frame (Auto contrast = display stretch only)
  CV 1 · thumbnail  grey, ROI-cropped, 128 px wide — the per-frame input (live: current
                    ROI box; works before any analysis)
  CV 2 · − mean     CV 1 of the nearest ANALYSED frame minus the clip's mean thumbnail
  CV 3 · drift-removed   … then temporally high-passed = the correlation input
  CV 2/3 need a finished analysis; signed images are shown with symmetric per-frame
  levels (grey = 0), which is what the per-frame L2 normalisation sees.
  "Streak profile" adds the profile across the streaks (blue) and its running mean
  (dashed); quality q = std(blue − dashed). It sits beside the image (horizontal
  streaks) or under it (vertical), axis-linked to the image.
CURSORS: the yellow line on 📉 Quality vs time and on 📈 Quality vs azimuth can be
  DRAGGED; the viewer follows (for azimuth: the frame at that angle in the revolution
  nearest the current time). A readout shows t / azimuth / q of the current frame.
"""

import os
import subprocess
import sys
import time
from typing import Callable, Optional

import numpy as np
from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFrame,
                             QGridLayout, QHBoxLayout, QLabel, QLineEdit, QProgressBar,
                             QScrollArea, QSizePolicy, QSlider, QSpinBox, QSplitter,
                             QTabWidget, QVBoxLayout, QWidget)

try:
    import pyqtgraph as pg
    HAS_PG = True
except ImportError:
    HAS_PG = False

try:
    import cv2
except ImportError:
    cv2 = None

from .ui_common import (BG, PBG, BDR, BL, GR, RD, OR, PU, FG, DIM, YL, TABS_CSS,
                        _ClickLabel, _btn, _group, display_levels, np_to_pixmap)
from .rotation import correlation_input, make_thumb, streak_profile
from .video_analysis import VIDEO_EXTS, VideoAnalysis, enhance, probe_video
from .video_steps import ExportStep, SpatialStep

PLAY_MS = 50                 # viewer playback tick
SEEK_FWD_MAX = 45            # forward jumps up to this many frames: grab() instead of seek
VIEW_MODES = ["Original", "CV 1 · grey thumbnail (ROI, 128 px)",
              "CV 2 · minus clip mean", "CV 3 · drift-removed (correlation input)"]


class _Bridge(QObject):
    progress = pyqtSignal(float, str)
    done = pyqtSignal(object, str)


class RHEEDVideoPanel(QWidget):
    def __init__(self, log_fn: Optional[Callable[[str], None]] = None,
                 out_root: str = "", parent=None):
        super().__init__(parent)
        self._log_fn = log_fn
        self.out_root = out_root
        self.info = None                    # VideoInfo of the loaded file
        self._cap = None                    # viewer's own VideoCapture (main thread)
        self._pos = -1                      # frame number currently shown
        self._cap_next = 0                  # frame the viewer capture returns on read()
        self._img, self._img_pos = None, -1 # last decoded frame (BGR) and its number
        self._cv_cache = {}                 # view mode → (n, h, w) stack from the analysis
        self._seek_to = 0
        self._last_dir = out_root
        self._last_open_dir = ""
        self._summary = None
        self.va = VideoAnalysis()
        self._b = _Bridge()
        self._b.progress.connect(self._on_progress)
        self._b.done.connect(self._on_done)
        self.va.on_progress = lambda f, m: self._b.progress.emit(f, m)
        self.va.on_done = lambda s, m: self._b.done.emit(s, m)
        self._build()
        self._seek_t = QTimer(self); self._seek_t.setSingleShot(True)
        self._seek_t.timeout.connect(lambda: self._show_frame(self._seek_to))
        self._play_t = QTimer(self); self._play_t.timeout.connect(self._play_tick)
        self.setAcceptDrops(True)
        self._update_buttons()

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
        root.addWidget(self._file_bar())
        split = QSplitter(Qt.Horizontal)
        left = QSplitter(Qt.Vertical)
        left.addWidget(self._view_panel())
        left.addWidget(self._results_panel())
        left.setStretchFactor(0, 3); left.setStretchFactor(1, 2)
        split.addWidget(left)
        split.addWidget(self._control_panel())
        split.setStretchFactor(0, 4); split.setStretchFactor(1, 1)
        root.addWidget(split, 1)
        self.log_lbl = QLabel("Open a RHEED video (or drop one here) to analyse it offline.")
        self.log_lbl.setStyleSheet(f"color:{DIM};font-family:'Courier New';")
        self.log_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self.log_lbl)
        self._reset_time_plot()                  # needs the range spinboxes → after all panels

    def _file_bar(self):
        f = QFrame(); f.setStyleSheet(f"QFrame{{background:{PBG};border:1px solid {BDR};border-radius:5px;}}"
                                      f"QLabel{{border:none;}}")
        h = QHBoxLayout(f); h.setContentsMargins(8, 4, 8, 4); h.setSpacing(8)
        self.open_vid_btn = _btn("📂 Open video…", GR); self.open_vid_btn.clicked.connect(self._browse)
        h.addWidget(self.open_vid_btn)
        self.file_lbl = QLabel("no video loaded"); self.file_lbl.setStyleSheet(f"color:{DIM};")
        h.addWidget(self.file_lbl, 1)
        self.info_lbl = QLabel(""); self.info_lbl.setStyleSheet(f"color:{DIM};")
        h.addWidget(self.info_lbl)
        return f

    def _view_panel(self):
        w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(4)
        if HAS_PG:
            self.glw = glw = pg.GraphicsLayoutWidget(); glw.setBackground(BG)
            self.vb = glw.addViewBox(row=0, col=0, lockAspect=True, invertY=True, enableMenu=False)
            self.img_item = pg.ImageItem(axisOrder="row-major")
            self.vb.addItem(self.img_item)
            self.roi = pg.RectROI([100, 100], [400, 300], pen=pg.mkPen(YL, width=2))
            self.roi.setVisible(False)
            self.roi.sigRegionChangeFinished.connect(lambda _: self._render())
            self.vb.addItem(self.roi)
            # streak-profile plots, axis-linked to the image; only one is in the layout
            # (no axes/titles on the linked side, or the plot is shorter than the image
            # and the rows no longer line up; q is a TextItem instead)
            self.prof_h = pg.PlotItem(); self.prof_h.setYLink(self.vb); self.prof_h.invertY(True)
            self.prof_h.hideAxis("left"); self.prof_h.hideAxis("bottom"); self.prof_h.setMaximumWidth(200)
            self.prof_h.vb.enableAutoRange(axis="y", enable=False)
            self.prof_v = pg.PlotItem(); self.prof_v.setXLink(self.vb)
            self.prof_v.hideAxis("bottom"); self.prof_v.hideAxis("left"); self.prof_v.setMaximumHeight(140)
            self.prof_v.vb.enableAutoRange(axis="x", enable=False)
            for p in (self.prof_h, self.prof_v):
                p.q_txt = pg.TextItem(color=YL, anchor=(0, 0))
                p.q_txt.setParentItem(p.vb)               # fixed in the corner, not in data coords
                p.q_txt.setPos(4, 2)
            self._prof_shown = None
            v.addWidget(glw, 1)
        else:
            self.img_item = self.roi = None
            v.addWidget(QLabel("pyqtgraph not installed"), 1)
        t = QHBoxLayout(); t.setSpacing(6)
        self.play_btn = _btn("▶", GR, 40); self.play_btn.clicked.connect(self._toggle_play)
        t.addWidget(self.play_btn)
        self.slider = QSlider(Qt.Horizontal); self.slider.setRange(0, 0)
        self.slider.valueChanged.connect(self._slider_moved)
        t.addWidget(self.slider, 1)
        self.time_lbl = QLabel("0.0 / 0.0 s"); self.time_lbl.setMinimumWidth(120)
        self.time_lbl.setStyleSheet(f"color:{DIM};font-family:'Courier New';")
        t.addWidget(self.time_lbl)
        self.set_start_btn = _btn("⇤ Start here", DIM); self.set_start_btn.clicked.connect(lambda: self._set_range_edge(0))
        self.set_end_btn = _btn("End here ⇥", DIM); self.set_end_btn.clicked.connect(lambda: self._set_range_edge(1))
        t.addWidget(self.set_start_btn); t.addWidget(self.set_end_btn)
        v.addLayout(t)

        t = QHBoxLayout(); t.setSpacing(6)
        t.addWidget(QLabel("👁 View:"))
        self.view_cb = QComboBox(); self.view_cb.addItems(VIEW_MODES)
        self.view_cb.setToolTip("See the video the way the algorithm does.\n"
                                "CV 1: per-frame input (grey, ROI, 128 px) — live, before analysis too.\n"
                                "CV 2: minus the clip's mean thumbnail (static screen/shadow removed).\n"
                                "CV 3: + slow drift removed = what is correlated to find the period.\n"
                                "CV 2/3 show the nearest ANALYSED frame (needs a finished analysis).")
        self.view_cb.currentIndexChanged.connect(lambda _: self._render())
        t.addWidget(self.view_cb)
        self.prof_chk = QCheckBox("Streak profile")
        self.prof_chk.setToolTip("Profile across the streaks (blue) and its running mean (dashed);\n"
                                 "streak contrast q = std(blue − dashed)")
        self.prof_chk.toggled.connect(lambda _: self._render())
        t.addWidget(self.prof_chk)
        self.auto_chk = QCheckBox("Auto contrast"); self.auto_chk.setChecked(True)
        self.auto_chk.setToolTip("Display only — stretch to the frame's 0.5–99.9 % grey levels")
        self.auto_chk.toggled.connect(lambda _: self._render())
        t.addWidget(self.auto_chk)
        self.cur_lbl = QLabel(""); self.cur_lbl.setStyleSheet(f"color:{YL};font-family:'Courier New';")
        t.addWidget(self.cur_lbl, 1, Qt.AlignRight)
        v.addLayout(t)
        return w

    def _results_panel(self):
        tabs = QTabWidget(); tabs.setStyleSheet(TABS_CSS)
        sc = QScrollArea(); sc.setWidgetResizable(True); sc.setStyleSheet(f"background:{PBG};border:none;")
        self.thumb_w = QWidget(); self.thumb_l = QHBoxLayout(self.thumb_w); self.thumb_l.setAlignment(Qt.AlignLeft)
        sc.setWidget(self.thumb_w)
        if HAS_PG:
            pg.setConfigOption("background", BG); pg.setConfigOption("foreground", FG)
            self.t_plot = pg.PlotWidget(); self.t_plot.showGrid(x=True, y=True, alpha=0.25)
            self.t_plot.setLabel("bottom", "Video time (s)"); self.t_plot.setLabel("left", "Streak contrast")
            self.region = pg.LinearRegionItem([0, 1], brush=pg.mkBrush(BL + "22"),
                                              pen=pg.mkPen(BL, width=1))
            self.region.setZValue(-10)
            self.region.movable = False      # body not draggable (edges still are) — drags
                                             # inside the range are for the yellow cursor
            self.region.sigRegionChangeFinished.connect(self._region_changed)
            self.pos_line = pg.InfiniteLine(0, angle=90, movable=True, pen=pg.mkPen(YL, width=2),
                                            hoverPen=pg.mkPen(YL, width=5))
            self.pos_line.setZValue(20)
            self.pos_line.sigDragged.connect(lambda ln: self._cursor_seek(ln.value()))
            self.t_plot.scene().sigMouseClicked.connect(self._plot_clicked)
            tabs.addTab(self.t_plot, "📉 Quality vs time")
            tabs.addTab(sc, "🖼 Saved azimuths")
            self.q_plot = pg.PlotWidget(); self.q_plot.showGrid(x=True, y=True, alpha=0.25)
            self.q_plot.setLabel("bottom", "Azimuth (° from analysis start)"); self.q_plot.setLabel("left", "Streak contrast")
            self.q_plot.setXRange(0, 360)
            self.az_line = pg.InfiniteLine(0, angle=90, movable=True, bounds=(0, 360),
                                           pen=pg.mkPen(YL, width=2), hoverPen=pg.mkPen(YL, width=5))
            self.az_line.setZValue(20)
            self.az_line.sigDragged.connect(lambda ln: (self._azimuth_seek(ln.value()),
                                                        self.spatial.cursor_azimuth_moved()))
            tabs.addTab(self.q_plot, "📈 Quality vs azimuth")
            self.l_plot = pg.PlotWidget(); self.l_plot.showGrid(x=True, y=True, alpha=0.25)
            self.l_plot.setLabel("bottom", "Time lag (s)"); self.l_plot.setLabel("left", "Self-similarity")
            tabs.addTab(self.l_plot, "🔁 Self-similarity vs lag")
        else:
            tabs.addTab(sc, "🖼 Saved azimuths")
        self.res_tabs = tabs
        return tabs

    def _control_panel(self):
        """Right column = the three steps: ① Import & analyse · ② Spatial · ③ Export."""
        self.steps = QTabWidget(); self.steps.setStyleSheet(TABS_CSS)
        self.steps.setMinimumWidth(390); self.steps.setMaximumWidth(480)

        def scrolled(widget):
            sc = QScrollArea(); sc.setWidgetResizable(True); sc.setFrameShape(QFrame.NoFrame)
            sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            sc.setStyleSheet(f"QScrollArea{{background:{BG};border:none;}}")
            sc.setWidget(widget)
            return sc
        self.steps.addTab(scrolled(self._import_step()), "① Import && analyse")
        self.spatial = SpatialStep(self)
        self.steps.addTab(scrolled(self.spatial), "② Spatial")
        self.exporter = ExportStep(self)
        self.steps.addTab(scrolled(self.exporter), "③ Export")
        self.steps.currentChanged.connect(self._step_changed)
        return self.steps

    def _step_changed(self, i):
        """Show the result tab that goes with the step."""
        if not HAS_PG:
            return
        if i == 1:
            self.res_tabs.setCurrentWidget(self.spatial.prof_plot)
        elif i == 0:
            self.res_tabs.setCurrentWidget(self.t_plot)

    def _import_step(self):
        w = QWidget()
        v = QVBoxLayout(w); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(8)

        g = _group("Analysis range"); gl = QGridLayout(g)
        gl.addWidget(QLabel("Start (s):"), 0, 0)
        self.start_sp = QDoubleSpinBox(); self.start_sp.setDecimals(1); self.start_sp.setRange(0, 0)
        gl.addWidget(self.start_sp, 0, 1)
        gl.addWidget(QLabel("End (s):"), 1, 0)
        self.end_sp = QDoubleSpinBox(); self.end_sp.setDecimals(1); self.end_sp.setRange(0, 0)
        gl.addWidget(self.end_sp, 1, 1)
        self.start_sp.valueChanged.connect(self._range_spins_changed)
        self.end_sp.valueChanged.connect(self._range_spins_changed)
        self.whole_btn = _btn("Whole video", DIM); self.whole_btn.clicked.connect(self._whole_range)
        gl.addWidget(self.whole_btn, 2, 0, 1, 2)
        gl.setColumnStretch(1, 1)
        v.addWidget(g)

        g = _group("Rotation analysis"); gl = QGridLayout(g); r = 0
        gl.addWidget(QLabel("Sample:"), r, 0)
        self.sample_ed = QLineEdit(); self.sample_ed.setPlaceholderText("defaults to the file name")
        gl.addWidget(self.sample_ed, r, 1); r += 1
        gl.addWidget(QLabel("Rotation (rpm):"), r, 0)
        self.rpm_sp = QDoubleSpinBox(); self.rpm_sp.setRange(0, 120); self.rpm_sp.setDecimals(2)
        self.rpm_sp.setSpecialValueText("auto")
        self.rpm_sp.setToolTip("0 = detect automatically. Entering the rpm used in the run narrows\n"
                               "the search and avoids locking onto the pattern's symmetry sub-period.\n"
                               "The range must hold ≥ 2 revolutions either way.")
        gl.addWidget(self.rpm_sp, r, 1); r += 1
        gl.addWidget(QLabel("Streaks run:"), r, 0)
        self.orient_cb = QComboBox(); self.orient_cb.addItems(["Auto", "Vertical", "Horizontal"])
        self.orient_cb.setToolTip("Direction of the streaks in the image (depends on how the camera is\n"
                                  "mounted). Auto picks the one whose contrast swings with rotation.")
        gl.addWidget(self.orient_cb, r, 1); r += 1
        gl.addWidget(QLabel("Analysis rate (fps):"), r, 0)
        self.afps_sp = QDoubleSpinBox(); self.afps_sp.setRange(0.5, 60); self.afps_sp.setDecimals(1); self.afps_sp.setValue(10)
        self.afps_sp.setToolTip("Frames per second used to FIND the period/azimuths (≤ 3000 frames total).\n"
                                "Extraction/averaging always uses every frame of the file.")
        gl.addWidget(self.afps_sp, r, 1); r += 1
        gl.addWidget(QLabel("Azimuths to keep:"), r, 0)
        self.npk_sp = QSpinBox(); self.npk_sp.setRange(1, 12); self.npk_sp.setValue(4)
        gl.addWidget(self.npk_sp, r, 1); r += 1
        gl.addWidget(QLabel("Average ± (°):"), r, 0)
        self.avg_sp = QDoubleSpinBox(); self.avg_sp.setRange(0.1, 10); self.avg_sp.setDecimals(1); self.avg_sp.setValue(1.0)
        self.avg_sp.setToolTip("Every frame within ± this azimuth of a kept direction (any revolution)\n"
                               "is averaged into its image — more frames, less noise, more blur.")
        gl.addWidget(self.avg_sp, r, 1); r += 1
        self.roi_chk = QCheckBox("Analyse ROI only (yellow box)")
        self.roi_chk.setToolTip("Restrict the analysis to the box — e.g. just the phosphor screen.")
        self.roi_chk.toggled.connect(lambda on: (self.roi is not None and self.roi.setVisible(on),
                                                 self._render()))
        gl.addWidget(self.roi_chk, r, 0, 1, 2); r += 1
        self.run_btn = _btn("▶ Analyse video", GR); self.run_btn.clicked.connect(self._toggle_run)
        gl.addWidget(self.run_btn, r, 0, 1, 2); r += 1
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
        v.addWidget(g)
        v.addStretch(1)
        return w

    # ══════════════════════════════════════════════════════════════════
    # Loading / viewing
    # ══════════════════════════════════════════════════════════════════
    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open RHEED video", self._last_open_dir,
                                              f"Videos ({VIDEO_EXTS});;All files (*)")
        if path:
            self.load(path)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        urls = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if urls:
            self.load(urls[0])

    def load(self, path: str) -> bool:
        if self.va.busy:
            self._log("Analysis running — abort it before opening another video", OR); return False
        if cv2 is None:
            self._log("OpenCV not installed — cannot read video files", RD); return False
        info = probe_video(path)
        if info is None:
            self._log(f"Cannot open video: {path}", RD); return False
        self._stop_play()
        if self._cap is not None:
            self._cap.release()
        self._cap = cv2.VideoCapture(path)
        self.info, self._pos, self._cap_next = info, -1, 0
        self._img, self._img_pos = None, -1
        self._summary, self._cv_cache = None, {}
        self.spatial.set_summary(None); self.exporter.set_enabled(False)
        self.exporter.name_ed.clear()
        self._last_open_dir = os.path.dirname(path)
        self.file_lbl.setText(os.path.basename(path)); self.file_lbl.setToolTip(path)
        self.file_lbl.setStyleSheet(f"color:{FG};")
        self.info_lbl.setText(f"{info.width}×{info.height} · {info.fps:.2f} fps · "
                              f"{info.duration_s:.1f} s · {info.n_frames} frames · {info.codec}")
        self.sample_ed.setPlaceholderText(os.path.splitext(os.path.basename(path))[0])
        dur = round(info.duration_s, 1)
        for sp in (self.start_sp, self.end_sp):
            sp.blockSignals(True); sp.setRange(0, dur); sp.blockSignals(False)
        self.slider.blockSignals(True)
        self.slider.setRange(0, info.n_frames - 1); self.slider.setValue(0)
        self.slider.blockSignals(False)
        self._whole_range()
        self._reset_time_plot()
        if self.roi is not None:
            W, H = info.width, info.height
            self.roi.setPos([W * 0.15, H * 0.1]); self.roi.setSize([W * 0.5, H * 0.8])
        self._show_frame(0)
        if self.roi is not None:
            self.vb.setRange(xRange=(0, info.width), yRange=(0, info.height), padding=0)
        self._log(f"Loaded {path}")
        self._update_buttons()
        return True

    def _read(self, fn: int):
        """Frame fn from the viewer's capture. Short forward jumps grab() through the
        frames (cheaper than an H.264 seek, which decodes from the last keyframe)."""
        d = fn - self._cap_next
        if d < 0 or d > SEEK_FWD_MAX:
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, fn)
        else:
            for _ in range(d):
                self._cap.grab()
        ok, img = self._cap.read()
        self._cap_next = fn + 1 if ok else -10**9     # unknown position → seek next time
        return img if ok else None

    def _frame_image(self):
        """Decoded image of the current frame, decoding lazily (CV 2/3 never need it)."""
        if self._img_pos != self._pos:
            img = self._read(self._pos)
            if img is None:
                return None
            self._img, self._img_pos = img, self._pos
        return self._img

    def _show_frame(self, fn: int):
        if self.info is None or self._cap is None:
            return
        fn = int(max(0, min(fn, self.info.n_frames - 1)))
        if fn == self._pos:
            return
        self._pos = fn
        if not self._render():
            self._stop_play()
        t = fn / self.info.fps
        self.time_lbl.setText(f"{t:6.1f} / {self.info.duration_s:.1f} s")
        self.spatial.on_frame(t)
        if HAS_PG:
            self.pos_line.setValue(t)
        if self.slider.value() != fn:
            self.slider.blockSignals(True); self.slider.setValue(fn); self.slider.blockSignals(False)

    # ── rendering (view modes) ──
    def _nearest_analysed(self):
        """Index into the analysis arrays of the analysed frame nearest the current one,
        or None (no analysis / current frame outside the analysed range)."""
        s = self._summary
        if s is None:
            return None
        fn = np.asarray(s["frame_numbers"])
        if self._pos < fn[0] - s["step"] or self._pos > fn[-1] + s["step"]:
            return None
        k = int(np.searchsorted(fn, self._pos))
        if k > 0 and (k == len(fn) or self._pos - fn[k - 1] <= fn[k] - self._pos):
            k -= 1
        return k

    def _cv_stack(self, mode):
        """(n, h, w) stage-2/3 stack of the last analysis, computed once and cached."""
        if mode not in self._cv_cache:
            s = self._summary; th = s["thumbs"]
            det = s["result"].detrend_s if mode == 3 else 0.0
            self._cv_cache[mode] = correlation_input(s["times"], th, det).reshape(th.shape)
        return self._cv_cache[mode]

    def _orientation(self):
        if self._summary is not None:
            return self._summary["result"].orientation
        o = self.orient_cb.currentText().lower()
        return o if o in ("vertical", "horizontal") else "vertical"

    def _render(self) -> bool:
        """Draw the current frame in the selected view mode. False = decode failed."""
        if self.info is None or self.img_item is None or self._pos < 0:
            return True
        mode = self.view_cb.currentIndex()
        W, H = self.info.width, self.info.height
        thumb, rect, note = None, (0, 0, W, H), ""
        if mode <= 1:
            img = self._frame_image()
            if img is None:
                return False
            roi = self._current_roi(quiet=True)
            thumb = make_thumb(img, roi)               # the exact per-frame algorithm input
            if roi:
                rect = roi                             # where the thumbnail (and profile) sit
            if mode == 0:
                step = 2 if img.shape[1] > 1600 else 1
                disp = np.ascontiguousarray(img[::step, ::step, ::-1])
                lv = display_levels(disp) if self.auto_chk.isChecked() else (0, 255)
                self.img_item.setImage(disp, autoLevels=False, levels=lv)
                self.img_item.setRect(0, 0, W, H)
                self._draw_profile(thumb, rect)
                self._note = ""
                self._update_readout()
                return True
            disp = thumb
            lv = display_levels(disp) if self.auto_chk.isChecked() else (0, 255)
        else:
            k = self._nearest_analysed()
            if k is None:
                self.img_item.clear()
                self._note = "CV 2/3: run an analysis covering this time first"
                self._draw_profile(None, rect)
                self._update_readout()
                return True
            s = self._summary
            thumb = s["thumbs"][k]
            disp = self._cv_stack(mode)[k]
            a = float(np.percentile(np.abs(disp), 99.5)) or 1.0
            lv = (-a, a)                                # symmetric: mid-grey = 0
            if s["roi"]:
                rect = s["roi"]
            note = f"analysed frame @ {s['times'][k]:.2f} s"
        self.img_item.setImage(disp, autoLevels=False, levels=lv)
        self.img_item.setRect(*rect)
        self._draw_profile(thumb, rect)
        self._note = note
        self._update_readout()
        return True

    def _draw_profile(self, thumb, rect):
        if not HAS_PG:
            return
        want = self._orientation() if (self.prof_chk.isChecked() and thumb is not None) else None
        if want != self._prof_shown:
            for p in (self.prof_h, self.prof_v):
                if p.scene() is not None:
                    self.glw.ci.removeItem(p)
            if want == "horizontal":
                self.glw.ci.addItem(self.prof_h, row=0, col=1)
            elif want == "vertical":
                self.glw.ci.addItem(self.prof_v, row=1, col=0)
            self._prof_shown = want
        if want is None:
            return
        prof, smooth = streak_profile(thumb, want)
        q = float((prof - smooth).std())
        x0, y0, w, h = rect
        if want == "horizontal":                       # profile along y, beside the image
            pos = y0 + (np.arange(len(prof)) + 0.5) * h / len(prof)
            p = self.prof_h; p.clear()
            p.plot(prof, pos, pen=pg.mkPen(BL, width=2))
            p.plot(smooth, pos, pen=pg.mkPen(DIM, width=1, style=Qt.DashLine))
        else:                                           # profile along x, under the image
            pos = x0 + (np.arange(len(prof)) + 0.5) * w / len(prof)
            p = self.prof_v; p.clear()
            p.plot(pos, prof, pen=pg.mkPen(BL, width=2))
            p.plot(pos, smooth, pen=pg.mkPen(DIM, width=1, style=Qt.DashLine))
        p.q_txt.setText(f"q = {q:.3f}")

    def _azimuth_of(self, t):
        s = self._summary
        if s is None or not s["result"].period_s:
            return None
        T = s["result"].period_s
        return ((t - s["t0"]) % T) / T * 360.0

    def _update_readout(self):
        if self.info is None:
            return
        t = self._pos / self.info.fps
        parts = [f"t {t:6.2f} s", f"frame {self._pos}"]
        az = self._azimuth_of(t)
        if az is not None:
            parts.append(f"az {az:5.1f}°")
            if HAS_PG:
                self.az_line.setValue(az)
        k = self._nearest_analysed()
        if k is not None:
            parts.append(f"q {self._summary['result'].quality[k]:.3f}")
        note = getattr(self, "_note", "")
        if self.view_cb.currentIndex() >= 2 and note:
            parts.append(note)
        self.cur_lbl.setText("  ·  ".join(parts))

    # ── cursors on the result plots ──
    def _cursor_seek(self, t: float):
        """Yellow line dragged on Quality vs time → follow in the viewer."""
        if self.info:
            self._stop_play()
            self._seek_to = int(round(max(0.0, t) * self.info.fps))
            self._seek_t.start(15)

    def _azimuth_seek(self, az: float):
        """Yellow line dragged on Quality vs azimuth → the frame at that azimuth, in
        the revolution nearest the current viewer time."""
        s = self._summary
        if self.info is None or s is None or not s["result"].period_s:
            return
        T, t0 = s["result"].period_s, s["t0"]
        t_now = self._pos / self.info.fps
        k = round((t_now - t0) / T - az / 360.0)
        t = t0 + (az / 360.0 + k) * T
        lo, hi = s["times"][0], s["times"][-1]
        if t < lo:
            t += T
        if t > hi and t - T >= lo:
            t -= T
        self._cursor_seek(t)

    def seek_time(self, t: float):
        if self.info:
            self._stop_play()
            self._show_frame(int(round(t * self.info.fps)))

    def _slider_moved(self, v):
        self._seek_to = v
        self._seek_t.start(40)                       # debounce: seeking decodes from a keyframe

    def _toggle_play(self):
        if self._play_t.isActive():
            self._stop_play()
        elif self.info:
            if self._pos >= self.info.n_frames - 1:
                self._show_frame(0)
            self._play_t.start(PLAY_MS); self.play_btn.setText("⏸")

    def _stop_play(self):
        self._play_t.stop(); self.play_btn.setText("▶")

    def _play_tick(self):
        if self.info is None:
            return self._stop_play()
        adv = max(1, int(round(self.info.fps * PLAY_MS / 1000)))   # _read grab()s the gap
        if self._pos + adv >= self.info.n_frames:
            return self._stop_play()
        self._show_frame(self._pos + adv)

    # ── range ──
    def _whole_range(self):
        if not self.info:
            return
        self.start_sp.setValue(0); self.end_sp.setValue(round(self.info.duration_s, 1))

    def _set_range_edge(self, which):
        if self.info and self._pos >= 0:
            t = round(self._pos / self.info.fps, 1)
            (self.start_sp if which == 0 else self.end_sp).setValue(t)

    def _range_spins_changed(self, _=None):
        if HAS_PG:
            self.region.blockSignals(True)
            self.region.setRegion([self.start_sp.value(), self.end_sp.value()])
            self.region.blockSignals(False)

    def _region_changed(self):
        a, b = self.region.getRegion()
        for sp, val in ((self.start_sp, a), (self.end_sp, b)):
            sp.blockSignals(True); sp.setValue(max(0.0, val)); sp.blockSignals(False)

    def _plot_clicked(self, ev):
        if ev.button() != Qt.LeftButton or self.info is None:
            return
        vb = self.t_plot.getPlotItem().vb
        if vb.sceneBoundingRect().contains(ev.scenePos()):
            self.seek_time(vb.mapSceneToView(ev.scenePos()).x())

    # ══════════════════════════════════════════════════════════════════
    # Analysis
    # ══════════════════════════════════════════════════════════════════
    def _current_roi(self, quiet: bool = False):
        if not (self.roi_chk.isChecked() and self.roi is not None and self.info):
            return None
        (x, y), (w, h) = self.roi.pos(), self.roi.size()
        x0, y0 = max(0, int(x)), max(0, int(y))
        x1, y1 = min(self.info.width, int(x + w)), min(self.info.height, int(y + h))
        if x1 - x0 < 16 or y1 - y0 < 16:
            if not quiet:
                self._log("ROI is outside the image — analysing the full frame", OR)
            return None
        return (x0, y0, x1 - x0, y1 - y0)

    def _toggle_run(self):
        if self.va.busy:
            self.va.abort(); self._log("Aborting analysis…", OR); return
        if not self.info:
            self._log("Open a video first", OR); return
        start, end = self.start_sp.value(), self.end_sp.value()
        if end - start < 2:
            self._log("Analysis range is too short", RD); return
        rpm = self.rpm_sp.value()
        if rpm > 0 and (end - start) < 2 * 60 / rpm:
            self._log(f"⚠ Range {end - start:.0f} s < 2 revolutions at {rpm:g} rpm "
                      f"({120 / rpm:.0f} s) — the period may not be resolvable", OR)
        ok = self.va.start(self.info.path, start_s=start, end_s=end,
                           analysis_fps=self.afps_sp.value(), roi=self._current_roi(),
                           expected_rpm=rpm, n_picks=self.npk_sp.value(),
                           orientation=self.orient_cb.currentText().lower(),
                           avg_deg=self.avg_sp.value(), band=self.spatial.band())
        if ok:
            self.prog.setValue(0)
            self._log(f"▶ Video analysis: {os.path.basename(self.info.path)} {start:.1f}–{end:.1f} s, "
                      f"rpm={'auto' if rpm == 0 else rpm}")
        self._update_buttons()

    def _on_progress(self, f, msg):
        self.prog.setValue(int(f * 1000)); self.prog_lbl.setText(msg)

    def _on_done(self, summary, msg):
        self._update_buttons()
        if summary is None:
            self.prog.setValue(0); self.prog_lbl.setText(msg)
            self._log(f"Video analysis: {msg}", RD if "abort" not in msg else OR)
            return
        self._summary, self._cv_cache = summary, {}
        self.prog_lbl.setText("done")
        r = summary["result"]
        ok = r.period_s is not None
        col = GR if ok else OR
        lines = [f"<b style='color:{col}'>{'Rotation locked' if ok else 'No rotation found'}</b>"]
        if ok:
            lines.append(f"Period <b>{r.period_s:.2f} s</b> &nbsp;({r.rpm:.3f} rpm)")
            lines.append(f"Self-similarity {r.confidence:.2f} · {r.duration_s / r.period_s:.1f} revs"
                         + (f" · {r.fold}-fold" if r.fold > 1 else ""))
        else:
            lines.append(f"<span style='color:{DIM}'>{r.message}</span>")
        lines.append(f"{r.n_frames} frames analysed @ {r.fps:.1f} fps · streaks {r.orientation}")
        lines.append(f"{len(summary['picks'])} azimuth(s) kept · {len(summary['profiles'].times)} "
                     f"profiles · <span style='color:{DIM}'>in memory — ③ Export to save</span>")
        if r.warning:
            lines.append(f"<span style='color:{YL}'>⚠ {r.warning}</span>")
        self.res_lbl.setText("<br>".join(lines))
        self._log(f"✓ Video analysis: {msg} (nothing written — use ③ Export)", col)
        self._show_thumbs(summary["picks"])
        self._plot_result(summary)
        self._render()                           # CV 2/3 + readout now have data
        self.spatial.set_summary(summary)
        self.exporter.set_enabled(True)

    def _show_thumbs(self, picks):
        while self.thumb_l.count():
            it = self.thumb_l.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for f in picks:
            img = f["avg"] if f.get("avg") is not None else f.get("best")
            if img is None:
                continue
            box = QWidget(); bv = QVBoxLayout(box); bv.setContentsMargins(2, 2, 2, 2)
            pic = _ClickLabel()
            pic.setPixmap(np_to_pixmap(enhance(img)).scaledToHeight(150, Qt.SmoothTransformation))
            pic.setToolTip(f"contrast-enhanced {'average' if f.get('avg') is not None else 'best frame'}\n"
                           f"click: show the best frame ({f['best_time_s']:.1f} s) in the viewer")
            pic.setCursor(Qt.PointingHandCursor)
            pic.clicked.connect(lambda t=f["best_time_s"]: self.seek_time(t))
            ang = f["angle_deg"]
            cap = QLabel((f"{ang:.1f}°" if ang is not None else "static") +
                         (f"  (avg of {f['n_avg']})" if f.get("n_avg") else "") +
                         f"  q={f['score']:.2f}  @{f['best_time_s']:.1f}s")
            cap.setStyleSheet(f"color:{DIM};"); cap.setAlignment(Qt.AlignCenter)
            bv.addWidget(pic); bv.addWidget(cap); bv.addStretch(1)
            box.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
            self.thumb_l.addWidget(box)
        self.thumb_l.addStretch(1)

    def _reset_time_plot(self):
        if not HAS_PG:
            return
        self.t_plot.clear()
        self.t_plot.addItem(self.region); self.t_plot.addItem(self.pos_line)
        self._range_spins_changed()
        if self.info:
            self.t_plot.setXRange(0, self.info.duration_s, padding=0.01)

    def _plot_result(self, summary):
        if not HAS_PG:
            return
        r, t0 = summary["result"], summary["t0"]
        self._reset_time_plot()
        self.t_plot.plot(t0 + r.times, r.quality, pen=pg.mkPen(BL, width=1.5))
        for f in summary["picks"]:
            lbl = f"{f['angle_deg']:.0f}°" if f["angle_deg"] is not None else ""
            self.t_plot.addItem(pg.InfiniteLine(f["best_time_s"], angle=90,
                                                pen=pg.mkPen(GR, width=1, style=Qt.DashLine),
                                                label=lbl, labelOpts={"color": GR, "position": 0.95}))
        self.q_plot.clear(); self.l_plot.clear()
        if r.period_s:
            self.q_plot.addItem(self.az_line)    # draggable azimuth cursor
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
                                                    label=f"T = {r.period_s:.2f} s",
                                                    labelOpts={"color": GR, "position": 0.9}))

    # ══════════════════════════════════════════════════════════════════
    def _update_buttons(self):
        busy, loaded = self.va.busy, self.info is not None
        self.run_btn.setEnabled(loaded or busy)
        self.run_btn.setText("✕ Abort" if busy else "▶ Analyse video")
        self.open_vid_btn.setEnabled(not busy)
        for wdg in (self.play_btn, self.slider, self.set_start_btn, self.set_end_btn, self.whole_btn):
            wdg.setEnabled(loaded)

    def sample_name(self) -> str:
        return self.sample_ed.text().strip() or self.sample_ed.placeholderText()

    def export_plots(self):
        """(name, PlotWidget) for the export screenshots — every result tab with a plot."""
        if not HAS_PG:
            return []
        return [("quality_vs_time", self.t_plot), ("quality_vs_azimuth", self.q_plot),
                ("self_similarity_vs_lag", self.l_plot)] + self.spatial.plots()

    def shutdown(self):
        self._play_t.stop(); self._seek_t.stop()
        if self.va.busy:
            self.va.abort()
        if self.spatial.pe.busy:
            self.spatial.pe.abort()
        if self._cap is not None:
            self._cap.release(); self._cap = None
