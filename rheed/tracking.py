"""
Live RHEED intensity tracking (RHEED oscillations) — Qt-free, worker threads.
Roadmap R11 (phase-locked acquisition) + the live half of R12 (oscillations).

  RotationLock      short burst of THUMBNAILS only (no video written, nothing saved) →
                    rotation.analyze_rotation(expected_period from the approximate
                    rpm) → LockResult: period T, phase reference t_ref (absolute
                    time.time() at azimuth 0), kept azimuths.
  IntensityTracker  subscribes to the camera and, for every frame, computes ONLY its
                    azimuth from the clock: phase = ((t − t_ref)/T mod 1)·360°.
                    Frames outside ±window° of the tracked azimuth are skipped
                    without touching the image (cheap: ±6° ⇒ ~3 % of frames are
                    processed). Frames inside the window → mean grey level in the
                    intensity box. When the window has been passed, the pass is
                    reduced to ONE point (peak = max of the 3-frame-smoothed values,
                    or mean) at its time → intensity vs time, one point / revolution.
                    PHASE TRACKING: the time of the intensity maximum (parabolic
                    sub-frame fit) vs the predicted azimuth crossing nudges t_ref
                    (gain 0.3) and successive peak times refine T (gain 0.2), so slow
                    rpm drift stays centred in the window. Only interior maxima with a
                    real peak (≥ 3 frames, not on the window edge) correct the lock.
                    STATIC mode (no rotation): every Nth frame's box mean → the
                    classic RHEED-oscillation trace.
  Points are kept in the tracker (thread-safe); the GUI polls snapshot(since).

Limit (physics, not code): one point per revolution samples the oscillation at 1/T,
so only oscillation periods > 2T are resolvable while rotating (Nyquist). Real MBE
monolayer periods are ~1–10 s, so oscillations are normally measured with rotation
OFF (static mode) or at a rotation much faster than the growth rate is slow.
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

import numpy as np

try:
    import cv2
except ImportError:
    cv2 = None

from .camera import STATE_CONNECTED
from .rotation import RotationResult, analyze_rotation, make_thumb

STALL_TIMEOUT = 3.0
PHASE_GAIN = 0.6            # fraction of the measured peak-time error applied to t_ref
PERIOD_GAIN = 0.3           # fraction of the phase error added to T each clean pass (PLL integral)
                            #   z²−(2−a−b)z+(1−a)=0 → |z| = √0.4 ≈ 0.63/rev: stable, fast
WIN_MAX = 25.0              # acquisition: window may widen up to ±this (deg) — below the
                            #   30°/45° spacing of neighbouring directions on 6-/4-fold surfaces
REARM_DEG = 90.0            # after a pass, the next one may start only once |phase−az| > this
WIN_GROW, WIN_SHRINK = 2.0, 0.7   # per pass, while re-acquiring / once locked again
MIN_PEAK_CONTRAST = 0.01    # peak must rise ≥ 1 % above both window edges to steer
PERIOD_TOL = 0.15           # tracked T stays within ±15 % of the locked T


@dataclass
class LockResult:
    period_s: Optional[float]
    t_ref: float                         # absolute time (time.time()) of azimuth 0
    picks: List[Tuple[float, float]]     # (azimuth deg, quality score)
    result: RotationResult
    duration_s: float
    message: str = ""

    @property
    def rpm(self):
        return 60.0 / self.period_s if self.period_s else None


@dataclass
class TrackPoint:
    t: float                 # absolute time (time.time()) of the point
    value: float             # grey level (box mean; peak or mean over the pass)
    n: int                   # frames that went into it
    phase_err_s: float = float("nan")   # measured − predicted peak time (rotating)
    period_s: float = float("nan")      # T in use after this pass
    locked: bool = True                 # clear interior peak (it steered the lock)
    hunting: bool = False               # peak was on the window edge → phase stepped


def box_mean(img: np.ndarray, box) -> float:
    """Mean grey level of box (x, y, w, h) — green channel ≈ grey for a phosphor screen
    and avoids a full-frame colour conversion; grey frames are used as they are."""
    x, y, w, h = (int(v) for v in box)
    crop = img[max(y, 0):y + h, max(x, 0):x + w]
    if crop.size == 0:
        return float("nan")
    if crop.ndim == 3:
        crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if cv2 is not None else crop.mean(axis=2)
    return float(crop.mean())


class _Worker:
    def __init__(self, source):
        self.source = source
        self.on_done: Optional[Callable] = None
        self.on_progress: Optional[Callable[[float, str], None]] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self):
        self._stop.set()

    def _launch(self, target, name, **kw):
        if self.busy or self.source is None or self.source.state != STATE_CONNECTED:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=target, kwargs=kw, name=name, daemon=True)
        self._thread.start()
        return True


# ══════════════════════════════════════════════════════════════════════

class RotationLock(_Worker):
    """Measure T and phase from a short thumbnail burst (nothing recorded to disk)."""

    def start(self, duration_s: float, expected_rpm: float = 0.0, roi=None,
              n_picks: int = 4, orientation: str = "auto") -> bool:
        return self._launch(self._run_safe, "RHEED-lock", duration_s=duration_s,
                            expected_rpm=expected_rpm, roi=roi, n_picks=n_picks,
                            orientation=orientation)

    def _run_safe(self, **kw):
        try:
            res, msg = self._run(**kw)
        except Exception as e:
            res, msg = None, f"lock failed: {e}"
        if self.on_done:
            self.on_done(res, msg)

    def _run(self, duration_s, expected_rpm, roi, n_picks, orientation):
        q = self.source.subscribe(maxsize=240)
        times, thumbs = [], []
        t0 = last = time.time()
        try:
            while time.time() - t0 < duration_s and not self._stop.is_set():
                try:
                    fr = q.get(timeout=0.5)
                except Exception:
                    if time.time() - last > STALL_TIMEOUT:
                        return None, "camera lost during lock"
                    continue
                last = time.time()
                times.append(fr.t); thumbs.append(make_thumb(fr.image, roi))
                if self.on_progress:
                    self.on_progress(min(1.0, (last - t0) / duration_s),
                                     f"locking {last - t0:4.1f}/{duration_s:.0f} s")
        finally:
            self.source.unsubscribe(q)
        if self._stop.is_set():
            return None, "lock aborted"
        if len(times) < 10:
            return None, "too few frames for a lock"
        exp_T = 60.0 / expected_rpm if expected_rpm > 0 else None
        res = analyze_rotation(times, thumbs, expected_period=exp_T, n_picks=n_picks,
                               orientation=orientation)
        picks = [(float(p.angle_deg), float(p.score)) for p in res.picks if np.isfinite(p.angle_deg)]
        lk = LockResult(period_s=res.period_s, t_ref=float(times[0]), picks=picks, result=res,
                        duration_s=float(times[-1] - times[0]), message=res.message)
        return lk, res.message + (f" — {res.warning}" if res.warning else "")


# ══════════════════════════════════════════════════════════════════════

class IntensityTracker(_Worker):
    """Intensity of a box at one azimuth, once per revolution (or every frame, static)."""

    def __init__(self, source):
        super().__init__(source)
        self._lock = threading.Lock()
        self._points: List[TrackPoint] = []
        self.frames_seen = 0
        self.frames_processed = 0
        self.passes = 0
        self.hunts = 0
        self.period_s: Optional[float] = None
        self.t_ref = 0.0
        self.message = ""

    # ── GUI side ──
    def snapshot(self, since: int = 0) -> List[TrackPoint]:
        with self._lock:
            return list(self._points[since:])

    def clear(self):
        with self._lock:
            self._points = []

    def set_box(self, box):
        self._box = box                  # picked up by the worker on the next frame

    def phase_of(self, t: float) -> Optional[float]:
        """Azimuth (deg) predicted for absolute time t with the CURRENT lock."""
        if not self.period_s:
            return None
        return ((t - self.t_ref) / self.period_s % 1.0) * 360.0

    # ── start ──
    def start_rotating(self, box, period_s: float, t_ref: float, azimuth: float,
                       window_deg: float = 6.0, value: str = "peak",
                       track_phase: bool = True) -> bool:
        self._box, self.period_s, self.t_ref = box, float(period_s), float(t_ref)
        self._T_lock = float(period_s)
        self.window_eff = float(window_deg)
        self.frames_seen = self.frames_processed = self.passes = self.hunts = 0
        return self._launch(self._run_rotating, "RHEED-track", azimuth=azimuth,
                            window=window_deg, value=value, track_phase=track_phase)

    def start_static(self, box, every: int = 1) -> bool:
        self._box, self.period_s = box, None
        self.frames_seen = self.frames_processed = self.passes = self.hunts = 0
        return self._launch(self._run_static, "RHEED-track", every=max(1, int(every)))

    def _add(self, p: TrackPoint):
        with self._lock:
            self._points.append(p)

    # ── workers ──
    def _frames(self):
        """Yield frames until stopped; ends on a camera stall."""
        q = self.source.subscribe(maxsize=240)
        last = time.time()
        try:
            while not self._stop.is_set():
                try:
                    fr = q.get(timeout=0.5)
                except Exception:
                    if time.time() - last > STALL_TIMEOUT:
                        self.message = "camera lost — tracking paused, waiting for frames"
                        last = time.time()
                    continue
                last = time.time()
                self.frames_seen += 1
                yield fr
        finally:
            self.source.unsubscribe(q)

    def _run_static(self, every):
        self.message = "tracking (static substrate)"
        try:
            for fr in self._frames():
                if self.frames_seen % every:
                    continue
                v = box_mean(fr.image, self._box)
                self.frames_processed += 1
                self._add(TrackPoint(fr.t, v, 1))
        except Exception as e:
            self.message = f"tracking stopped: {e}"
        if self.on_done:
            self.on_done(None, self.message)

    def _run_rotating(self, azimuth, window, value, track_phase):
        self.message = "tracking (rotating — one point per revolution)"
        buf: List[Tuple[float, float]] = []          # (t, box mean) inside the window
        prev_peak_t = None
        armed = True                                 # may a new pass start?
        try:
            for fr in self._frames():
                T = self.period_s
                # signed azimuth distance to the tracked direction, (−180, 180]
                ph = ((fr.t - self.t_ref) / T % 1.0) * 360.0
                d = (ph - azimuth + 180.0) % 360.0 - 180.0
                if not armed:
                    # Right after a pass the window may widen and t_ref may move, which
                    # can put the very next frame back "inside" → passes 0.2 s apart
                    # (seen in testing). Re-arm only once the phase is far away.
                    armed = abs(d) > REARM_DEG
                    continue
                if abs(d) <= self.window_eff:
                    buf.append((fr.t, box_mean(fr.image, self._box)))
                    self.frames_processed += 1
                    continue
                if not buf:
                    continue
                # window left → one point for this pass
                prev_peak_t = self._close_pass(buf, azimuth, value, track_phase, prev_peak_t, window)
                buf = []
                armed = False
        except Exception as e:
            self.message = f"tracking stopped: {e}"
        if self.on_done:
            self.on_done(None, self.message)

    def _close_pass(self, buf, azimuth, value, track_phase, prev_peak_t, window):
        t = np.array([b[0] for b in buf]); v = np.array([b[1] for b in buf])
        T = self.period_s
        sm = np.convolve(np.pad(v, 1, mode="edge"), np.ones(3) / 3, mode="valid") if len(v) >= 3 else v
        k = int(np.argmax(sm))
        # Peak TIME = intensity-weighted centroid of the pass (weights = smoothed
        # value minus the pass minimum). Tried argmax + parabola first: with the few
        # frames a window holds at higher rpm (4 at 20 rpm/30 fps/±8°) the argmax was
        # mostly noise and often sat on the window edge, and an edge "hunting" step
        # then knocked the phase around every pass. The centroid uses every frame,
        # moves towards a peak that drifts towards an edge (so it pulls the lock back
        # by itself), and is unbiased once the peak is centred.
        # Centroid over the MAIN peak only: the contiguous run around the maximum above
        # half its height (a widened window can contain a neighbouring direction 45° /
        # 30° away on a 4-/6-fold surface, which would pull a whole-window centroid).
        lo_v = sm.min()
        half = lo_v + 0.5 * (sm[k] - lo_v)
        a = k
        while a > 0 and sm[a - 1] >= half:
            a -= 1
        b = k
        while b < len(sm) - 1 and sm[b + 1] >= half:
            b += 1
        w = sm[a:b + 1] - lo_v
        peak_t = float((t[a:b + 1] * w).sum() / w.sum()) if w.sum() > 0 else float(t[k])
        val = float(sm[k]) if value == "peak" else float(v.mean())
        t_pt = peak_t if value == "peak" else float(t.mean())
        # predicted crossing of the tracked azimuth closest to this pass
        n = np.round((t.mean() - self.t_ref) / T - azimuth / 360.0)
        t_pred = self.t_ref + (n + azimuth / 360.0) * T
        err = float(peak_t - t_pred)
        # only a pass with real contrast steers the lock (a flat window — beam off,
        # pattern gone, wrong azimuth — must not drag the phase around)
        clear = len(sm) >= 3 and (sm.max() - sm.min()) >= MIN_PEAK_CONTRAST * max(abs(sm.max()), 1e-9)
        hunting = False
        if track_phase and clear:
            # second-order PLL: phase (proportional) + period (integral) correction.
            # A period mismatch shows up as an error growing by ΔT every revolution,
            # so the integral term learns it. (Learning T from two consecutive clean
            # peaks was tried first — slow to converge after an rpm step.)
            t_cross = t_pred + PHASE_GAIN * err        # corrected crossing of the azimuth
            T_new = T + PERIOD_GAIN * err
            T0 = self._T_lock
            self.period_s = min(max(T_new, T0 * (1 - PERIOD_TOL)), T0 * (1 + PERIOD_TOL))
            # re-anchor at THIS crossing: a T update must not swing the phase by
            # (revolutions since the lock) × ΔT — 0.1 % after 30 revs ≈ 11°
            self.t_ref = t_cross - azimuth / 360.0 * self.period_s
            prev_peak_t = float(peak_t)
        # Acquisition mode: inside the window the centroid can report at most ~half a
        # window of error, so a slip larger than that (rpm step, lost lock) can't be
        # measured. Unclean pass or |error| > ½ window → widen the window (×2, up to
        # WIN_MAX); clean and well centred → shrink back towards the user's window.
        # Extra frames are processed only while re-acquiring.
        err_deg = abs(err) / T * 360.0
        if track_phase and (not clear or err_deg > 0.5 * self.window_eff):
            if self.window_eff < WIN_MAX:
                hunting = True
            self.window_eff = min(WIN_MAX, self.window_eff * WIN_GROW)
        else:
            self.window_eff = max(window, self.window_eff * WIN_SHRINK)
        self.passes += 1
        if hunting:
            self.hunts += 1
        self._add(TrackPoint(t_pt, val, len(buf), err, float(self.period_s), locked=clear, hunting=hunting))
        return prev_peak_t
