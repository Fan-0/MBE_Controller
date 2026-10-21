"""
RHEED capture workflows — Qt-free, each runs on its own daemon thread.

  save_snapshot(source, out_dir, sample)    one full-resolution PNG of the live frame
  record_clip(source, path, duration_s)     BLOCKING timed clip (recipe RHEED_VIDEO)
  VideoRecorder                             continuous recording to .avi until stopped
  RotationCapture                           rotation-synchronised keyframe capture:

RotationCapture pipeline (the "short clip → keep the good frames → delete the clip" flow):
  1. RECORD   Every frame for `duration_s` goes to a temp clip in the system temp
              folder — NOT the OneDrive-synced
              project folder, which would start uploading hundreds of MB. Only a
              128-px grey thumbnail per frame is kept in RAM (~30 MB for 20 s @ 30 fps).
              Codec: MJPG by default (~75 fps encode at 1080p, 0.4 MB/frame, mean error
              ~1.6 grey levels — below the camera's own noise). lossless=True uses FFV1,
              which is exact but only encodes ~10 fps at 1080p, so at full HD most
              frames are DROPPED (the analysis tolerates drops; azimuth resolution
              suffers). Benchmarked 2026-10-01, opencv 4.10.
  2. ANALYSE  rotation.analyze_rotation() on the thumbnails + frame timestamps →
              rotation period, per-frame azimuth, and the best-pattern azimuths.
  3. EXTRACT  One sequential pass over the temp clip pulls the selected frames at full
              resolution: per kept azimuth, the single best frame and the mean of the
              matching frame from every revolution (lower noise, same azimuth).
  4. CLEAN UP The temp clip is deleted (or moved next to the results if keep_video).

Output: logs/rheed/<YYYYMMDD_HHMMSS>_<sample>/
    az1_000.4deg_best.png, az1_000.4deg_avg3.png, …    (angles relative to clip start)
    rotation_summary.json                              period, rpm, picks, settings
    frames.csv                                         per-frame time / azimuth / quality

Callbacks are invoked from the worker thread — a GUI must bridge them to its main
thread (pyqtSignal; see rheed_tab._Bridge).
"""

import csv
import json
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import datetime
from typing import Callable, Optional, Tuple

import numpy as np

try:
    import cv2
except ImportError:
    cv2 = None

from .camera import STATE_CONNECTED
from .rotation import RotationResult, analyze_rotation, make_thumb

STALL_TIMEOUT = 3.0     # s with no frames → camera lost; finish with what we have


def _safe(name: str) -> str:
    return re.sub(r"[^\w\-]+", "_", name.strip())[:60] if name else ""


def _stamp(sample: str = "") -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{ts}_{_safe(sample)}" if sample else ts


def _open_writer(path: str, size: Tuple[int, int], fps: float, codecs=("MJPG", "FFV1")):
    """First codec that opens. MJPG = OpenCV's built-in encoder (fast, near-lossless at
    RHEED noise levels); FFV1 = lossless via ffmpeg (slow at 1080p)."""
    for c in codecs:
        if c == "MJPG":
            w = cv2.VideoWriter(path, cv2.CAP_OPENCV_MJPEG, cv2.VideoWriter_fourcc(*c), fps, size)
        else:
            w = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*c), fps, size)
        if w.isOpened():
            return w, c
        w.release()
    return None, None


def save_snapshot(source, out_dir: str, sample: str = "") -> Optional[str]:
    """Write the latest full-resolution frame as PNG. Returns the path or None."""
    fr = source.latest()
    if fr is None or cv2 is None or not source.connected:
        return None
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{_stamp(sample)}.png")
    return path if cv2.imwrite(path, fr.image) else None


