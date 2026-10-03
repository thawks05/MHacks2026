"""Run:  pip install numpy && python demo_pipeline.py
Simulates: 60s healthy baseline -> fault ramps up -> health drops -> procurement proposal.
Map frequency bands to parts AFTER you record the real rig (see PART_MAP)."""
import time
from health import Baseline, Trend
from sim import synth
from suppliers import propose
from contracts import PartHealth, OrderProposal

FS = 400  # samples/sec - set to your sensor's real rate
PART_MAP = [(0, 20, "motor_shaft"), (20, 80, "belt"), (80, 1e9, "drive_gear")]  # (lo Hz, hi Hz, part) - EDIT after recording

def part_for(hz):
    return next(p for lo, hi, p in PART_MAP if lo <= hz < hi)

base = Baseline(FS).fit(synth(FS, 60, fault=0.0, seed=1))
trend = Trend(replace_below=40)
for i, sev in enumerate([0.0, 0.0, 0.15, 0.3, 0.5, 0.8, 1.0]):
    r = base.score(synth(FS, 5, fault=sev, seed=10 + i))
    trend.add(i * 5.0, r["health"])
    lo, hi = r["worst_band"]
    ph = PartHealth(part_for((lo + hi) / 2), r["health"], round(lo), round(hi), trend.eta_s(), time.time())
    print(f"t={i*5:>3}s  health={ph.health:5.1f}  band=({ph.drift_lo_hz}-{ph.drift_hi_hz})Hz  part={ph.part:<11} eta_s={ph.eta_s}")
    if ph.health < 60:
        o = OrderProposal(**propose(ph.part, ph.eta_s), updated_at=time.time())
        print("   ->", o.to_json())
        break
