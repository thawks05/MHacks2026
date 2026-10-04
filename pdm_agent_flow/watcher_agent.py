"""Watcher agent (Layer 1): senses, using the hardware pipeline (pipeline.py + dsp.py).

  host ESP32 serial (or a recording, or the sim)
    -> pipeline.py: 1 s windows -> dsp features -> baseline / monitoring -> status hold rule
       (healthy -> watch -> degraded -> critical after 10/20/30 s of sustained deviation)
    -> this agent: writes every scored window to Spacetime, and the moment a pod's status is
       anything other than healthy, opens an incident and sends a PartAlert to the Analyst.

pipeline.py runs unchanged in a background thread; its sink rows land in a queue that this agent
drains on its own loop (Spacetime writes + uAgent messages happen here, never in that thread).

Training: on startup, and after every incident the operator resolves by text, the pipeline is
told "replace" -> it closes its own alert and records a fresh 60-window baseline. The Watcher
never closes an incident itself, even if the status drops back to healthy.

Config (env vars):
  SENSOR_SOURCE  sim (default) | serial | file
  SERIAL_PORT    e.g. /dev/cu.usbserial-0001      BAUD (921600)   COUNTS_PER_G (8192 = +-4 g)
  REPLAY_FILE    a CSV recorded with  python3 pipeline.py --port ... --record rec.csv
  FS             measured sample rate from the host's "rate=" line (1000)
  SIM_FAULT_AT   sim: seconds after start before the obstruction ramps in (90)
  SIM_SPEED      sim/replay speed, 1 = real time (1)"""
import json
import os
import queue
import threading
import time

from uagents import Agent, Context

import dsp
import pipeline
from store import Store
from contracts import PartHealth
from messages import PartAlert, WATCHER_SEED, WATCHER_PORT, ANALYST_ADDRESS

# pipeline.py names physical slots; the Analyst/Buyer/suppliers name replaceable parts.
# EDIT if the pod moves or you demo a different part.
SLOT_PARTS = {"conveyor_17_phx_az/drive_motor": "drive_gear"}

SOURCE = os.environ.get("SENSOR_SOURCE", "sim")
FS = float(os.environ.get("FS", "1000"))
SIM_FAULT_AT = float(os.environ.get("SIM_FAULT_AT", "90"))
SIM_SPEED = float(os.environ.get("SIM_SPEED", "1"))

RANK = {s: i for i, s in enumerate(pipeline.STATUS_ORDER)}   # healthy < watch < degraded < critical

store = Store()
agent = Agent(name="watcher", seed=WATCHER_SEED, port=WATCHER_PORT,
              endpoint=[f"http://127.0.0.1:{WATCHER_PORT}/submit"])
print("WATCHER_ADDRESS", agent.address)


class QueueSink:
    """pipeline.py's sink interface: emit(table, row). Rows are handled on the agent's loop."""
    def __init__(self):
        self.q: queue.Queue = queue.Queue()

    def emit(self, table: str, row: dict) -> None:
        self.q.put((table, row))


sink = QueueSink()
cmds: queue.Queue = queue.Queue()          # "replace <pod>" etc., applied by pipeline.run between windows
sim_fixed = threading.Event()              # sim only: set once the operator resolves, so the fault goes away
pipe = pipeline.Pipeline(sink, pipeline.StateStore(None), FS, every=1)   # Spacetime is the source of truth

S = {
    "mode": None,     # last pod mode written to Spacetime
    "open": {},       # part -> {"incident_id", "state", "pod"} for incidents still open
    "retraining": set(),  # slot_ids resolved by the operator whose new baseline hasn't landed yet
}


def part_of(slot_id: str) -> str:
    return SLOT_PARTS.get(slot_id, slot_id.split("/")[-1])


def _make_source():
    if SOURCE == "serial":
        port = os.environ.get("SERIAL_PORT")
        if not port:
            raise SystemExit("SENSOR_SOURCE=serial needs SERIAL_PORT, e.g. /dev/cu.usbserial-0001")
        return pipeline.serial_source(port, int(os.environ.get("BAUD", "921600")),
                                      float(os.environ.get("COUNTS_PER_G", "8192")))
    if SOURCE == "file":
        return pipeline.file_source(os.environ["REPLAY_FILE"], float(os.environ.get("COUNTS_PER_G", "8192")),
                                    FS, SIM_SPEED)
    if SOURCE == "sim":
        return pipeline.sim_source(next(iter(pipeline.SLOTS)), FS, 10 ** 7, SIM_FAULT_AT, SIM_SPEED, fixed=sim_fixed)
    raise SystemExit(f"unknown SENSOR_SOURCE: {SOURCE}")


def _sensor_thread():
    for st in pipe.slots.values():                     # every startup = fresh 60 s baseline
        pipe.start_baseline(st, time.time(), force=True)
    pipeline.run(pipe, _make_source(), FS, cmds)


# ---------------------------------------------------------------------------
# Turning pipeline rows into Spacetime writes + alerts
# ---------------------------------------------------------------------------

def _band_view(pod: int, feats: dict):
    """z-score of each frequency band vs the baseline, and the band that moved most."""
    b = pipe.slots[pod].baseline
    if b is None:
        return [], [], (0.0, 0.0)
    bands = list(dsp.BANDS.items())
    zs = [(feats[n] - b.mean[n]) / b.std[n] for n, _ in bands]
    worst = max(range(len(bands)), key=lambda i: abs(zs[i]))
    return [list(r) for _, r in bands], [round(z, 2) for z in zs], bands[worst][1]


