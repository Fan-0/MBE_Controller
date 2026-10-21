"""
Azimuth-locked spatial analysis of a RHEED video — Qt-free, numpy only (requests only
for the optional COD lookup). Runs on the per-frame line profiles that
video_analysis.VideoAnalysis extracts at the full frame rate.

  ProfileSeries          every frame's profile ACROSS the streaks inside the profile band
                         (mean along the streak direction) — a kymograph (time × position)
  azimuth_profile()      mean profile of the frames within ±tol of one azimuth (any rev)
  find_peaks()           streaks/spots on that profile (baseline-subtracted, σ threshold)
  fit_spacing()          streak spacing Δ (px) from the peak positions: integer orders
                         n about a reference (00) peak, least-squares x = x0 + n·Δ;
                         peaks halfway between orders are flagged (2× reconstruction)
  peak_intensity()       background-subtracted intensity of one peak, every frame
  azimuth_locked()       … averaged once per revolution at the chosen azimuth
  rotation_normalised()  … divided by its mean azimuthal dependence → growth-only signal
  oscillation_period()   FFT of an intensity trace → dominant period → growth rate
  Calibration            px → Å: Δ = L·λ/d   (reference spacing OR beam keV + geometry)
  identify()             measured row spacing d → candidate (material, surface, azimuth)
  cod_lookup()           Crystallography Open Database query by formula → surface nets

RHEED geometry used for identification (kinematic, 1×1 surface):
  streak spacing on the screen  Δ = L·λ / d_row
  d_row = spacing of the atomic rows PARALLEL to the beam:
    hexagonal net a   beam ∥ ⟨11-20⟩ (along a lattice vector): d = a·√3/2
                      beam ∥ ⟨1-100⟩ (30° from it):            d = a/2      ratio √3
    square net a      beam ∥ ⟨10⟩: d = a        beam ∥ ⟨11⟩: d = a/√2      ratio √2
    rectangular a×b   beam ∥ a: d = b   beam ∥ b: d = a   diagonal: d = ab/√(a²+b²)
  Surface nets from bulk cells (cod_lookup / materials.csv): hexagonal & trigonal (0001)
  → hex a; cubic F (fcc/diamond/zincblende/rock salt) (001) → square a/√2, (111) → hex
  a/√2; cubic P/I (001) → square a, (111) → hex a·√2; tetragonal (001) → square a;
  orthorhombic (001) → rect a×b. Reconstructions add fractional-order streaks
  (fit_spacing flags half orders) and are NOT matched automatically.
"""

import csv
import math
import os
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import numpy as np

MATERIALS_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "materials.csv")
SQ3 = math.sqrt(3.0)
SQ2 = math.sqrt(2.0)


def _circ_dist(a, b):
    d = np.abs(np.asarray(a) - b) % 360.0
    return np.minimum(d, 360.0 - d)


