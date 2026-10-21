"""
Export of a video-analysis session — Qt-free. Called ONLY from the panel's ③ Export
step (nothing is written automatically; user request 2026-10-06).

export_analysis(out_dir, summary, opts, spatial=None, extra=()) → list of written paths

  opts (bools): images (best + averaged PNG per kept azimuth), avg16 (averaged images
    as 16-bit PNG, grey ×256 — keeps the sub-grey-level precision averaging buys),
    enhanced (contrast-stretched _enh.png copies, viewing only), tables (frames.csv),
    summary (analysis_summary.json), spatial (profile / peaks / intensity / locked /
    identification CSVs + kymograph PNG).
  spatial: dict assembled by the GUI from its current spatial-analysis state (see
    video_steps.SpatialStep.report()).
  extra: callables f(out_dir) → [paths] (e.g. plot screenshots rendered by the GUI).
"""

import csv
import json
import os
import re
from datetime import datetime

import numpy as np

try:
    import cv2
except ImportError:
    cv2 = None

from .video_analysis import enhance


def safe_name(name: str) -> str:
    """Folder name typed by the user → safe single path component."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", (name or "").strip()).strip(" .")
    return s[:120]


def default_folder_name(sample: str = "") -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    s = safe_name(sample)
    return f"{ts}_{s}" if s else ts


def _jsonable(v):
    if isinstance(v, (np.floating,)):
        return float(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, np.ndarray):
        return v.tolist()
    if isinstance(v, float) and not np.isfinite(v):
        return None
    return v


def _tag(k, p):
    a = p["angle_deg"]
    return f"az{k + 1}_{a:05.1f}deg" if a is not None else f"frame{k + 1}"


def _write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return path


def export_analysis(out_dir, summary, opts, spatial=None, extra=()):
    os.makedirs(out_dir, exist_ok=True)
    written = []
    res = summary["result"]
    picks = summary["picks"]

    if opts.get("images") or opts.get("avg16") or opts.get("enhanced"):
        for k, p in enumerate(picks):
            tag = _tag(k, p)
            if p.get("best") is not None:
                if opts.get("images"):
                    f = os.path.join(out_dir, f"{tag}_best.png"); cv2.imwrite(f, p["best"]); written.append(f)
                if opts.get("enhanced"):
                    f = os.path.join(out_dir, f"{tag}_best_enh.png"); cv2.imwrite(f, enhance(p["best"])); written.append(f)
            avg = p.get("avg")
            if avg is not None:
                n = p["n_avg"]
                if opts.get("images"):
                    f = os.path.join(out_dir, f"{tag}_avg{n}.png")
                    cv2.imwrite(f, np.clip(avg + 0.5, 0, 255).astype(np.uint8)); written.append(f)
                if opts.get("avg16"):
                    f = os.path.join(out_dir, f"{tag}_avg{n}_16bit.png")
                    cv2.imwrite(f, np.clip(avg * 256.0 + 0.5, 0, 65535).astype(np.uint16)); written.append(f)
                if opts.get("enhanced"):
                    f = os.path.join(out_dir, f"{tag}_avg{n}_enh.png"); cv2.imwrite(f, enhance(avg)); written.append(f)

    if opts.get("tables"):
        t = summary["times"]; t0 = summary["t0"]
        rows = [[summary["frame_numbers"][i], f"{t[i]:.4f}", f"{t[i] - t0:.4f}",
                 f"{res.angles[i]:.2f}" if len(res.angles) else "", f"{res.quality[i]:.4f}"]
                for i in range(len(t))]
        written.append(_write_csv(os.path.join(out_dir, "frames.csv"),
                                  ["File_Frame", "Video_Time_s", "Elapsed_s", "Azimuth_deg", "Quality"], rows))

    if spatial and opts.get("spatial"):
        written += _export_spatial(out_dir, spatial)

    if opts.get("summary"):
        doc = {
            "exported": datetime.now().isoformat(timespec="seconds"),
            "settings": summary["settings"],
            "rotation": {
                "n_frames": res.n_frames, "duration_s": round(res.duration_s, 3),
                "analysis_fps": round(res.fps, 3),
                "period_s": None if res.period_s is None else round(res.period_s, 4),
                "rpm": None if res.rpm is None else round(res.rpm, 4),
                "confidence": round(res.confidence, 3), "fold": res.fold,
                "orientation": res.orientation, "detrend_s": round(res.detrend_s, 2),
                "message": res.message, "warning": res.warning,
                "note": "azimuth angles are relative to the first analysed frame (start_s)"},
            "azimuths": [{k: _jsonable(v) for k, v in p.items() if k not in ("best", "avg")}
                         for p in picks],
        }
        if spatial:
            doc["spatial"] = {k: _jsonable(v) for k, v in spatial.get("summary", {}).items()}
        f = os.path.join(out_dir, "analysis_summary.json")
        with open(f, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, default=_jsonable)
        written.append(f)

    for fn in extra:
        try:
            written += list(fn(out_dir) or [])
        except Exception:
            pass
    return written


def _export_spatial(out_dir, sp):
    out = []
    az = sp.get("azimuth")
    tag = f"_az{az:05.1f}" if az is not None else ""
    if sp.get("profile") is not None:
        pos, prof, hp = sp["profile"]
        out.append(_write_csv(os.path.join(out_dir, f"profile{tag}.csv"),
                              ["Position_px", "Profile_grey", "Minus_baseline"],
                              [[f"{a:.1f}", f"{b:.4f}", f"{c:.4f}"] for a, b, c in zip(pos, prof, hp)]))
    if sp.get("peaks"):
        d = sp.get("d_per_peak") or [None] * len(sp["peaks"])
        orders = sp.get("orders") or [None] * len(sp["peaks"])
        out.append(_write_csv(os.path.join(out_dir, f"peaks{tag}.csv"),
                              ["Position_px", "Order", "Height_grey", "FWHM_px", "SNR"],
                              [[f"{p.pos:.2f}", "" if o is None else o, f"{p.height:.4f}",
                                f"{p.fwhm:.2f}", f"{p.snr:.1f}"] for p, o in zip(sp["peaks"], orders)]))
    if sp.get("intensity") is not None:
        it = sp["intensity"]
        cols = [("Video_Time_s", it["t"], "{:.4f}"), ("Azimuth_deg", it.get("az"), "{:.2f}"),
                ("Intensity", it["I"], "{:.5f}"), ("Rotation_normalised", it.get("norm"), "{:.5f}")]
        cols = [c for c in cols if c[1] is not None]
        rows = [[("" if not np.isfinite(c[1][i]) else c[2].format(c[1][i])) for c in cols]
                for i in range(len(it["t"]))]
        out.append(_write_csv(os.path.join(out_dir, f"intensity_all_frames{tag}.csv"),
                              [c[0] for c in cols], rows))
    if sp.get("locked") is not None and len(sp["locked"][0]):
        t, I, s, n = sp["locked"]
        out.append(_write_csv(os.path.join(out_dir, f"intensity_azimuth_locked{tag}.csv"),
                              ["Video_Time_s", "Intensity_mean", "Intensity_std", "N_frames"],
                              [[f"{a:.4f}", f"{b:.5f}", f"{c:.5f}", int(d)] for a, b, c, d in zip(t, I, s, n)]))
    if sp.get("candidates"):
        out.append(_write_csv(os.path.join(out_dir, "identification.csv"),
                              ["Rel_error_pct", "Material", "Surface", "Beam_azimuth", "d_row_A", "Source"],
                              [[f"{e * 100:+.2f}", m.name, m.surface, lab, f"{dr:.4f}", m.source]
                               for e, m, lab, dr in sp["candidates"]]))
    if sp.get("kymograph") is not None and cv2 is not None:
        P = sp["kymograph"]                        # (n_frames, m) → image: rows=position, cols=time
        img = enhance(P.T.astype(np.float32))
        f = os.path.join(out_dir, "kymograph_profile_vs_time.png")
        cv2.imwrite(f, img)
        out.append(f)
    return out
