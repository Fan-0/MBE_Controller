"""
RHEED camera sources.

Hardware path: Canon EOS 90D  →  HDMI (mini/micro HDMI out)  →  HDMI-to-USB capture
device (UVC: INOGENI 4KXPLUS / generic "USB C Video Adaptor")  →  laptop USB-C.
The capture device enumerates as an ordinary webcam, so OpenCV's DirectShow
backend reads it directly — no Canon SDK is involved.

Design rules (same spirit as devices/):
  * NOTHING here raises to the caller once start() has been called. A missing,
    unplugged or re-plugged camera only changes `state`; the capture thread keeps
    retrying every RECONNECT_INTERVAL seconds and resumes on its own.
  * One capture thread per source. Consumers never touch cv2.VideoCapture:
      latest()          → most recent Frame (GUI preview polls this on a QTimer)
      subscribe()       → a bounded FrameQueue of every frame (recorders/analysis);
                          when a consumer falls behind, frames are DROPPED and
                          counted rather than blocking the capture thread.
  * Qt-free, so it can be tested headlessly and reused by the main controller.

Classes:
  RHEEDCamera        real UVC capture device (cv2.VideoCapture)
  SimRHEEDCamera     synthetic rotating-substrate RHEED pattern (test mode / no camera);
                     variable rpm, optional growth oscillation
  VideoReplayCamera  a recorded video file played as the live camera (file timing)
"""

import logging
import math
import queue
import threading
import time
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

import os
# The reconnect loop would otherwise print a DSHOW "can't capture by index" warning
# every 2 s. Must be set before cv2 is first imported (cv2.utils.logging is absent
# from the opencv-python wheels).
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
try:
    import cv2
    HAS_CV2 = True
except ImportError:          # GUI still opens; real-camera source reports the problem
    cv2 = None
    HAS_CV2 = False

log = logging.getLogger(__name__)

# ── States ────────────────────────────────────────────────────────────
STATE_IDLE         = "IDLE"           # not started
STATE_CONNECTING   = "CONNECTING"
STATE_CONNECTED    = "CONNECTED"
STATE_DISCONNECTED = "DISCONNECTED"   # retrying in the background

# ── Tunables ──────────────────────────────────────────────────────────
RECONNECT_INTERVAL = 2.0    # s between open attempts while disconnected
READ_FAIL_LIMIT    = 15     # consecutive failed reads → treat as unplugged
DARK_STD           = 2.0    # grey-level std below this → image is blank: capture card
                            #   has no HDMI input, camera asleep, or e-beam off.
                            #   Reported as a flag, NOT a disconnect (a dark RHEED
                            #   screen is a legitimate state).
DARK_CHECK_EVERY   = 15     # frames between dark checks (cheap, but no need per frame)


@dataclass
class Frame:
    image: np.ndarray        # BGR uint8, H×W×3 — treat as read-only (shared by consumers)
    t: float                 # time.time() when the frame was received
    index: int               # running frame counter for this source


class FrameQueue(queue.Queue):
    """Bounded queue that counts frames dropped because its consumer fell behind."""
    def __init__(self, maxsize: int):
        super().__init__(maxsize)
        self.dropped = 0