def record_clip(source, path: str, duration_s: float,
                should_stop: Optional[Callable[[], bool]] = None) -> dict:
    """
    BLOCKING timed recording — for the recipe action RHEED_VIDEO (runs on the recipe
    thread). Writes every frame for duration_s to `path` (MJPG .avi, as VideoRecorder).
    duration_s <= 0 → one still PNG instead (path's extension is replaced by .png).
    Returns {ok, path, frames, dropped, t_first, t_last, fps, resolution, codec, msg};
    never raises. should_stop() is polled so a recipe Stop ends the clip early (the
    partial clip is kept). Frame timestamps (t_first/t_last) define the window the
    metadata file covers.
    """
    out = {"ok": False, "path": path, "frames": 0, "dropped": 0, "t_first": None,
           "t_last": None, "fps": 0.0, "resolution": tuple(getattr(source, "resolution", (0, 0))),
           "codec": "", "msg": ""}
    try:
        if cv2 is None or source is None or not source.connected:
            out["msg"] = "camera not connected"
            return out
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if duration_s <= 0:
            fr = source.latest()
            if fr is None:
                out["msg"] = "no frame"
                return out
            png = os.path.splitext(path)[0] + ".png"
            ok = cv2.imwrite(png, fr.image)
            out.update(ok=bool(ok), path=png, frames=1, t_first=fr.t, t_last=fr.t, codec="PNG",
                       resolution=(fr.image.shape[1], fr.image.shape[0]),
                       msg="still snapshot" if ok else "PNG write failed")
            return out
        q = source.subscribe(maxsize=240)
        writer, codec = None, ""
        t_end = time.time() + duration_s
        last = time.time()
        try:
            while time.time() < t_end:
                if should_stop and should_stop():
                    out["msg"] = "stopped early (recipe stop)"
                    break
                try:
                    fr = q.get(timeout=0.5)
                except Exception:
                    if time.time() - last > STALL_TIMEOUT:
                        out["msg"] = "camera lost during the clip"
                        break
                    continue
                last = time.time()
                if writer is None:
                    h, w = fr.image.shape[:2]
                    writer, codec = _open_writer(path, (w, h), source.fps or 30.0, ("MJPG", "FFV1"))
                    if writer is None:
                        out["msg"] = "could not open a video writer"
                        return out
                    out["resolution"] = (w, h)
                writer.write(fr.image)
                out["frames"] += 1
                out["t_first"] = out["t_first"] or fr.t
                out["t_last"] = fr.t
        finally:
            source.unsubscribe(q)
            if writer:
                writer.release()
        out["dropped"], out["codec"] = q.dropped, codec
        if out["frames"] > 1:
            out["fps"] = (out["frames"] - 1) / max(out["t_last"] - out["t_first"], 1e-6)
        out["ok"] = out["frames"] > 0
        if not out["msg"]:
            out["msg"] = f"{out['frames']} frames"
        return out
    except Exception as e:                      # never raise into the recipe thread
        out["msg"] = f"clip failed: {e}"
        return out


# ══════════════════════════════════════════════════════════════════════

class VideoRecorder:
    """Record every frame to <out_dir>/<stamp>.avi until stop()."""

    def __init__(self, source):
        self.source = source
        self.path: Optional[str] = None
        self.frames = 0
        self.t_start = 0.0
        self.on_done: Optional[Callable[[Optional[str], str], None]] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, out_dir: str, sample: str = "", codec: str = "MJPG") -> bool:
        if self.busy or cv2 is None or not self.source.connected:
            return False
        os.makedirs(out_dir, exist_ok=True)
        self.path = os.path.join(out_dir, f"{_stamp(sample)}.avi")
        self.frames, self.t_start = 0, time.time()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(codec,), name="RHEED-rec", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()

    def _run(self, codec: str):
        q = self.source.subscribe(maxsize=90)
        writer, msg, last = None, "", time.time()
        try:
            while not self._stop.is_set():
                try:
                    fr = q.get(timeout=0.5)
                except Exception:
                    if time.time() - last > STALL_TIMEOUT:
                        msg = "camera lost — recording stopped"
                        break
                    continue
                last = time.time()
                if writer is None:
                    h, w = fr.image.shape[:2]
                    fps = self.source.fps or 30.0
                    writer, _ = _open_writer(self.path, (w, h), fps, (codec, "MJPG"))
                    if writer is None:
                        msg = "could not open a video writer"
                        break
                writer.write(fr.image)
                self.frames += 1
        finally:
            self.source.unsubscribe(q)
            if writer:
                writer.release()
        if self.frames == 0:
            try:
                os.remove(self.path)
            except OSError:
                pass
            path = None
        else:
            path = self.path
            dur = time.time() - self.t_start
            msg = (msg + "; " if msg else "") + \
                f"{self.frames} frames, {dur:.1f} s, {q.dropped} dropped"
        if self.on_done:
            self.on_done(path, msg)