def _running_mean(y: np.ndarray, k: int) -> np.ndarray:
    k = max(3, int(k)) | 1
    return np.convolve(np.pad(y, k // 2, mode="edge"), np.ones(k) / k, mode="valid")


def _gauss_smooth(y: np.ndarray, sigma: float) -> np.ndarray:
    if sigma <= 0:
        return y
    r = int(3 * sigma + 0.5)
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2); k /= k.sum()
    return np.convolve(np.pad(y, r, mode="edge"), k, mode="valid")


# ══════════════════════════════════════════════════════════════════════
# Profiles
# ══════════════════════════════════════════════════════════════════════

@dataclass
class ProfileSeries:
    times: np.ndarray            # (n,) video time of each profiled frame, s
    pos: np.ndarray              # (m,) pixel coordinate along the profile (full-res frame)
    P: np.ndarray                # (n, m) float32 grey-level profiles
    orientation: str             # streak direction ("horizontal" → profile along y)
    band: Tuple[int, int, int, int]   # (x, y, w, h) frame region the profiles come from

    def azimuths(self, T: float, t0: float) -> np.ndarray:
        return ((self.times - t0) % T) / T * 360.0


def frame_profile(grey: np.ndarray, band, orientation: str) -> np.ndarray:
    """Profile across the streaks: mean ALONG the streaks inside band (x, y, w, h)."""
    x, y, w, h = band
    crop = grey[y:y + h, x:x + w]
    return crop.mean(axis=1 if orientation == "horizontal" else 0).astype(np.float32)


def profile_axis(band, orientation: str) -> np.ndarray:
    x, y, w, h = band
    return (y + np.arange(h)) if orientation == "horizontal" else (x + np.arange(w))


def azimuth_mask(series: ProfileSeries, T: float, t0: float, az: float, tol: float) -> np.ndarray:
    return _circ_dist(series.azimuths(T, t0), az) <= tol


def azimuth_profile(series: ProfileSeries, T: Optional[float], t0: float, az: float,
                    tol: float) -> Tuple[np.ndarray, int]:
    """Mean profile at one azimuth (all revolutions). T None → mean of all frames."""
    m = azimuth_mask(series, T, t0, az, tol) if T else np.ones(len(series.times), bool)
    if not m.any():
        return np.full(series.P.shape[1], np.nan, np.float32), 0
    return series.P[m].mean(axis=0), int(m.sum())


# ══════════════════════════════════════════════════════════════════════
# Peaks & spacing
# ══════════════════════════════════════════════════════════════════════

@dataclass
class Peak:
    pos: float          # px (sub-pixel, parabolic)
    height: float       # above baseline, grey levels
    fwhm: float         # px
    snr: float          # height / profile noise (σ)


def baseline(prof: np.ndarray, width: int) -> np.ndarray:
    """Broad background under the streaks: running mean over `width` px of the profile
    after clipping it to a running minimum, so peaks don't pull their own baseline up."""
    w = max(5, int(width)) | 1
    pad = np.pad(np.asarray(prof, np.float64), w // 2, mode="edge")
    mins = np.lib.stride_tricks.sliding_window_view(pad, w).min(axis=1)
    base = _running_mean(mins, w)
    # a rolling minimum sits under the noise; lift it to the median residual (the
    # background level, as long as the peaks cover less than half the profile)
    return base + float(np.median(prof - base))


def find_peaks(prof: np.ndarray, bg_width: int = 100, sigma_thresh: float = 5.0,
               min_dist: int = 20, smooth_px: float = 2.0, max_fwhm: Optional[float] = None,
               axis: Optional[np.ndarray] = None) -> Tuple[List[Peak], np.ndarray, float]:
    """Peaks of (smoothed profile − baseline) above sigma_thresh × noise.
    Peaks wider than max_fwhm (default: bg_width) are diffuse glow, not streaks, and are
    dropped — on MVI_2527 the glow under the spot made 150–300 px "peaks" between the
    30–60 px streaks and fooled the spacing fit. axis: pixel coordinate of each profile
    sample (ProfileSeries.pos) → Peak.pos in frame pixels; default = index.
    Returns (peaks sorted by position, baseline-subtracted profile, noise σ)."""
    max_fwhm = bg_width if max_fwhm is None else max_fwhm
    p = _gauss_smooth(np.asarray(prof, np.float64), smooth_px)
    hp = p - baseline(p, bg_width)
    d = np.diff(np.asarray(prof, np.float64))           # noise from the RAW profile
    noise = float(1.4826 * np.median(np.abs(d - np.median(d))) / SQ2) or 1e-6
    cand = np.where((hp[1:-1] > hp[:-2]) & (hp[1:-1] >= hp[2:]) & (hp[1:-1] > sigma_thresh * noise))[0] + 1
    cand = sorted(cand, key=lambda i: -hp[i])
    kept: List[int] = []
    for i in cand:                                       # strongest first, enforce spacing
        if all(abs(i - j) >= min_dist for j in kept):
            kept.append(i)
    peaks = []
    for i in sorted(kept):
        a, b, c = hp[i - 1], hp[i], hp[i + 1]
        den = a - 2 * b + c
        off = 0.5 * (a - c) / den if den < 0 else 0.0
        half = b / 2
        l = i
        while l > 0 and hp[l] > half:
            l -= 1
        r = i
        while r < len(hp) - 1 and hp[r] > half:
            r += 1
        # linear interpolation of the half-maximum crossings
        lx = l + (half - hp[l]) / (hp[l + 1] - hp[l]) if hp[l + 1] != hp[l] else l
        rx = r - (half - hp[r]) / (hp[r - 1] - hp[r]) if hp[r - 1] != hp[r] else r
        if rx - lx > max_fwhm:
            continue
        x0 = float(axis[0]) if axis is not None else 0.0
        peaks.append(Peak(pos=x0 + i + off, height=float(b), fwhm=float(rx - lx), snr=float(b / noise)))
    return peaks, hp, noise


@dataclass
class SpacingResult:
    delta: float                      # px between adjacent orders
    err: float                        # 1σ (px)
    x0: float                         # position of order 0 (px)
    orders: List[Optional[int]]       # per peak: integer order, or None (not on the grid)
    half_orders: List[int] = field(default_factory=list)   # peak indices at n + ½
    n_used: int = 0
    message: str = ""


def fit_spacing(peaks: Sequence[Peak], ref: Optional[int] = None, tol: float = 0.2,
                delta0: Optional[float] = None) -> Optional[SpacingResult]:
    """Streak spacing from peak positions. ref = index of the (00) peak (default:
    the strongest). Orders n = round((x − x_ref)/Δ0) with Δ0 = the smallest robust
    neighbour gap; then least squares x = x0 + n·Δ over the peaks within tol·Δ of the
    grid. Peaks near n+½ are reported as half orders (possible 2× reconstruction)."""
    if len(peaks) < 2:
        return None
    x = np.array([p.pos for p in peaks])
    if ref is None or not 0 <= ref < len(peaks):
        ref = int(np.argmax([p.height for p in peaks]))
    # start from the strongest neighbours of the reference peak: Δ0 = the nearest strong
    # peak's distance (weak half-order streaks must not halve the estimate)
    h = np.array([p.height for p in peaks])
    strong = (h >= 0.3 * h[ref]) & (np.arange(len(x)) != ref)
    d_cand = np.abs(x[strong] - x[ref]) if strong.any() else np.abs(np.delete(x, ref) - x[ref])
    if not len(d_cand) or d_cand.min() <= 0:
        return None
    x0, d0 = float(x[ref]), float(delta0 or d_cand.min())   # delta0: user-picked seed
    for _ in range(3):                                  # refine Δ, x0 by least squares
        n = np.round((x - x0) / d0)
        on = np.abs((x - x0) / d0 - n) <= tol
        if on.sum() < 2 or len(np.unique(n[on])) < 2:
            break
        sol, *_ = np.linalg.lstsq(np.c_[np.ones(on.sum()), n[on]], x[on], rcond=None)
        x0, d0 = float(sol[0]), float(sol[1])
    n = np.round((x - x0) / d0)
    res = (x - x0) / d0 - n
    on = np.abs(res) <= tol
    orders = [int(k) if o else None for k, o in zip(n, on)]
    half = [i for i, r in enumerate(res) if abs(abs(r) - 0.5) <= tol and not on[i]]
    if on.sum() >= 3:
        A = np.c_[np.ones(on.sum()), n[on]]
        r = x[on] - A @ np.array([x0, d0])
        cov = np.linalg.inv(A.T @ A) * (r @ r) / max(1, on.sum() - 2)
        err = float(math.sqrt(max(cov[1, 1], 0.0)))
    else:
        err = float("nan")
    msg = f"Δ = {abs(d0):.2f} px from {int(on.sum())} orders"
    if half:
        msg += f"; {len(half)} peak(s) at half order — possible ×2 reconstruction"
    return SpacingResult(delta=abs(d0), err=err, x0=x0, orders=orders, half_orders=half,
                         n_used=int(on.sum()), message=msg)


# ══════════════════════════════════════════════════════════════════════
# Intensity vs time
# ══════════════════════════════════════════════════════════════════════

def peak_intensity(series: ProfileSeries, pos: float, half_w: float, gap: float = 4.0,
                   bg_w: float = 10.0) -> np.ndarray:
    """Per-frame intensity of a peak: mean over [pos ± half_w] minus the mean of two
    flanking background windows (width bg_w, gap px outside the peak window)."""
    i = int(round(pos - series.pos[0]))
    hw = max(1, int(round(half_w)))
    m = series.P.shape[1]
    lo, hi = max(0, i - hw), min(m, i + hw + 1)
    g, bw = int(round(gap)), max(2, int(round(bg_w)))
    l0, l1 = max(0, lo - g - bw), max(0, lo - g)
    r0, r1 = min(m, hi + g), min(m, hi + g + bw)
    sig = series.P[:, lo:hi].mean(axis=1)
    sides = [series.P[:, a:b].mean(axis=1) for a, b in ((l0, l1), (r0, r1)) if b > a]
    bg = np.mean(sides, axis=0) if sides else 0.0
    return (sig - bg).astype(np.float64)


def azimuth_locked(series: ProfileSeries, I: np.ndarray, T: float, t0: float, az: float,
                   tol: float):
    """One point per revolution: mean I over the frames within ±tol of az.
    Returns (t_mean, I_mean, I_std, n_frames) arrays."""
    m = azimuth_mask(series, T, t0, az, tol)
    t_az = t0 + az / 360.0 * T
    rev = np.round((series.times - t_az) / T).astype(int)
    out = []
    for r in np.unique(rev[m]):
        s = m & (rev == r)
        out.append((series.times[s].mean(), I[s].mean(), I[s].std(), s.sum()))
    if not out:
        return (np.zeros(0),) * 4
    return tuple(np.array(c) for c in zip(*out))


def rotation_normalised(series: ProfileSeries, I: np.ndarray, T: float, t0: float,
                        n_bins: int = 72, min_frac: float = 0.2):
    """I(t) / A(φ(t)), A = median intensity per azimuth bin over all revolutions — divides
    out the rotation modulation, leaving the growth-related change (approximate: assumes
    growth scales every azimuth alike). Bins with A < min_frac × max(A) → NaN."""
    az = series.azimuths(T, t0)
    b = np.minimum((az / 360.0 * n_bins).astype(int), n_bins - 1)
    A = np.array([np.median(I[b == k]) if (b == k).any() else np.nan for k in range(n_bins)])
    ok = A >= min_frac * np.nanmax(A)
    g = np.full_like(I, np.nan)
    sel = ok[b]
    g[sel] = I[sel] / A[b[sel]]
    return g, A


def oscillation_period(t: np.ndarray, y: np.ndarray, pmin: float, pmax: float,
                       detrend_s: Optional[float] = None):
    """Dominant period in [pmin, pmax] s of y(t). NaNs are interpolated, a running mean
    (detrend_s, default 2·pmax) is removed, Hann-windowed FFT. Returns
    (period or None, freqs, power)."""
    t = np.asarray(t, float); y = np.asarray(y, float)
    ok = np.isfinite(y)
    if ok.sum() < 16:
        return None, np.zeros(0), np.zeros(0)
    dt = float(np.median(np.diff(t)))
    tu = np.arange(t[ok][0], t[ok][-1], dt)
    yu = np.interp(tu, t[ok], y[ok])
    k = int(round((detrend_s or 2 * pmax) / dt))
    if 3 <= k < len(yu):
        yu = yu - _running_mean(yu, k)
    yu = (yu - yu.mean()) * np.hanning(len(yu))
    f = np.fft.rfftfreq(len(yu), dt)
    pw = np.abs(np.fft.rfft(yu)) ** 2
    band = (f >= 1.0 / pmax) & (f <= 1.0 / pmin)
    if not band.any():
        return None, f, pw
    i = np.where(band)[0][np.argmax(pw[band])]
    if 0 < i < len(pw) - 1:                              # parabolic refinement (log power)
        a, b, c = np.log(pw[i - 1:i + 2] + 1e-30)
        den = a - 2 * b + c
        fi = f[i] + (0.5 * (a - c) / den * (f[1] - f[0]) if den < 0 else 0.0)
    else:
        fi = f[i]
    return (1.0 / fi if fi > 0 else None), f, pw


# ══════════════════════════════════════════════════════════════════════
# Calibration & identification
# ══════════════════════════════════════════════════════════════════════

def electron_wavelength(kev: float) -> float:
    """Relativistic electron wavelength, Å (λ = 12.2643/√(V(1 + 0.97848e-6 V)))."""
    V = kev * 1000.0
    return 12.2643 / math.sqrt(V * (1.0 + 0.97848e-6 * V))


@dataclass
class Calibration:
    """k = Δ_px · d_Å — the product is constant for a fixed beam energy, camera length,
    camera zoom and screen position (Δ = L·λ/d). Re-calibrate when any of those change."""
    k: Optional[float] = None
    source: str = ""

    @classmethod
    def from_reference(cls, delta_px: float, d_ref: float, label: str = ""):
        return cls(k=delta_px * d_ref, source=f"reference {label} d={d_ref:.4f} Å at Δ={delta_px:.2f} px")

    @classmethod
    def from_geometry(cls, kev: float, camera_mm: float, mm_per_px: float):
        lam = electron_wavelength(kev)
        return cls(k=camera_mm * lam / mm_per_px,
                   source=f"geometry {kev:g} keV (λ={lam:.5f} Å), L={camera_mm:g} mm, {mm_per_px:g} mm/px")

    def d(self, delta_px: float) -> Optional[float]:
        return self.k / delta_px if (self.k and delta_px) else None


@dataclass
class Material:
    name: str
    lattice: str          # hex | square | rect  (2D surface net)
    a: float
    b: float = 0.0
    surface: str = ""     # e.g. "(0001)"
    source: str = ""


def row_spacings(m: Material) -> List[Tuple[str, float]]:
    """(beam azimuth label, row spacing Å) for the low-index azimuths of a surface net."""
    if m.lattice == "hex":
        return [("⟨11-20⟩", m.a * SQ3 / 2), ("⟨1-100⟩", m.a / 2)]
    if m.lattice == "square":
        return [("⟨10⟩", m.a), ("⟨11⟩", m.a / SQ2)]
    if m.lattice == "rect" and m.b:
        return [("∥a", m.b), ("∥b", m.a), ("⟨11⟩", m.a * m.b / math.hypot(m.a, m.b))]
    return []


def load_materials(path: str = MATERIALS_CSV) -> List[Material]:
    out = []
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(row for row in fh if not row.lstrip().startswith("#")):
                try:
                    out.append(Material(name=r["name"].strip(), lattice=r["lattice"].strip(),
                                        a=float(r["a"]), b=float(r.get("b") or 0),
                                        surface=(r.get("surface") or "").strip(),
                                        source=(r.get("source") or "").strip()))
                except (KeyError, ValueError):
                    continue
    except OSError:
        pass
    return out


def identify(d: float, materials: Sequence[Material], tol: float = 0.05, n_max: int = 15):
    """Candidates whose low-index row spacing is within tol (fraction) of d.
    Returns [(rel_error, Material, azimuth label, d_row)], best first."""
    hits = []
    for m in materials:
        for lab, dr in row_spacings(m):
            e = (d - dr) / dr
            if abs(e) <= tol:
                hits.append((e, m, lab, dr))
    hits.sort(key=lambda h: abs(h[0]))
    return hits[:n_max]


RATIO_HINTS = [(1.0, "same family"), (SQ2, "√2 → square net (⟨10⟩ vs ⟨11⟩, 45° apart)"),
               (SQ3, "√3 → hexagonal net (⟨1-100⟩ vs ⟨11-20⟩, 30° apart)"), (2.0, "2 → ×2 reconstruction or rect net")]


def ratio_hint(delta1: float, delta2: float, tol: float = 0.04) -> str:
    r = max(delta1, delta2) / min(delta1, delta2)
    for v, txt in RATIO_HINTS:
        if abs(r - v) / v <= tol:
            return f"ratio {r:.3f} ≈ {txt}"
    return f"ratio {r:.3f} — no simple match"


# ── online: Crystallography Open Database ─────────────────────────────

def surface_nets(a: float, b: float, c: float, gamma: float, sg: str) -> List[Material]:
    """Low-index surface nets from a bulk cell (see module docstring for the rules)."""
    sg = (sg or "").strip()
    letter = sg[:1].upper()
    hexlike = abs(gamma - 120) < 1 and abs(a - b) < 1e-3
    out = []
    if hexlike:
        out.append(Material("", "hex", a, surface="(0001)"))
    elif abs(a - b) < 1e-3 and abs(b - c) < 1e-3:          # cubic
        if letter == "F":
            out += [Material("", "square", a / SQ2, surface="(001)"),
                    Material("", "hex", a / SQ2, surface="(111)")]
        else:
            out += [Material("", "square", a, surface="(001)"),
                    Material("", "hex", a * SQ2, surface="(111)")]
    elif abs(a - b) < 1e-3:                                  # tetragonal
        out.append(Material("", "square", a, surface="(001)"))
    else:                                                    # orthorhombic & lower: (001) rect
        out.append(Material("", "rect", a, b, surface="(001)"))
    return out


def cod_lookup(formula: str, timeout: float = 20.0) -> List[Material]:
    """Query COD (crystallography.net, no key needed) by formula, e.g. "In2 Se3" (Hill
    order, space separated — normalised here). Raises on network errors (call from a
    worker thread). Returns one Material per surface net per distinct structure."""
    import re
    import requests
    parts = re.findall(r"([A-Z][a-z]?)(\d*\.?\d*)", formula.replace(" ", ""))
    els = {e: n for e, n in parts if e}
    keys = sorted(els, key=lambda e: (e != "C", e != "H", e)) if "C" in els else sorted(els)
    hill = " ".join(f"{e}{els[e] if els[e] not in ('', '1') else ''}" for e in keys)
    r = requests.get("https://www.crystallography.net/cod/result",
                     params={"formula": hill, "format": "json"}, timeout=timeout)
    r.raise_for_status()
    seen, out = set(), []
    for e in r.json():
        try:
            a, b, c = float(e["a"]), float(e["b"]), float(e["c"])
            g = float(e["gamma"])
        except (KeyError, TypeError, ValueError):
            continue
        sg = (e.get("sg") or "").strip()
        key = (round(a, 2), round(b, 2), round(c, 1), sg)
        if key in seen:
            continue
        seen.add(key)
        for m in surface_nets(a, b, c, g, sg):
            m.name = f"{formula} {sg}"
            m.source = f"COD {e.get('file', '')} (a={a:g} c={c:g})"
            out.append(m)
    return out
