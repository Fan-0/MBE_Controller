"""
Offline RHEED video analysis — run the rotation-sync analysis on a recorded video
file (e.g. a Canon .MP4 / .MOV, or a clip saved by VideoRecorder). Qt-free; the
workers run on their own daemon threads like capture.RotationCapture.

NOTHING IS WRITTEN TO DISK here (2026-10-06, user request): results stay in memory and
rheed/export.py writes them only when the user presses Export.

  probe_video(path)   → VideoInfo (size, fps, frame count, duration, codec) or None
  VideoAnalysis       → two passes over the file, result = summary dict (below):

  PASS 1  ANALYSE  Read [start_s, end_s] at a reduced analysis rate (every `step`-th
                   frame, ≤ MAX_ANALYSIS_FRAMES total — the correlation matrix is
                   n²) → thumbnails → rotation.analyze_rotation() → period, per-frame
                   azimuth, best azimuths. Timestamps = frame_number / fps (camera
                   files are constant frame rate).
  PASS 2  EXTRACT  One sequential read at FULL frame rate. Every frame gets its azimuth
                   from the measured period; every frame within ±avg_deg of a kept
                   azimuth (in ANY revolution) goes into that azimuth's average; the
                   single best analysis frame is kept too. Every frame (≤ MAX_PROFILE_
                   FRAMES, else decimated) also yields its line profile ACROSS the
                   streaks inside the profile band → spatial.ProfileSeries for the
                   azimuth-locked spatial analysis (spacing, intensity vs time).
  ProfileExtraction  re-reads only the profiles with a different band (one pass).

summary keys: info, result (RotationResult), settings, t0, step, frame_numbers, times,
  thumbs (n,h,w float32), roi, picks [ {angle_deg, score, best_frame, best_time_s,
  best (BGR uint8), avg (BGR float32 | None), n_avg} ], profiles (ProfileSeries).
"""

import os
import threading
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

import numpy as np

try:
    import cv2
except ImportError:
    cv2 = None

from .rotation import analyze_rotation, make_thumb, _circ_dist
from .spatial import ProfileSeries, frame_profile, profile_axis

MAX_ANALYSIS_FRAMES = 3000      # n² correlation: 3000 frames ≈ 0.4 GB peak
MAX_PROFILE_FRAMES = 30000      # 30000 × ~900 px float32 ≈ 110 MB
STATIC_AVG_S = 0.25             # no rotation found: average ±this around each pick
VIDEO_EXTS = "*.mp4 *.MP4 *.mov *.MOV *.avi *.AVI *.mkv *.MKV *.m4v *.mts *.MTS"


@dataclass
class VideoInfo:
    path: str
    width: int
    height: int
    fps: float
    n_frames: int
    codec: str

    @property
    def duration_s(self) -> float:
        return self.n_frames / self.fps if self.fps else 0.0


def probe_video(path: str) -> Optional[VideoInfo]:
    if cv2 is None or not os.path.isfile(path):
        return None
    cap = cv2.VideoCapture(path)
    try:
        if not cap.isOpened():
            return None
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        cc = int(cap.get(cv2.CAP_PROP_FOURCC) or 0)
        codec = "".join(chr((cc >> 8 * i) & 0xFF) for i in range(4)).strip("\0 ") or "?"
        ok, img = cap.read()
        if not ok or fps <= 0 or n <= 0:
            return None
        return VideoInfo(path, img.shape[1], img.shape[0], float(fps), n, codec)
    finally:
        cap.release()


