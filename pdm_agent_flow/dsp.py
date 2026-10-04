"""
dsp.py — signal processing for the vibration pods.

Pure math only: no serial, no database, no alerts, no state machine.
That keeps it testable on recorded data and easy to explain to judges.

  extract_features(window_g, fs) -> dict      one 1 s window of accel (N x 3, in g)
  Baseline.fit(list_of_feature_dicts, fs)     mean + std of every feature (healthy run)
  score(features, baseline) -> Score          z-scores, 0-100 health, top deviations
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict

import numpy as np
from scipy.signal import welch
from scipy.stats import kurtosis as _kurtosis

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
# Frequency bands (Hz) whose vibration level is tracked.
# PLACEHOLDERS: set these from the healthy vs obstructed recordings
# (shaft rate = RPM / 60, its harmonics, gear mesh = teeth x shaft rate).
BANDS: dict[str, tuple[float, float]] = {
    "band_low_g":  (2.0, 60.0),
    "band_mid_g":  (60.0, 200.0),
    "band_high_g": (200.0, 450.0),
}

MIN_FREQ_HZ = 2.0      # ignore DC / slow drift when looking for the dominant peak
NPERSEG = 512          # Welch segment: ~2 Hz bins at 1 kHz, 3 averaged segments per 1 s window

CORE_FEATURES = ["rms_g", "peak_g", "crest", "kurtosis", "dom_freq_hz"]

# Std floors so a feature that barely moves in the baseline (e.g. dominant
# frequency) doesn't produce huge z-scores from tiny, meaningless changes.
REL_STD_FLOOR = 0.05   # at least 5 % of the baseline mean
ABS_STD_FLOOR = {
    "rms_g": 0.002, "peak_g": 0.005, "crest": 0.1, "kurtosis": 0.2,
    "dom_freq_hz": 0.5,
}
DEFAULT_ABS_FLOOR = 0.001

Z_DEVIATION = 4.0      # a window counts as "deviating" when its worst z-score reaches this


def feature_names() -> list[str]:
    return CORE_FEATURES + list(BANDS)


# ----------------------------------------------------------------------------
# Features
# ----------------------------------------------------------------------------
def _dominant_freq(f: np.ndarray, psd: np.ndarray) -> float:
    """Frequency of the biggest spectral peak, refined between bins (parabolic fit)."""
    valid = np.where(f >= MIN_FREQ_HZ)[0]
    if valid.size == 0:
        return 0.0
    i = int(valid[np.argmax(psd[valid])])
    if 0 < i < len(psd) - 1:
        a, b, c = psd[i - 1], psd[i], psd[i + 1]
        denom = a - 2 * b + c
        offset = 0.5 * (a - c) / denom if denom != 0 else 0.0
        return float(f[i] + offset * (f[1] - f[0]))
    return float(f[i])


def extract_features(window_g, fs: float) -> dict:
    """
    window_g: array-like, shape (N, 3), accel X/Y/Z in g. N is normally fs (1 s).
    Returns a flat dict of floats.
    """
    x = np.asarray(window_g, dtype=float)
    x = x - x.mean(axis=0)                         # remove gravity / DC per axis

    rms_axes = np.sqrt(np.mean(x ** 2, axis=0))
    rms = float(np.sqrt(np.sum(rms_axes ** 2)))    # total vibration level, all axes
    abs_max_axes = np.max(np.abs(x), axis=0)
    peak = float(np.max(abs_max_axes))
    crest = float(np.max(abs_max_axes / np.maximum(rms_axes, 1e-12)))

    k = _kurtosis(x, axis=0, fisher=False, bias=True)   # Gaussian noise ~= 3
    k = k[np.isfinite(k)]
    kurt = float(np.max(k)) if k.size else 3.0

    nperseg = min(NPERSEG, len(x))
    f, pxx = welch(x, fs=fs, window="hann", nperseg=nperseg,
                   noverlap=nperseg // 2, detrend="constant", axis=0)
    psd = pxx.sum(axis=1)                          # total spectrum across axes
    df = f[1] - f[0]

    feats = {
        "rms_g": rms,
        "peak_g": peak,
        "crest": crest,
        "kurtosis": kurt,
        "dom_freq_hz": _dominant_freq(f, psd),
    }
    for name, (lo, hi) in BANDS.items():
        power = float(np.sum(psd[(f >= lo) & (f < hi)]) * df)
        feats[name] = math.sqrt(max(power, 0.0))   # RMS (g) inside the band
    return feats


# ----------------------------------------------------------------------------
# Baseline + scoring
# ----------------------------------------------------------------------------
@dataclass
class Baseline:
    mean: dict
    std: dict          # already floored, ready for z-scores
    n_windows: int
    fs: float
    created_ts: float

    @classmethod
    def fit(cls, rows: list[dict], fs: float, created_ts: float) -> "Baseline":
        if not rows:
            raise ValueError("no windows to fit a baseline on")
        mean, std = {}, {}
        for name in feature_names():
            vals = np.array([r[name] for r in rows], dtype=float)
            m = float(vals.mean())
            s = float(vals.std(ddof=1)) if len(vals) > 1 else 0.0
            floor = max(REL_STD_FLOOR * abs(m), ABS_STD_FLOOR.get(name, DEFAULT_ABS_FLOOR))
            mean[name], std[name] = m, max(s, floor)
        return cls(mean=mean, std=std, n_windows=len(rows), fs=fs, created_ts=created_ts)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Baseline":
        return cls(**d)


@dataclass
class Score:
    z: dict                 # signed z-score per feature
    z_max: float            # worst |z|
    health: float           # 0-100 (rough mapping, not a calibrated probability)
    deviating: bool         # z_max >= Z_DEVIATION
    top: list               # [(feature, z), ...] largest |z| first


Z_ZERO_HEALTH = 50.0   # |z| at which health reaches 0


def health_from_z(z_max: float) -> float:
    """Log scale, because real faults push z into the tens.
    |z| <= 2 -> 100, z=4 -> ~78, z=10 -> 50, z=20 -> ~28, z>=50 -> 0. Rough, not calibrated."""
    if z_max <= 2.0:
        return 100.0
    h = 100.0 * (1.0 - math.log(z_max / 2.0) / math.log(Z_ZERO_HEALTH / 2.0))
    return float(min(100.0, max(0.0, h)))


def score(features: dict, baseline: Baseline, top_n: int = 3) -> Score:
    z = {n: (features[n] - baseline.mean[n]) / baseline.std[n] for n in feature_names()}
    ranked = sorted(z.items(), key=lambda kv: abs(kv[1]), reverse=True)
    z_max = abs(ranked[0][1]) if ranked else 0.0
    return Score(
        z=z,
        z_max=z_max,
        health=health_from_z(z_max),
        deviating=z_max >= Z_DEVIATION,
        top=ranked[:top_n],
    )