def _eta_text(eta) -> str:
    if not eta:
        return "no clear downward trend yet"
    if eta["demo_s"] == 0:
        return "it's at the failure threshold now"
    return (f"~{eta['runtime_hours']} hours of runtime (~{eta['shifts']} shifts, around "
            f"{eta['calendar_estimate'].replace('T', ' ')}), a rough linear estimate")


def _set_mode(mode: str, note: str):
    if mode != S["mode"]:
        S["mode"] = mode
        store.set_pod_mode(mode, note)


async def _handle_row(ctx: Context, table: str, row: dict):
    if table == "slots":
        mode = {"baseline": "training", "monitoring": "monitoring"}.get(row["mode"], "idle")
        _set_mode(mode, f"{row['slot_id']}: {row['mode']}")
        if mode == "training":
            ctx.logger.info(f"TRAINING baseline ({pipeline.BASELINE_S} windows) on {row['slot_id']}")
            store.log_event("watcher", "training_started", row["slot_id"], part=part_of(row["slot_id"]))
        return
    if table == "baselines":
        S["retraining"].discard(row["slot_id"])
        ctx.logger.info(f"baseline fitted on {row['slot_id']} ({row['n_windows']} windows) - MONITORING")
        store.log_event("watcher", "training_done", json.dumps({"mean": row["mean"]}), part=part_of(row["slot_id"]))
        return
    if table == "baseline_events":
        ctx.logger.warning(f"baseline {row['result']}: {row['reason']}")
        store.log_event("watcher", f"baseline_{row['result']}", row["reason"], part=part_of(row["slot_id"]))
        return
    if table == "shift_summaries":
        store.log_event("watcher", "shift_summary", json.dumps(row), part=part_of(row["slot_id"]))
        return
    if table != "part_health" or row.get("health") is None:
        return                                       # alerts rows: incidents are decided below instead
    if row["slot_id"] in S["retraining"]:
        return                                       # scored before the operator's fix - stale, drop it

    part, pod, status = part_of(row["slot_id"]), row["pod_id"], row["status"]
    feats = row["features"]
    bands, band_z, (lo, hi) = _band_view(pod, feats)
    top = row["top_deviations"] or [["none", 0.0]]
    eta = row["eta"]
    ph = PartHealth(part, row["health"], lo, hi, eta["demo_s"] if eta else None, row["ts"])
    store.report_pod_health(ph, status)
    store.write_spectrum_reading(part, json.dumps(bands), json.dumps(band_z), row["ts"])

    if status == "healthy":
        return                                       # going green does NOT close an incident
    prev = S["open"].get(part)
    if prev and RANK[status] <= RANK[prev["state"]]:
        return                                       # same incident, not worse

    incident_id = store.open_incident(part, status, ph.health)
    S["open"][part] = {"incident_id": incident_id, "state": status, "pod": pod}
    what = "incident_opened" if prev is None else "incident_worsened"
    evidence = ", ".join(f"{n} {z:+.1f} std dev" for n, z in top)
    ctx.logger.info(f"{what.upper()}: #{incident_id} {part} -> {status} ({evidence})")
    store.log_event("watcher", what, f"{part} -> {status} ({ph.health:.0f}%): {evidence}", part=part)
    await ctx.send(ANALYST_ADDRESS, PartAlert(
        incident_id=incident_id, state=status, part=part, health=ph.health,
        drift_lo_hz=lo, drift_hi_hz=hi, drift_z=float(top[0][1]), eta_s=ph.eta_s,
        updated_at=ph.updated_at, evidence=evidence, eta_text=_eta_text(eta),
    ))


# ---------------------------------------------------------------------------
# Agent wiring
# ---------------------------------------------------------------------------

@agent.on_event("startup")
async def startup(ctx: Context):
    try:
        for inc in store.open_incidents():           # after a restart, don't open duplicates
            S["open"][inc["part"]] = {"incident_id": inc["id"], "state": inc["state"], "pod": None}
            ctx.logger.info(f"resuming open incident #{inc['id']} on {inc['part']} ({inc['state']})")
    except Exception as e:
        ctx.logger.error(f"couldn't read open incidents: {e}")
    threading.Thread(target=_sensor_thread, daemon=True).start()
    ctx.logger.info(f"sensor thread started: source={SOURCE} fs={FS}")


@agent.on_interval(period=0.25)
async def drain(ctx: Context):
    for _ in range(200):                             # bounded, so one slow write can't stall the loop
        try:
            table, row = sink.q.get_nowait()
        except queue.Empty:
            return
        try:
            await _handle_row(ctx, table, row)
        except Exception as e:
            ctx.logger.error(f"handling {table} row failed: {e}")


@agent.on_interval(period=2.0)
async def check_resolutions(ctx: Context):
    """Operator texted RESOLVED -> Analyst resolved the incident -> retrain on the new normal."""
    if not S["open"]:
        return
    still_open = {i["id"] for i in store.open_incidents()}
    for part, o in list(S["open"].items()):
        if o["incident_id"] in still_open:
            continue
        del S["open"][part]
        pod = o["pod"] if o["pod"] is not None else next(p for p, st in pipe.slots.items() if part_of(st.slot_id) == part)
        ctx.logger.info(f"incident #{o['incident_id']} on {part} resolved by operator - retraining pod {pod}")
        S["retraining"].add(pipe.slots[pod].slot_id)
        sim_fixed.set()
        cmds.put(f"replace {pod}")       # pipeline closes its own alert + forces a fresh baseline


@agent.on_interval(period=10.0)
async def heartbeat(ctx: Context):
    store.heartbeat("watcher", S["mode"] or "starting")


if __name__ == "__main__":
    agent.run()
