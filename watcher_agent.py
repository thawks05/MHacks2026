"""Watcher agent (Layer 1): senses. Runs the health-scoring pipeline (health.py/ingest.py) and
writes report_health + spectrum_reading to Spacetime about once every 2 seconds per part. On a
state transition into (or within) warning/critical, it opens/refreshes an observation_request -
Photon picks that up to start the operator interview. The PartAlert push to the Analyst still
only fires once per incident (on first entry into bad territory), so the chat conversation only
starts a single time per failure; the observation_request itself can refresh more often (e.g.
warning -> critical) since the reducer is idempotent about not duplicating it.

Simulator mode only by default (SENSOR_SOURCE=sim) - ingest.py is the seam for real USB serial
later; nothing downstream needs to change. FS and PART_MAP must match the real rig once recorded."""
import json
import time
from uagents import Agent, Context

from health import Baseline, Trend, derive_state
import ingest
from store import Store
from contracts import PartHealth
from messages import PartAlert, WATCHER_SEED, WATCHER_PORT, ANALYST_ADDRESS

FS = 400  # samples/sec - set to your sensor's real rate
PART_MAP = [(0, 20, "motor_shaft"), (20, 80, "belt"), (80, 1e9, "drive_gear")]  # EDIT after recording the real rig
FAULT_RAMP = [0.0, 0.0, 0.15, 0.3, 0.5, 0.8, 1.0]  # holds at full fault once the ramp ends


def part_for(hz):
    return next(p for lo, hi, p in PART_MAP if lo <= hz < hi)


store = Store()
base = Baseline(FS).fit(ingest.read_baseline(FS, 60))
trend = Trend(replace_below=40)
last_state = {}   # part -> last known state (healthy/warning/critical)
tick_count = 0

agent = Agent(name="watcher", seed=WATCHER_SEED, port=WATCHER_PORT, endpoint=[f"http://127.0.0.1:{WATCHER_PORT}/submit"])
print("WATCHER_ADDRESS", agent.address)


@agent.on_interval(period=10.0)
async def heartbeat(ctx: Context):
    store.heartbeat("watcher")


@agent.on_interval(period=2.0)
async def tick(ctx: Context):
    global tick_count
    sev = FAULT_RAMP[min(tick_count, len(FAULT_RAMP) - 1)]
    r = base.score(ingest.read_chunk(FS, 5, tick_count, fault=sev))
    trend.add(tick_count * 5.0, r["health"])
    lo, hi = r["worst_band"]
    drift_z = max(r["z"], key=abs)  # signed z-score of the worst-deviating band - real number, cited honestly downstream
    ph = PartHealth(part_for((lo + hi) / 2), r["health"], round(lo), round(hi), trend.eta_s(), time.time())
    tick_count += 1

    state = derive_state(ph.health)
    ctx.logger.info(f"health={ph.health:5.1f} part={ph.part:<11} state={state} band=({ph.drift_lo_hz}-{ph.drift_hi_hz})Hz z={drift_z:+.1f} eta_s={ph.eta_s}")
    store.report_health(ph)
    store.write_spectrum_reading(ph.part, json.dumps(base.bands), json.dumps(r["z"]), ph.updated_at)

    if state in ("warning", "critical") and state != last_state.get(ph.part):
        is_new_incident = last_state.get(ph.part) in (None, "healthy")
        store.open_observation_request(ph.part, state, ph.health, -1.0 if ph.eta_s is None else ph.eta_s)
        if is_new_incident:
            ctx.logger.info(f"INCIDENT: {ph.part} -> {state} - telling Analyst")
            store.log_event("watcher", "incident_opened", f"{ph.part} -> {state}", part=ph.part)
            await ctx.send(ANALYST_ADDRESS, PartAlert(
                part=ph.part, health=ph.health, drift_lo_hz=ph.drift_lo_hz,
                drift_hi_hz=ph.drift_hi_hz, drift_z=drift_z, eta_s=ph.eta_s, updated_at=ph.updated_at,
            ))
    last_state[ph.part] = state


if __name__ == "__main__":
    agent.run()
