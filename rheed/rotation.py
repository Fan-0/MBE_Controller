"""
Rotation synchronisation for RHEED on a rotating substrate.

Problem: with substrate rotation on, the RHEED pattern cycles through every
azimuth once per revolution. Only a few azimuths (the principal directions,
e.g. [110] / [1-10] / [100]) give the sharp streak patterns worth keeping.

Approach (pure numpy, Qt-free, runs on a recorded clip):
  1. Every frame is reduced to a small grey thumbnail (make_thumb) — the whole
     analysis runs on these, so a 20 s clip costs ~30 MB of RAM, not GBs.
  2. The clip's MEAN image is subtracted from every thumbnail. What is left is the
     part of the pattern that changes with azimuth; the static shadow edge and
     background glow would otherwise make every frame look alike.
  3. Self-similarity vs time lag: the normalised correlation of every frame pair,
     averaged in bins of their time separation. The rotation period is the lag of
     the strongest peak. Using real timestamps (not frame indices) means dropped
     frames do not bias the period.
  4. Each frame gets an azimuth angle = 360·((t − t0) mod T)/T. Frames with the
     same angle in different revolutions are "the same image".
  5. A per-frame pattern-quality score (streak contrast, see quality_score) is
     averaged per angle bin; its peaks are the azimuths kept. For each kept
     azimuth the best single frame and the frames at that angle in every
     revolution (for an averaged, lower-noise image) are returned.

Angles are relative to the first frame of the clip — there is no absolute
azimuth reference without a rotation encoder.

Lessons from the first real clip (MVI_2527.MP4, 2026-10-06 — Canon video, 130 s,
~0.62 rpm, dim pattern, camera mounted so streaks run HORIZONTALLY):
  * Slow drift (beam current / background glow rising and falling over a minute)
    dominated the raw self-similarity and produced a "period" by coincidence. The
    thumbnails are therefore TEMPORALLY high-passed (moving mean over detrend_s
    subtracted) before correlating — only the pattern's azimuthal changes remain.
  * Streak orientation depends on how the camera is mounted, so quality_score takes
    an orientation and analyze_rotation picks it automatically (streak_orientation).
"""

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import numpy as np

try:
    import cv2
except ImportError:
    cv2 = None

THUMB_W      = 128      # thumbnail width (px); height keeps the aspect ratio
MIN_CONF     = 0.25     # peak self-similarity below this → "no rotation found"
N_ANGLE_BINS = 72       # 5° bins for the quality-vs-azimuth curve
FOLDS        = (6, 4, 3, 2)   # surface-symmetry candidates, checked largest first
FOLD_RATIO   = 0.75     # sub-period peak must reach this fraction of the main peak
HARMONIC_TOL = 0.90     # earliest peak ≥ this × best peak is taken as the period
ORIENTATIONS = ("vertical", "horizontal")   # streak direction in the image
MIN_ORIENT_CV = 0.03    # below this the score barely varies either way → keep "vertical"


# ── Per-frame reductions ──────────────────────────────────────────────