# ══════════════════════════════════════════════════════════════════════

class RotationCapture:
    """Short clip → rotation analysis → keep best-azimuth frames → delete clip."""

    def __init__(self, source):
        self.source = source
        self.on_progress: Optional[Callable[[float, str], None]] = None
        self.on_done: Optional[Callable[[Optional[dict], str], None]] = None
        self._abort = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def abort(self):
        self._abort.set()

    def start(self, out_root: str, duration_s: float = 20.0, sample: str = "",
              roi: Optional[Tuple[int, int, int, int]] = None, expected_rpm: float = 0.0,
              n_picks: int = 4, keep_video: bool = False, lossless: bool = False) -> bool:
        if self.busy or cv2 is None or self.source.state != STATE_CONNECTED:
            return False
        self._abort.clear()
        kw = dict(out_root=out_root, duration_s=duration_s, sample=sample, roi=roi,
                  expected_rpm=expected_rpm, n_picks=n_picks, keep_video=keep_video,
                  codecs=("FFV1", "MJPG") if lossless else ("MJPG", "FFV1"))
        self._thread = threading.Thread(target=self._run_safe, kwargs=kw, name="RHEED-rot", daemon=True)
        self._thread.start()
        return True

    # ── worker ──
    def _progress(self, f: float, msg: str):
        if self.on_progress:
            self.on_progress(max(0.0, min(1.0, f)), msg)

    def _run_safe(self, **kw):
        tmp = None
        try:
            tmp = os.path.join(tempfile.gettempdir(), f"rheed_clip_{os.getpid()}_{int(time.time())}.avi")
            summary, msg = self._run(tmp, **kw)
        except Exception as e:                  # the GUI must never see an exception
            summary, msg = None, f"capture failed: {e}"
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        if self.on_done:
            self.on_done(summary, msg)

    def _run(self, tmp, out_root, duration_s, sample, roi, expected_rpm, n_picks, keep_video, codecs):
        # 1. RECORD ---------------------------------------------------------
        q = self.source.subscribe(maxsize=240)
        writer, codec = None, None
        times, thumbs = [], []
        t0 = last = time.time()
        stalled = False
        try:
            while time.time() - t0 < duration_s and not self._abort.is_set():
                try:
                    fr = q.get(timeout=0.5)
                except Exception:
                    if time.time() - last > STALL_TIMEOUT:
                        stalled = True
                        break
                    continue
                last = time.time()
                if writer is None:
                    h, w = fr.image.shape[:2]
                    writer, codec = _open_writer(tmp, (w, h), self.source.fps or 30.0, codecs)
                    if writer is None:
                        return None, "could not open a temp video writer"
                writer.write(fr.image)
                times.append(fr.t)
                thumbs.append(make_thumb(fr.image, roi))
                self._progress(0.8 * (time.time() - t0) / duration_s,
                               f"recording {time.time() - t0:4.1f}/{duration_s:.0f} s  ({len(times)} frames)")
        finally:
            self.source.unsubscribe(q)
            if writer:
                writer.release()
        if self._abort.is_set():
            return None, "capture aborted"
        dropped = q.dropped
        if len(times) < 10:
            return None, ("camera lost during capture" if stalled else "too few frames captured")

        # 2. ANALYSE --------------------------------------------------------
        self._progress(0.82, "analysing rotation…")
        exp_T = 60.0 / expected_rpm if expected_rpm > 0 else None
        res = analyze_rotation(times, thumbs, expected_period=exp_T, n_picks=n_picks)
        del thumbs

        # 3. EXTRACT --------------------------------------------------------
        out_dir = os.path.join(out_root, _stamp(sample))
        os.makedirs(out_dir, exist_ok=True)
        self._progress(0.86, "extracting frames…")
        files = self._extract(tmp, res, out_dir)
        self._write_tables(out_dir, res, times, dict(
            sample=sample, duration_s=duration_s, roi=roi, expected_rpm=expected_rpm,
            n_picks=n_picks, codec=codec, dropped_frames=dropped,
            camera_resolution=list(self.source.resolution), stalled=stalled), files)

        # 4. CLEAN UP -------------------------------------------------------
        if keep_video:
            shutil.move(tmp, os.path.join(out_dir, "clip.avi"))
        self._progress(1.0, "done")
        summary = {"out_dir": out_dir, "result": res, "files": files,
                   "dropped": dropped, "stalled": stalled}
        msg = res.message + (f"  ({dropped} frames dropped)" if dropped else "")
        if stalled:
            msg = "camera lost mid-capture — analysed partial clip. " + msg
        return summary, msg

    def _extract(self, path: str, res: RotationResult, out_dir: str) -> list:
        """One sequential read of the clip; returns [{angle, best, avg, n_avg}]."""
        need = {}
        for k, p in enumerate(res.picks):
            need.setdefault(p.best_index, []).append(("best", k))
            for m in p.members:
                need.setdefault(m, []).append(("avg", k))
        if not need:
            return []
        sums = [None] * len(res.picks)
        counts = [0] * len(res.picks)
        bests = [None] * len(res.picks)
        cap = cv2.VideoCapture(path)
        last = max(need)
        i = 0
        try:
            while i <= last:
                ok, img = cap.read()
                if not ok:
                    break
                for kind, k in need.get(i, ()):
                    if kind == "best":
                        bests[k] = img
                    else:
                        sums[k] = img.astype(np.float32) if sums[k] is None else sums[k] + img
                        counts[k] += 1
                i += 1
                if i % 30 == 0:
                    self._progress(0.86 + 0.12 * i / (last + 1), "extracting frames…")
        finally:
            cap.release()

        files = []
        for k, p in enumerate(res.picks):
            tag = f"az{k + 1}_{p.angle_deg:05.1f}deg" if np.isfinite(p.angle_deg) else f"frame{k + 1}"
            entry = {"angle_deg": None if not np.isfinite(p.angle_deg) else round(p.angle_deg, 2),
                     "score": round(p.score, 3), "best_index": p.best_index, "members": p.members}
            if bests[k] is not None:
                f = os.path.join(out_dir, f"{tag}_best.png")
                cv2.imwrite(f, bests[k])
                entry["best"] = f
            if counts[k] > 1:
                f = os.path.join(out_dir, f"{tag}_avg{counts[k]}.png")
                cv2.imwrite(f, np.clip(sums[k] / counts[k] + 0.5, 0, 255).astype(np.uint8))
                entry["avg"], entry["n_avg"] = f, counts[k]
            files.append(entry)
        return files

    @staticmethod
    def _write_tables(out_dir, res: RotationResult, times, settings, files):
        t0 = times[0]
        with open(os.path.join(out_dir, "frames.csv"), "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["Frame", "Unix_Time", "Elapsed_s", "Azimuth_deg", "Quality"])
            for i, t in enumerate(times):
                az = f"{res.angles[i]:.2f}" if len(res.angles) else ""
                w.writerow([i, f"{t:.4f}", f"{t - t0:.4f}", az, f"{res.quality[i]:.4f}"])
        summary = {
            "timestamp": datetime.fromtimestamp(t0).isoformat(timespec="seconds"),
            "settings": settings,
            "n_frames": res.n_frames, "duration_s": round(res.duration_s, 3),
            "fps": round(res.fps, 2),
            "period_s": None if res.period_s is None else round(res.period_s, 4),
            "rpm": None if res.rpm is None else round(res.rpm, 3),
            "confidence": round(res.confidence, 3), "fold": res.fold,
            "orientation": res.orientation, "detrend_s": round(res.detrend_s, 2),
            "message": res.message, "warning": res.warning,
            "azimuths": [{k: v for k, v in f.items() if k != "members"} | {"n_members": len(f["members"])}
                         for f in files],
            "note": "azimuth angles are relative to the first frame of the clip",
        }
        with open(os.path.join(out_dir, "rotation_summary.json"), "w") as fh:
            json.dump(summary, fh, indent=2)