class _FrameSource:
    """Shared capture-thread machinery. Subclasses implement _open/_grab/_close."""

    name = "source"

    def __init__(self):
        self.state = STATE_IDLE
        self.message = ""                # human-readable status detail
        self.dark = False
        self.fps = 0.0                   # measured, EMA
        self.resolution = (0, 0)         # (w, h) of delivered frames
        self._latest: Optional[Frame] = None
        self._subs: List[FrameQueue] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._idx = 0

    # ── public API ──
    @property
    def connected(self) -> bool:
        return self.state == STATE_CONNECTED

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=f"RHEED-{self.name}", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
        self._thread = None
        self.state, self.message, self.fps = STATE_IDLE, "stopped", 0.0

    def latest(self) -> Optional[Frame]:
        with self._lock:
            return self._latest

    def subscribe(self, maxsize: int = 120) -> FrameQueue:
        q = FrameQueue(maxsize)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: FrameQueue):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    # ── subclass hooks ──
    def _open(self) -> bool: raise NotImplementedError
    def _grab(self) -> Optional[np.ndarray]: raise NotImplementedError
    def _close(self): pass

    # ── capture thread ──
    def _set(self, state: str, msg: str):
        if (state, msg) != (self.state, self.message):
            log.info("%s: %s %s", self.name, state, msg)
        self.state, self.message = state, msg

    def _publish(self, img: np.ndarray, t: Optional[float] = None):
        now = time.time() if t is None else t
        with self._lock:
            prev = self._latest
            self._idx += 1
            fr = Frame(img, now, self._idx)
            self._latest = fr
            subs = list(self._subs)
        if prev is not None:
            dt = now - prev.t
            if dt > 0:
                self.fps = 1.0 / dt if self.fps == 0 else 0.9 * self.fps + 0.1 / dt
        self.resolution = (img.shape[1], img.shape[0])
        if self._idx % DARK_CHECK_EVERY == 1:
            self.dark = float(img[::8, ::8].std()) < DARK_STD
        for q in subs:
            try:
                q.put_nowait(fr)
            except queue.Full:
                q.dropped += 1

    def _loop(self):
        opened = False
        fails = 0
        while not self._stop.is_set():
            if not opened:
                self._set(STATE_CONNECTING, "opening…")
                try:
                    opened = self._open()
                except Exception as e:          # never let a driver quirk kill the thread
                    log.warning("%s open error: %s", self.name, e)
                    opened = False
                if not opened:
                    self._close()
                    self.fps = 0.0
                    why = "not found" if HAS_CV2 or self.name == "sim" else \
                          "opencv not installed (pip install opencv-python-headless)"
                    self._set(STATE_DISCONNECTED, f"{why} — retrying every {RECONNECT_INTERVAL:.0f} s")
                    self._stop.wait(RECONNECT_INTERVAL)
                    continue
                fails = 0
                self._set(STATE_CONNECTED, "")
            try:
                img = self._grab()
            except Exception as e:
                log.warning("%s read error: %s", self.name, e)
                img = None
            if img is None:
                fails += 1
                if fails >= READ_FAIL_LIMIT:
                    self._close()
                    opened = False
                    self.fps = 0.0
                    self._set(STATE_DISCONNECTED, f"signal lost — retrying every {RECONNECT_INTERVAL:.0f} s")
                    self._stop.wait(RECONNECT_INTERVAL)
                else:
                    self._stop.wait(0.02)
                continue
            fails = 0
            if isinstance(img, tuple):          # (image, timestamp) from a source with
                self._publish(*img)             # its own clock (VideoReplayCamera)
            else:
                self._publish(img)
        self._close()


# ══════════════════════════════════════════════════════════════════════
# Real camera
# ══════════════════════════════════════════════════════════════════════

_BACKENDS = {"dshow": "CAP_DSHOW", "msmf": "CAP_MSMF", "any": "CAP_ANY"}


