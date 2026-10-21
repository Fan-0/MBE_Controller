"""
RHEED camera probe — find the HDMI capture device and check what it delivers.

Usage (from vac_control/, RHEED app closed so the device is free):
    python diagnostics/rheed_camera_probe.py                 probe indices 0-5, DirectShow
    python diagnostics/rheed_camera_probe.py --index 1       detail one device: resolutions,
                                                             formats, fps, saves a test frame
Checklist when nothing shows up:
  * Canon 90D powered on, in movie/live-view mode, HDMI info display OFF (clean output)
  * HDMI cable into the capture device, capture device into the laptop USB-C port
  * Windows Settings → Privacy → Camera → "Let desktop apps access your camera" = ON
  * Device Manager → Cameras: the capture device (e.g. "INOGENI 4KXPLUS") is listed
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
from rheed.camera import list_cameras

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "rheed", "probe")


def detail(idx):
    os.makedirs(OUT, exist_ok=True)
    for fmt in (None, "MJPG", "YUY2"):
        for w, h in ((1920, 1080), (1280, 720), (3840, 2160)):
            # one combination failing (driver exception, odd FOURCC) must not end the probe
            cap = None
            try:
                cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
                if not cap.isOpened():
                    print(f"  index {idx}: cannot open (in use by another program?)"); return
                if fmt:
                    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fmt))
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, w); cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
                cap.set(cv2.CAP_PROP_FPS, 30)
                ok, img = cap.read()
                n, t0 = 0, time.time()
                while ok and time.time() - t0 < 2.0:
                    ok, img = cap.read(); n += 1
                fps = n / (time.time() - t0)
                # some drivers report the FOURCC as a negative int (2026-10-07: crashed
                # the probe on a laptop webcam) → mask to 32 bits before decoding
                fc = (int(cap.get(cv2.CAP_PROP_FOURCC)) & 0xFFFFFFFF).to_bytes(4, "little").decode(errors="replace")
                if img is not None:
                    ah, aw = img.shape[:2]
                    name = f"probe_cam{idx}_{fmt or 'auto'}_{aw}x{ah}.png"
                    cv2.imwrite(os.path.join(OUT, name), img)
                    print(f"  req {fmt or 'auto':4} {w}x{h} → got {aw}x{ah} fourcc={fc!r} "
                          f"{fps:5.1f} fps  mean={img.mean():5.1f} std={img.std():5.1f}  → {name}")
                else:
                    print(f"  req {fmt or 'auto':4} {w}x{h} → no frame")
            except Exception as e:
                print(f"  req {fmt or 'auto':4} {w}x{h} → error: {e}")
            finally:
                if cap is not None:
                    cap.release()
    print(f"\nTest frames in {OUT}\n"
          "std < 2 means a blank image: no HDMI signal / camera asleep / beam off.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=int, help="detail one device")
    ap.add_argument("--max", type=int, default=6, help="highest index to scan (exclusive)")
    a = ap.parse_args()
    if a.index is not None:
        print(f"Detailing camera index {a.index} (DirectShow):"); detail(a.index); return
    print(f"Scanning DirectShow indices 0..{a.max - 1} …")
    cams = list_cameras(a.max)
    if not cams:
        print("No cameras found. See the checklist at the top of this file.")
    for c in cams:
        print(f"  [{c['index']}] {c['name']:40s} default frame {c['width']}x{c['height']}")
    print("\nThe built-in laptop webcam is usually index 0. Re-run with --index N on the\n"
          "capture device to see which resolution/format combination it accepts.")


if __name__ == "__main__":
    main()
