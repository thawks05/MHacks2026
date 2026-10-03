"""Signal -> health score. Sensor-agnostic: feed it a 1-D array of samples (accelerometer axis or mic) + sample rate.
Flow: record ~60s healthy -> Baseline.fit -> score() every few seconds -> health 0-100 + which band drifted."""
import numpy as np

def _frames(x, fs, frame_s, hop_s):
    n, hop = int(frame_s * fs), int(hop_s * fs)
    win = np.hanning(n)
    spec = [np.abs(np.fft.rfft((x[s:s+n] - x[s:s+n].mean()) * win)) ** 2
            for s in range(0, len(x) - n + 1, hop)]
    return np.fft.rfftfreq(n, 1 / fs), np.array(spec)

def make_bands(fs, n_bands=8, f_lo=5.0):
    e = np.geomspace(f_lo, fs / 2, n_bands + 1)
    return [(float(e[i]), float(e[i + 1])) for i in range(n_bands)]

def _log_band_energy(freqs, spec, bands):
    cols = [spec[:, (freqs >= lo) & (freqs < hi)].sum(axis=1) for lo, hi in bands]
    return np.log10(np.array(cols).T + 1e-12)

class Baseline:
    def __init__(self, fs, bands=None, frame_s=1.0, hop_s=0.5):
        self.fs, self.frame_s, self.hop_s = fs, frame_s, hop_s
        self.bands = bands or make_bands(fs)

    def fit(self, healthy):
        f, s = _frames(healthy, self.fs, self.frame_s, self.hop_s)
        e = _log_band_energy(f, s, self.bands)
        self.mu, self.sd = e.mean(0), np.maximum(e.std(0), 0.05)   # floor stops tiny noise looking huge
        return self

    def score(self, chunk):
        """chunk: >= frame_s seconds of new samples. Returns dict with health (0-100), worst band, per-band z."""
        f, s = _frames(chunk, self.fs, self.frame_s, self.hop_s)
        z = (_log_band_energy(f, s, self.bands).mean(0) - self.mu) / self.sd
        drift = float(np.abs(z).max())
        health = float(np.clip(100 - 8 * max(0.0, drift - 2.0), 0, 100))   # |z|<=2 is normal noise
        w = int(np.abs(z).argmax())
        return {"health": health, "worst_band": self.bands[w], "z": z.round(1).tolist()}

class Trend:
    """Linear extrapolation of recent health -> rough time-to-threshold. Label it an estimate in the pitch."""
    def __init__(self, replace_below=40.0, window=6):
        self.t, self.h, self.thr, self.w = [], [], replace_below, window
    def add(self, t, health):
        self.t.append(t); self.h.append(health)
    def eta_s(self):
        if len(self.t) < 3: return None
        slope = np.polyfit(self.t[-self.w:], self.h[-self.w:], 1)[0]
        if slope > -0.05: return None
        return max(0.0, (self.h[-1] - self.thr) / -slope)