class RHEEDCamera(_FrameSource):
    """
    UVC capture device via OpenCV.

    index:   DirectShow device index (0 is usually the laptop's built-in webcam —
             run diagnostics/rheed_camera_probe.py to find the capture card).
    backend: "dshow" (default; reliable resolution/FOURCC control on Windows),
             "msmf" or "any".
    fourcc:  None = device default, or "MJPG" / "YUY2". USB-2 dongles usually need
             MJPG to reach 1080p30; USB-3 boxes (INOGENI) deliver YUY2 natively.

    CAMERA SETUP (Canon 90D): set HDMI info display → OFF ("clean HDMI") or the
    focus box / exposure overlay is burned into every frame; disable auto power off.
    """

    def __init__(self, index: int = 0, backend: str = "dshow",
                 width: int = 1920, height: int = 1080, fps: float = 30.0,
                 fourcc: Optional[str] = None):
        super().__init__()
        self.index, self.backend = index, backend
        self.req_w, self.req_h, self.req_fps, self.fourcc = width, height, fps, fourcc
        self.name = f"cam{index}"
        self._cap = None

    def _open(self) -> bool:
        if not HAS_CV2:
            return False
        api = getattr(cv2, _BACKENDS.get(self.backend, "CAP_DSHOW"))
        cap = cv2.VideoCapture(self.index, api)
        if not cap.isOpened():
            cap.release()
            return False
        if self.fourcc:                     # must precede the size request on DirectShow
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.req_w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.req_h)
        cap.set(cv2.CAP_PROP_FPS, self.req_fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # newest frame, not a stale backlog
        ok, img = cap.read()                 # isOpened() alone accepts phantom devices
        if not ok or img is None:
            cap.release()
            return False
        self._cap = cap
        self._publish(img)
        return True

    def _grab(self) -> Optional[np.ndarray]:
        ok, img = self._cap.read()
        return img if ok else None

    def _close(self):
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
        self._cap = None


def list_cameras(max_index: int = 6, backend: str = "dshow", skip=()) -> List[dict]:
    """
    Probe DirectShow indices 0..max_index-1. Returns [{index, name, width, height}].
    skip: indices NOT to open (e.g. the camera already streaming — opening it a
    second time would fail or steal it from the capture thread).
    Names come from pygrabber when installed (optional); otherwise "Camera N".
    Opening each device takes ~0.5–1 s — call from a background thread.
    """
    names = []
    try:
        from pygrabber.dshow_graph import FilterGraph   # optional dependency
        names = FilterGraph().get_input_devices()
    except Exception:
        pass
    found = []
    if not HAS_CV2:
        return found
    api = getattr(cv2, _BACKENDS.get(backend, "CAP_DSHOW"))
    for i in range(max_index):
        if i in skip:
            continue
        cap = cv2.VideoCapture(i, api)
        try:
            if not cap.isOpened():
                continue
            ok, img = cap.read()
            w, h = (img.shape[1], img.shape[0]) if ok and img is not None else (0, 0)
            found.append({"index": i, "name": names[i] if i < len(names) else f"Camera {i}",
                          "width": w, "height": h})
        finally:
            cap.release()
    return found


# ══════════════════════════════════════════════════════════════════════
# Simulated source
# ══════════════════════════════════════════════════════════════════════

def render_sim_pattern(phi_deg: float, w: int = 960, h: int = 540,
                       rng: Optional[np.random.Generator] = None,
                       noise: float = 3.0, gain: float = 1.0) -> np.ndarray:
    """
    Synthetic RHEED screen for substrate azimuth phi (degrees), BGR uint8.

    Models a 4-fold surface:
      * principal azimuth A (0/90/180/270°): 7 sharp streaks, spacing d
      * principal azimuth B (45/135/…°):     weaker streaks, spacing d·√2
      * off-azimuth: dim diffuse spots that move with phi (so in-between frames
        are distinguishable from each other)
      * specular spot whose height wobbles with sin(phi) — a sample-tilt term that
        makes the TRUE 360° period repeat exactly while the 90° sub-period only
        repeats approximately, exactly like a real wobbling holder.
    gain scales the streaks + specular spot (growth oscillations, see SimRHEEDCamera).
    """
    rng = rng or np.random.default_rng()
    x = np.arange(w, dtype=np.float32)
    y = np.arange(h, dtype=np.float32)
    cx, y_shadow = w * 0.5, h * 0.22
    d = w * 0.085

    def az_weight(center, period=90.0, sigma=5.0):
        dphi = (phi_deg - center + period / 2) % period - period / 2
        return math.exp(-(dphi / sigma) ** 2)

    wa, wb = az_weight(0.0), 0.65 * az_weight(45.0)
    tilt = math.sin(math.radians(phi_deg))

    img = np.full((h, w), 9.0, np.float32)
    below = np.clip((y - y_shadow) / 6.0, 0, 1)             # shadow edge
    img += (25.0 * below * np.exp(-(y - y_shadow) / (h * 0.9)))[:, None]

    # streaks — separable: column profile ⊗ row profile
    row = below * np.exp(-np.maximum(y - y_shadow, 0) / (h * 0.45))
    col = np.zeros(w, np.float32)
    for k in range(-3, 4):
        amp = 1.0 if k == 0 else 0.75 - 0.08 * abs(k)
        col += wa * amp * np.exp(-((x - cx - k * d) / 5.0) ** 2)
        col += wb * amp * np.exp(-((x - cx - k * d * 1.414) / 7.0) ** 2)
    img += 170.0 * gain * col[None, :] * row[:, None]

    def spot(sx, sy, amp, sig):
        x0, x1 = int(max(sx - 4 * sig, 0)), int(min(sx + 4 * sig, w))
        y0, y1 = int(max(sy - 4 * sig, 0)), int(min(sy + 4 * sig, h))
        if x1 <= x0 or y1 <= y0:
            return
        gx = np.exp(-((x[x0:x1] - sx) / sig) ** 2)
        gy = np.exp(-((y[y0:y1] - sy) / sig) ** 2)
        img[y0:y1, x0:x1] += amp * gy[:, None] * gx[None, :]

    spot(cx, y_shadow + h * 0.18 + 10 * tilt, (120 + 40 * tilt) * gain, 9)   # specular
    p = math.radians(phi_deg)
    for j, r in enumerate((0.18, 0.27, 0.36)):                      # off-azimuth diffuse
        amp = 35.0 * (1 - max(wa, wb / 0.65))
        spot(cx + w * r * math.sin(4 * p + j), y_shadow + h * (0.35 + 0.1 * j), amp, 11)
        spot(cx - w * r * math.sin(4 * p + 2 * j), y_shadow + h * (0.42 + 0.08 * j), amp, 11)

    if noise > 0:
        img += rng.standard_normal((h, w), dtype=np.float32) * noise
    g = np.clip(img, 0, 255)
    # green phosphor: B, G, R
    return np.dstack([g * 0.25, g, g * 0.5]).astype(np.uint8)


class SimRHEEDCamera(_FrameSource):
    """
    Rotating-substrate RHEED simulator with the same interface as RHEEDCamera.

    rpm may be changed while running (the azimuth is INTEGRATED, so a change does not
    jump the phase — used to test drift tracking; rpm 0 = static substrate).
    Growth oscillations for the intensity tracker: osc_period > 0 multiplies the
    streaks + specular spot by 1 + osc_amp·cos(2π t/osc_period)·exp(−t/osc_decay),
    t from start_growth() (or from start). osc_decay 0 = no damping.
    """

    def __init__(self, rpm: float = 10.0, fps: float = 30.0, width: int = 960, height: int = 540,
                 osc_period: float = 0.0, osc_amp: float = 0.35, osc_decay: float = 0.0):
        super().__init__()
        self.name = "sim"
        self.rpm, self.req_fps, self.w, self.h = rpm, fps, width, height
        self.osc_period, self.osc_amp, self.osc_decay = osc_period, osc_amp, osc_decay
        self._t0 = 0.0
        self._t_growth = None
        self._next = 0.0
        self._phi = 0.0
        self._t_last = 0.0
        self._rng = np.random.default_rng()

    def start_growth(self):
        """Restart the simulated oscillation clock (like opening the shutter)."""
        self._t_growth = time.monotonic()

    def _open(self) -> bool:
        self._t0 = self._next = self._t_last = time.monotonic()
        if self._t_growth is None:
            self._t_growth = self._t0
        return True

    def _grab(self) -> np.ndarray:
        self._next += 1.0 / self.req_fps
        delay = self._next - time.monotonic()
        if delay > 0:
            self._stop.wait(delay)
        else:
            self._next = time.monotonic()      # fell behind — don't burst to catch up
        now = time.monotonic()
        self._phi = (self._phi + (now - self._t_last) * self.rpm * 6.0) % 360.0   # rpm × 360/60
        self._t_last = now
        gain = 1.0
        if self.osc_period > 0:
            tg = now - self._t_growth
            env = math.exp(-tg / self.osc_decay) if self.osc_decay > 0 else 1.0
            gain = 1.0 + self.osc_amp * math.cos(2 * math.pi * tg / self.osc_period) * env
        return render_sim_pattern(self._phi, self.w, self.h, self._rng, gain=gain)


class VideoReplayCamera(_FrameSource):
    """
    Plays a recorded video file as if it were the live camera (frames paced at the
    file's frame rate × speed, timestamps = wall clock) — exercises the live tools
    (rotation lock, intensity tracking) on real data without the beam. Loops at the end
    (a loop restarts the rotation phase: the tracker re-locks on the next passes).

    Frame.t = replay start + video time / speed — the FILE's timing, not the moment
    the frame was decoded. (Wall-clock stamps were tried first: at 2× a 1080p H.264
    file decodes slower than 60 fps, playback silently lagged ~2 % and the lock read
    a 2 % longer period.) If decoding can't keep up, playback falls behind real time
    but the timestamps — and every period/phase derived from them — stay exact.
    """

    def __init__(self, path: str, speed: float = 1.0, loop: bool = True):
        super().__init__()
        self.path, self.speed, self.loop = path, max(0.05, speed), loop
        self.name = "replay"
        self._cap = None
        self._next = 0.0
        self._dt = 1 / 30.0
        self._t0 = 0.0          # wall time of the first frame
        self._k = 0             # frames delivered since then (keeps counting across loops)

    def _open(self) -> bool:
        if not HAS_CV2 or not os.path.isfile(self.path):
            return False
        cap = cv2.VideoCapture(self.path)
        if not cap.isOpened():
            return False
        self._dt = 1.0 / ((cap.get(cv2.CAP_PROP_FPS) or 30.0) * self.speed)
        self._cap = cap
        self._next = time.monotonic()
        self._t0, self._k = time.time(), 0
        return True

    def _grab(self) -> Optional[np.ndarray]:
        self._next += self._dt
        delay = self._next - time.monotonic()
        if delay > 0:
            self._stop.wait(delay)
        else:
            self._next = time.monotonic()
        ok, img = self._cap.read()
        if not ok and self.loop:
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, img = self._cap.read()
        if not ok:
            return None
        t = self._t0 + self._k * self._dt
        self._k += 1
        return img, t                         # (image, timestamp) — see _FrameSource._loop

    def _close(self):
        if self._cap is not None:
            self._cap.release()
        self._cap = None