def make_thumb(img: np.ndarray, roi: Optional[Tuple[int, int, int, int]] = None,
               width: int = THUMB_W) -> np.ndarray:
    """BGR/grey frame → float32 grey thumbnail. roi = (x, y, w, h) in frame pixels."""
    if roi is not None:
        x, y, w, h = (int(v) for v in roi)
        img = img[max(y, 0):y + h, max(x, 0):x + w]
    if img.ndim == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if cv2 is not None else img.mean(axis=2)
    h, w = img.shape[:2]
    th = max(8, int(round(h * width / max(w, 1))))
    if cv2 is not None:
        return cv2.resize(img, (width, th), interpolation=cv2.INTER_AREA).astype(np.float32)
    ys = (np.arange(th) * h // th)
    xs = (np.arange(width) * w // width)
    return img[ys][:, xs].astype(np.float32)


def quality_score(thumb: np.ndarray, orientation: str = "vertical") -> float:
    """
    Streak contrast. Vertical streaks show up as sharp peaks in the horizontal
    profile (column means); horizontal streaks (camera rotated 90°) in the vertical
    profile (row means). The profile is high-pass filtered (minus a running mean
    ~1/8 of its length) to remove the broad glow, and its standard deviation is the
    score — sharp, bright, well-separated streaks score high; diffuse or off-azimuth
    patterns score low. Units: grey levels.
    """
    prof, smooth = streak_profile(thumb, orientation)
    return float((prof - smooth).std())


def streak_profile(thumb: np.ndarray, orientation: str = "vertical"):
    """(profile across the streaks, its running mean) — quality_score's input; also
    drawn by the video panel's CV view so it shows exactly what is scored."""
    prof = thumb.mean(axis=0 if orientation == "vertical" else 1)
    k = max(3, len(prof) // 8) | 1
    pad = np.pad(prof, k // 2, mode="edge")
    smooth = np.convolve(pad, np.ones(k) / k, mode="valid")
    return prof, smooth


def correlation_input(times: Sequence[float], thumbs: Sequence[np.ndarray],
                      detrend_s: float = 0.0) -> np.ndarray:
    """
    What the self-similarity correlates (before the per-frame L2 normalisation):
    thumbnails as rows, minus the clip's mean thumbnail, then temporally high-passed
    over detrend_s (0 = skip). Returns float32 (n_frames, n_pixels).
    """
    X = np.stack([th.ravel() for th in thumbs]).astype(np.float32)
    X -= X.mean(axis=0, keepdims=True)                 # remove the static background
    t = np.asarray(times, dtype=np.float64)
    if detrend_s > 0 and len(t) > 1:                   # remove slow drift (beam, glow)
        bin_s = float(np.median(np.diff(t)))
        _temporal_highpass(X, int(round(detrend_s / max(bin_s, 1e-6))) | 1)
    return X


def streak_orientation(thumbs: Sequence[np.ndarray]) -> Tuple[str, dict]:
    """
    Which way do the streaks run? On a rotating substrate the right score swings
    strongly as principal azimuths pass the beam, while the wrong one is dominated by
    static edges (screen rim, shadow edge) and barely moves. So: the orientation whose
    per-frame score has the larger coefficient of variation wins. Returns
    (orientation, {orientation: per-frame scores}).
    """
    scores = {o: np.array([quality_score(th, o) for th in thumbs]) for o in ORIENTATIONS}
    cv = {o: float(q.std() / max(q.mean(), 1e-9)) for o, q in scores.items()}
    best = max(cv, key=cv.get)
    if cv[best] < MIN_ORIENT_CV:          # static substrate: no evidence either way
        best = "vertical"
    return best, scores


def _temporal_highpass(X: np.ndarray, win: int) -> np.ndarray:
    """X (n_frames × n_px) minus its centred moving mean over `win` frames (in place).
    The window shrinks at the clip ends instead of padding."""
    n = len(X)
    if win < 3 or n < 3:
        return X
    h = win // 2
    cs = np.zeros((n + 1, X.shape[1]), dtype=np.float64)
    np.cumsum(X, axis=0, out=cs[1:])
    i = np.arange(n)
    lo, hi = np.maximum(i - h, 0), np.minimum(i + h + 1, n)
    X -= ((cs[hi] - cs[lo]) / (hi - lo)[:, None]).astype(X.dtype)
    return X


# ── Result containers ─────────────────────────────────────────────────

@dataclass
class AzimuthPick:
    angle_deg: float          # relative to the clip's first frame
    best_index: int           # clip frame index with the highest quality at this angle
    members: List[int]        # one frame per revolution at this angle (for averaging)
    score: float              # binned mean quality at this angle


@dataclass
class RotationResult:
    n_frames: int
    duration_s: float
    fps: float
    period_s: Optional[float]           # None → no rotation detected
    confidence: float                   # peak mean correlation (0–1)
    fold: int = 1                       # apparent pattern symmetry (1 = none detected)
    orientation: str = "vertical"       # streak direction used for the quality score
    detrend_s: float = 0.0              # temporal high-pass window used (0 = none)
    times: np.ndarray = field(default_factory=lambda: np.zeros(0))     # per frame, s from start
    lags: np.ndarray = field(default_factory=lambda: np.zeros(0))
    lag_corr: np.ndarray = field(default_factory=lambda: np.zeros(0))
    angles: np.ndarray = field(default_factory=lambda: np.zeros(0))     # per frame, deg
    quality: np.ndarray = field(default_factory=lambda: np.zeros(0))    # per frame
    bin_centers: np.ndarray = field(default_factory=lambda: np.zeros(0))
    bin_quality: np.ndarray = field(default_factory=lambda: np.zeros(0))
    picks: List[AzimuthPick] = field(default_factory=list)
    message: str = ""
    warning: str = ""

    @property
    def rpm(self) -> Optional[float]:
        return 60.0 / self.period_s if self.period_s else None


# ── Analysis ──────────────────────────────────────────────────────────

def _lag_curve(times: np.ndarray, X: np.ndarray, bin_s: float, max_lag: float):
    """Mean pairwise correlation binned by time separation."""
    C = X @ X.T
    iu = np.triu_indices(len(times), k=1)
    dt = (times[None, :] - times[:, None])[iu]
    c = C[iu]
    keep = dt <= max_lag
    b = np.floor(dt[keep] / bin_s + 0.5).astype(int)
    nb = int(max_lag / bin_s) + 2
    s = np.bincount(b, weights=c[keep], minlength=nb)
    n = np.bincount(b, minlength=nb)
    with np.errstate(invalid="ignore", divide="ignore"):
        curve = np.where(n > 0, s / np.maximum(n, 1), np.nan)
    return np.arange(nb) * bin_s, curve, n


def _interp_at(lags, curve, t):
    ok = ~np.isnan(curve)
    return float(np.interp(t, lags[ok], curve[ok])) if ok.any() else float("nan")


def analyze_rotation(times: Sequence[float], thumbs: Sequence[np.ndarray],
                     expected_period: Optional[float] = None,
                     min_period: float = 1.0, n_picks: int = 4,
                     min_sep_deg: float = 20.0, orientation: str = "auto",
                     detrend_s: Optional[float] = None) -> RotationResult:
    """
    times:  frame timestamps (s, any origin), same length as thumbs.
    thumbs: make_thumb() outputs (all the same shape).
    expected_period: if given (s), the peak search is limited to ±25 % around it —
        use when you know the rpm; otherwise the clip should hold ≥ 2 revolutions.
    orientation: "auto" | "vertical" | "horizontal" — streak direction for the
        quality score (auto: streak_orientation()).
    detrend_s: temporal high-pass window (s) applied before correlating; None = auto
        (1.5 × expected_period, else half the clip), 0 = off.
    """
    t = np.asarray(times, dtype=np.float64)
    n = len(t)
    res = RotationResult(n_frames=n, duration_s=float(t[-1] - t[0]) if n > 1 else 0.0,
                         fps=0.0, period_s=None, confidence=0.0)
    if n < 10:
        res.message = f"only {n} frames — clip too short"
        return res
    t = t - t[0]
    res.times = t
    res.fps = (n - 1) / max(res.duration_s, 1e-9)
    if orientation in ORIENTATIONS:
        Q = np.array([quality_score(th, orientation) for th in thumbs], dtype=np.float64)
    else:
        orientation, scores = streak_orientation(thumbs)
        Q = scores[orientation].astype(np.float64)
    res.quality, res.orientation = Q, orientation

    bin_s = float(np.median(np.diff(t)))
    if detrend_s is None:
        # The window must stay LONGER than the period: a short one also strips the
        # once-per-revolution content (holder wobble, per-azimuth differences) that
        # tells T apart from its symmetry sub-period T/n — tested: 1 s on the 4-fold
        # sim locked T/4; 20–30 s on MVI_2527 locked T/5. clip/2 or 1.5·T is safe.
        detrend_s = 1.5 * expected_period if expected_period else res.duration_s / 2
        detrend_s = max(1.0, min(detrend_s, res.duration_s))
    X = correlation_input(t, thumbs, detrend_s)
    res.detrend_s = float(max(detrend_s, 0.0))
    norms = np.linalg.norm(X, axis=1, keepdims=True)
    X /= np.maximum(norms, 1e-6)
    max_lag = res.duration_s * 0.85                    # need overlap to average over
    lags, curve, counts = _lag_curve(t, X, bin_s, max_lag)
    res.lags, res.lag_corr = lags, curve

    lo, hi = min_period, max_lag
    if expected_period:
        lo, hi = max(lo, expected_period * 0.75), min(hi, expected_period * 1.25)
    if hi <= lo:
        res.message = (f"clip {res.duration_s:.1f} s is too short to see a "
                       f"{expected_period or min_period:.1f} s rotation — record ≥ 2 revolutions")
        return res
    # Skip the trivial short-lag peak: start after the curve first decorrelates
    # (first local minimum past min_period) unless a period was supplied.
    win = (lags >= lo) & (lags <= hi) & (counts >= 3) & ~np.isnan(curve)
    if not expected_period:
        idx = np.where(win)[0]
        for i in idx[1:-1]:
            if curve[i] <= curve[i - 1] and curve[i] <= curve[i + 1]:
                win &= lags >= lags[i]
                break
    if not win.any():
        res.message = "no usable lag range"
        return res
    cand = np.where(win)[0]
    gmax = float(np.nanmax(curve[cand]))
    # Fundamental, not a harmonic: 2T, 3T… repeat the image just as well (and can
    # edge out T on noise), so take the EARLIEST local peak within HARMONIC_TOL of
    # the best one rather than the global maximum.
    i = cand[np.nanargmax(curve[cand])]
    for j in cand:
        lo_ok = j == 0 or np.isnan(curve[j - 1]) or curve[j] >= curve[j - 1]
        hi_ok = j + 1 >= len(curve) or np.isnan(curve[j + 1]) or curve[j] >= curve[j + 1]
        if lo_ok and hi_ok and curve[j] >= HARMONIC_TOL * gmax:
            i = j
            break
    peak = float(curve[i])
    res.confidence = peak
    if peak < MIN_CONF:
        res.message = (f"no rotation found (best self-similarity {peak:.2f} < {MIN_CONF}) — "
                       "substrate static, clip shorter than a revolution, or pattern too weak")
        _static_picks(res, n_picks)
        return res
    # parabolic sub-bin refinement
    T = lags[i]
    if 0 < i < len(curve) - 1 and not np.isnan(curve[i - 1]) and not np.isnan(curve[i + 1]):
        a, b, c = curve[i - 1], curve[i], curve[i + 1]
        den = a - 2 * b + c
        if den < 0:
            T += 0.5 * (a - c) / den * bin_s
    res.period_s = float(T)

    # Apparent symmetry: equal-height peaks at every k·T/n (informational).
    for f in FOLDS:
        if T / f < min_period * 0.5:
            continue
        vals = [_interp_at(lags, curve, k * T / f) for k in range(1, f)]
        if all(v >= FOLD_RATIO * peak for v in vals):
            res.fold = f
            break

    res.angles = (t % T) / T * 360.0
    _pick_azimuths(res, t, n_picks, min_sep_deg)
    revs = res.duration_s / T
    res.message = (f"period {T:.3f} s ({60 / T:.2f} rpm), {revs:.1f} revolutions, "
                   f"self-similarity {peak:.2f}"
                   + (f", {res.fold}-fold pattern symmetry" if res.fold > 1 else ""))
    if expected_period and T >= hi - 1.5 * bin_s:
        # The peak sits on the upper edge of the search window: the clip cannot
        # show a full repeat at this rpm (lags only reach 0.85 × clip length).
        res.warning = (f"period found at the edge of the search range ({T:.1f} s) — the clip "
                       f"is probably too short for {60 / expected_period:.2f} rpm; use ≥ "
                       f"{2 * expected_period:.0f} s (2 revolutions)")
    elif not expected_period:
        # A clip shorter than one true revolution can only reveal the pattern's
        # symmetry sub-period (e.g. 90° on a 4-fold surface) → rpm reads n× high.
        res.warning = ("no expected rpm given — if this reads n× the real rpm, the clip is "
                       "shorter than one revolution: lengthen it or enter the rpm")
    return res


def _pick_azimuths(res: RotationResult, t: np.ndarray, n_picks: int, min_sep_deg: float):
    T, A, Q = res.period_s, res.angles, res.quality
    nb = N_ANGLE_BINS
    edges = np.linspace(0, 360, nb + 1)
    centers = (edges[:-1] + edges[1:]) / 2
    b = np.minimum((A / 360 * nb).astype(int), nb - 1)
    s = np.bincount(b, weights=Q, minlength=nb)
    c = np.bincount(b, minlength=nb)
    binq = np.where(c > 0, s / np.maximum(c, 1), np.nan)
    # fill empty bins circularly, then light circular smoothing (1-2-1)
    if np.isnan(binq).any():
        ok = ~np.isnan(binq)
        binq = np.interp(np.arange(nb), np.arange(nb)[ok], binq[ok], period=nb)
    sm = (np.roll(binq, 1) + 2 * binq + np.roll(binq, -1)) / 4
    res.bin_centers, res.bin_quality = centers, sm

    order = np.argsort(sm)[::-1]
    peaks = [k for k in order
             if sm[k] >= sm[(k - 1) % nb] and sm[k] >= sm[(k + 1) % nb]]
    chosen: List[int] = []
    for k in peaks:
        if len(chosen) >= n_picks:
            break
        if all(_circ_dist(centers[k], centers[j]) >= min_sep_deg for j in chosen):
            chosen.append(k)

    # one frame per revolution, the closest in angle, within ±1 frame of rotation
    tol = max(360.0 / (res.fps * T), 360.0 / nb / 2)
    rev = np.floor(t / T).astype(int)
    for k in chosen:
        # anchor the azimuth on the single best frame within ±1 bin of the peak
        near = np.where(_circ_dist(A, centers[k]) <= 360.0 / nb)[0]
        ang = centers[k]
        if len(near):
            best = near[np.argmax(Q[near])]
            ang = float(A[best])
        else:
            best = int(np.argmin(_circ_dist(A, ang)))
        members = []
        for r in np.unique(rev):
            idx = np.where(rev == r)[0]
            dd = _circ_dist(A[idx], ang)
            j = int(np.argmin(dd))
            if dd[j] <= tol:
                members.append(int(idx[j]))
        res.picks.append(AzimuthPick(angle_deg=ang, best_index=int(best),
                                     members=members or [int(best)], score=float(sm[k])))
    res.picks.sort(key=lambda p: p.angle_deg)


def _static_picks(res: RotationResult, n_picks: int):
    """No rotation: keep the highest-quality frames (they are all one azimuth)."""
    order = np.argsort(res.quality)[::-1][:max(1, n_picks)]
    res.picks = [AzimuthPick(angle_deg=float("nan"), best_index=int(i), members=[int(i)],
                             score=float(res.quality[i])) for i in order]


def _circ_dist(a, b):
    d = np.abs(np.asarray(a) - b) % 360.0
    return np.minimum(d, 360.0 - d)
