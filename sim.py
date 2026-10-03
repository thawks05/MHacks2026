"""Synthetic motor/conveyor signal so you can build + test BEFORE the hardware rig is ready.
Swap for real data later: np.loadtxt('recording.csv') and use the same fs as the sensor."""
import numpy as np

def synth(fs, seconds, f_rot=12.0, fault=0.0, seed=0):
    rng = np.random.default_rng(seed)
    t = np.arange(int(fs * seconds)) / fs
    x = np.sin(2*np.pi*f_rot*t) + 0.4*np.sin(2*np.pi*2*f_rot*t) + 0.1*rng.standard_normal(len(t))
    if fault > 0:   # a sticking gear tooth = one high-frequency 'knock' per rotation
        imp = np.zeros_like(x); imp[::int(fs / f_rot)] = 1
        k = np.arange(20)
        x += fault * 3 * np.convolve(imp, np.exp(-k/4) * np.sin(2*np.pi*0.3*k), mode="same")
    return x