def enhance(img: np.ndarray, lo_pct: float = 0.5, hi_pct: float = 99.9) -> np.ndarray:
    """Percentile contrast stretch → uint8 (display / _enh.png only)."""
    sub = img[::4, ::4]
    lo, hi = np.percentile(sub, lo_pct), np.percentile(sub, hi_pct)
    if hi <= lo:
        hi = lo + 1
    return np.clip((img.astype(np.float32) - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8)


def _grey(img):
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img


def _clip_band(band, w, h):
    if band is None:
        return (0, 0, w, h)
    x, y, bw, bh = (int(v) for v in band)
    x0, y0 = max(0, x), max(0, y)
    return (x0, y0, max(1, min(w, x + bw) - x0), max(1, min(h, y + bh) - y0))


class _Worker:
    """Shared thread / abort / progress plumbing. Callbacks fire on the worker thread."""

    def __init__(self):
        self.on_progress: Optional[Callable[[float, str], None]] = None
        self.on_done: Optional[Callable[[Optional[object], str], None]] = None
        self._abort = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def abort(self):
        self._abort.set()

    def _launch(self, name, **kw):
        if self.busy or cv2 is None:
            return False
        self._abort.clear()
        self._thread = threading.Thread(target=self._run_safe, kwargs=kw, name=name, daemon=True)
        self._thread.start()
        return True

    def _progress(self, f: float, msg: str):
        if self.on_progress:
            self.on_progress(max(0.0, min(1.0, f)), msg)

    def _run_safe(self, **kw):
        try:
            out, msg = self._run(**kw)
        except Exception as e:                  # the GUI must never see an exception
            out, msg = None, f"video analysis failed: {e}"
        if self.on_done:
            self.on_done(out, msg)

    def _scan(self, path, first, last, need, visit, p0, p1, label):
        """Sequential read of frames first..last; visit(fn, img) for frames where
        need(fn) — others are grab()bed only. Returns the last frame reached, or None
        when aborted."""
        cap = cv2.VideoCapture(path)
        try:
            if first:
                cap.set(cv2.CAP_PROP_POS_FRAMES, first)
            fn = first
            span = max(1, last - first)
            while fn <= last:
                if self._abort.is_set():
                    return None
                if not cap.grab():
                    break
                if need(fn):
                    ok, img = cap.retrieve()
                    if ok:
                        visit(fn, img)
                if (fn - first) % 30 == 0:
                    self._progress(p0 + (p1 - p0) * (fn - first) / span, f"{label} {fn:6d} / {last}")
                fn += 1
            return fn - 1
        finally:
            cap.release()


class VideoAnalysis(_Worker):
    """Rotation analysis + full-rate extraction of a video file (in memory)."""

    def start(self, path: str, start_s: float = 0.0, end_s: Optional[float] = None,
              analysis_fps: float = 10.0, roi: Optional[Tuple[int, int, int, int]] = None,
              expected_rpm: float = 0.0, n_picks: int = 4, orientation: str = "auto",
              avg_deg: float = 1.0, band: Optional[Tuple[int, int, int, int]] = None) -> bool:
        return self._launch("RHEED-video", path=path, start_s=start_s, end_s=end_s,
                            analysis_fps=analysis_fps, roi=roi, expected_rpm=expected_rpm,
                            n_picks=n_picks, orientation=orientation, avg_deg=avg_deg, band=band)

    def _run(self, path, start_s, end_s, analysis_fps, roi, expected_rpm, n_picks,
             orientation, avg_deg, band):
        info = probe_video(path)
        if info is None:
            return None, f"cannot open video: {path}"
        fps = info.fps
        f0 = max(0, int(round(start_s * fps)))
        f1 = info.n_frames if not end_s else min(info.n_frames, int(round(end_s * fps)))
        if f1 - f0 < 10:
            return None, "analysis range is too short"
        step = max(1, int(round(fps / max(analysis_fps, 0.1))))
        step = max(step, int(np.ceil((f1 - f0) / MAX_ANALYSIS_FRAMES)))

        # PASS 1 — thumbnails at the analysis rate --------------------------
        times, thumbs, fnums = [], [], []

        def visit1(fn, img):
            times.append(fn / fps); thumbs.append(make_thumb(img, roi)); fnums.append(fn)
        last = self._scan(path, f0, f1 - 1, lambda fn: (fn - f0) % step == 0, visit1,
                          0.0, 0.45, "reading frame")
        if last is None:
            return None, "analysis aborted"
        f1 = min(f1, last + 1)                   # the file may hold fewer frames than claimed
        if len(times) < 10:
            return None, "too few frames could be decoded"

        self._progress(0.46, f"analysing {len(times)} frames…")
        exp_T = 60.0 / expected_rpm if expected_rpm > 0 else None
        res = analyze_rotation(times, thumbs, expected_period=exp_T, n_picks=n_picks,
                               orientation=orientation)
        thumbs = np.stack(thumbs)                # kept for the panel's CV view (~50 MB max)
        if self._abort.is_set():
            return None, "analysis aborted"

        # PASS 2 — full-rate: azimuth images + line profiles ----------------
        t0 = times[0]
        T = res.period_s
        picks = res.picks
        best_fn = [fnums[p.best_index] for p in picks]
        angs = np.array([p.angle_deg for p in picks])
        half = max(1, int(round(STATIC_AVG_S * fps)))
        sums, counts = [None] * len(picks), [0] * len(picks)
        bests = [None] * len(picks)
        bnd = _clip_band(band if band is not None else roi, info.width, info.height)
        pstep = max(1, int(np.ceil((f1 - f0) / MAX_PROFILE_FRAMES)))
        p_t, p_rows = [], []

        def members(fn):
            if not len(picks):
                return ()
            if T:
                return np.where(_circ_dist(angs, ((fn / fps - t0) % T) / T * 360.0) <= avg_deg)[0]
            return [k for k, b in enumerate(best_fn) if abs(fn - b) <= half]

        def need2(fn):
            return (fn - f0) % pstep == 0 or fn in best_fn or len(members(fn)) > 0

        def visit2(fn, img):
            for k in members(fn):
                sums[k] = img.astype(np.float32) if sums[k] is None else sums[k] + img
                counts[k] += 1
            for k, b in enumerate(best_fn):
                if b == fn:
                    bests[k] = img.copy()
            if (fn - f0) % pstep == 0:
                p_t.append(fn / fps)
                p_rows.append(frame_profile(_grey(img), bnd, res.orientation))
        if self._scan(path, f0, f1 - 1, need2, visit2, 0.48, 0.99, "extracting frame") is None:
            return None, "analysis aborted"

        out_picks = []
        for k, p in enumerate(picks):
            out_picks.append({
                "angle_deg": None if not np.isfinite(p.angle_deg) else round(float(p.angle_deg), 2),
                "score": round(float(p.score), 3), "best_frame": int(best_fn[k]),
                "best_time_s": round(best_fn[k] / fps, 3), "best": bests[k],
                "avg": sums[k] / counts[k] if counts[k] > 1 else None, "n_avg": counts[k]})
        profiles = ProfileSeries(times=np.asarray(p_t), pos=profile_axis(bnd, res.orientation),
                                 P=np.asarray(p_rows, np.float32), orientation=res.orientation,
                                 band=bnd)
        settings = dict(video=os.path.abspath(path), video_fps=round(fps, 4),
                        video_resolution=[info.width, info.height], video_codec=info.codec,
                        start_s=round(f0 / fps, 3), end_s=round(f1 / fps, 3), analysis_step=step,
                        analysis_fps=round(fps / step, 3), roi=roi, expected_rpm=expected_rpm,
                        n_picks=n_picks, orientation=res.orientation, avg_deg=avg_deg,
                        profile_band=list(bnd), profile_step=pstep)
        self._progress(1.0, "done")
        return {"info": info, "result": res, "settings": settings, "t0": t0, "step": step,
                "frame_numbers": fnums, "times": np.asarray(times), "thumbs": thumbs,
                "roi": roi, "picks": out_picks, "profiles": profiles}, res.message


class ProfileExtraction(_Worker):
    """Re-extract the per-frame line profiles with a new band (one full-rate pass)."""

    def start(self, path: str, f0: int, f1: int, band, orientation: str) -> bool:
        return self._launch("RHEED-profiles", path=path, f0=f0, f1=f1, band=band,
                            orientation=orientation)

    def _run(self, path, f0, f1, band, orientation):
        info = probe_video(path)
        if info is None:
            return None, f"cannot open video: {path}"
        bnd = _clip_band(band, info.width, info.height)
        pstep = max(1, int(np.ceil((f1 - f0) / MAX_PROFILE_FRAMES)))
        p_t, rows = [], []

        def visit(fn, img):
            p_t.append(fn / info.fps); rows.append(frame_profile(_grey(img), bnd, orientation))
        if self._scan(path, f0, f1 - 1, lambda fn: (fn - f0) % pstep == 0, visit,
                      0.0, 1.0, "profiling frame") is None:
            return None, "profile extraction aborted"
        if not rows:
            return None, "no frames decoded"
        ser = ProfileSeries(times=np.asarray(p_t), pos=profile_axis(bnd, orientation),
                            P=np.asarray(rows, np.float32), orientation=orientation, band=bnd)
        return ser, f"{len(rows)} profiles, band {bnd}"
