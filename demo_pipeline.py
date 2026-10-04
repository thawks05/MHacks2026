"""Run:  pip install numpy && python demo_pipeline.py
Simulates: 60s healthy baseline -> fault ramps up -> health drops -> the full buyer conversation
(average price/lead time quoted, simulated replies for max price / max wait / priority, research,
then a final yes/no prompt with a mock product link before anything is marked "approved").
SIMULATED_REPLY stands in for the real iMessage/Photon conversation - swap it for the live chat later.
Map frequency bands to parts AFTER you record the real rig (see PART_MAP)."""
import time
from health import Baseline, Trend
from sim import synth
from suppliers import average, choose
from contracts import PartHealth, OrderProposal

FS = 400  # samples/sec - set to your sensor's real rate
PART_MAP = [(0, 20, "motor_shaft"), (20, 80, "belt"), (80, 1e9, "drive_gear")]  # (lo Hz, hi Hz, part) - EDIT after recording

SIMULATED_REPLY = {"max_price": 60.0, "max_wait_days": 2, "priority": "speed"}

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
        avg = average(ph.part)
        print(f"\n[text] {ph.part} is failing (health {ph.health:.0f}). Found {avg['count']} suppliers: "
              f"${avg['min_price']:.0f}-${avg['max_price']:.0f}/unit (avg ${avg['avg_price']:.2f}), "
              f"{avg['min_lead_days']:.0f}-{avg['max_lead_days']:.0f}d lead (avg {avg['avg_lead_days']:.1f}d).")
        print(f"[text] What's your max price? -> you: ${SIMULATED_REPLY['max_price']:.0f}")
        print(f"[text] What's your max wait, in days? -> you: {SIMULATED_REPLY['max_wait_days']:g}")
        print(f"[text] Price or speed matters more? -> you: {SIMULATED_REPLY['priority']}")

        o = OrderProposal(**choose(ph.part, **SIMULATED_REPLY), updated_at=time.time())
        print(f"\n[text] Found it: {o.supplier} - {o.reason}")
        print(f"[text] View before buying: {o.product_url}")
        print(f"[text] Buy it? (yes/no) -> you: yes")
        o.status = "approved"
        print("\n   ->", o.to_json())
        break
