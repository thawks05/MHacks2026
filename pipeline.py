"""
pipeline.py — everything between the host ESP32 and the database.

  serial (host CSV) -> 1 s windows -> dsp features -> mode (baseline / monitoring)
  -> status hold (watch / degraded / critical) -> alert gate -> shift summary + ETA
  -> sink.emit(table, row)

Right now the sink PRINTS each row as one JSON line. Next stage: replace
ConsoleSink with a SpacetimeDB sink, and replace the stdin commands + state file
with reads of the slots / alerts / baselines tables.

Run:
  python3 pipeline.py --sim --auto-baseline --speed 0                 # no hardware, fast
  python3 pipeline.py --port /dev/cu.usbserial-0001 --record rec.csv  # live from host ESP32
  python3 pipeline.py --file rec.csv --auto-baseline                  # replay a recording

Live commands (type + Enter):  b = baseline   replace = part replaced (resolve + baseline)
                                r = resolve alert   q = quit      (optionally add a pod id: "b 1")
"""
from __future__ import annotations

import argparse
import json
import math
import queue
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

import dsp

# ============================================================================
# Config
# ============================================================================
# Permanent slot IDs. Key = pod id printed by host.ino (later: pod MAC address).
SLOTS: dict[int, str] = {
    1: "conveyor_17_phx_az/drive_motor",
}

WINDOW_S = 1.0               # one scored row per second
BASELINE_S = 60              # windows needed for a baseline
MIN_BASELINE_RMS_G = 0.003   # quieter than this = motor probably off
MAX_BASELINE_REJECT = 0.2    # abort baseline if >20 % of windows are rejected

# Status hold rule: consecutive deviating windows (demo seconds)
WATCH_S, DEGRADED_S, CRITICAL_S = 10, 20, 30
RECOVER_S = 10               # normal windows in a row before the run resets to healthy
NOTIFY_AT = "degraded"       # alert's agent_notification_stage -> "open" at this severity

# Simulated time: 60 demo seconds = one 8 h shift of machine runtime
SHIFT_S = 60
SHIFT_HOURS = 8.0
TIME_SCALE = SHIFT_HOURS * 3600 / SHIFT_S        # 480 runtime seconds per demo second
WORKDAYS = {0, 1, 2, 3, 4}                       # Mon-Fri
SHIFT_START_H, SHIFT_END_H = 9, 17               # 9-5

ETA_FIT_S = 30               # fit the health trend over the last 30 windows
FAIL_HEALTH = 20.0           # health at which we call the part "failed" for ETA purposes

STATUS_ORDER = ["healthy", "watch", "degraded", "critical"]


def rank(status: str) -> int:
    return STATUS_ORDER.index(status) if status in STATUS_ORDER else -1


def level_for(run_s: int) -> str:
    if run_s >= CRITICAL_S:
        return "critical"
    if run_s >= DEGRADED_S:
        return "degraded"
    if run_s >= WATCH_S:
        return "watch"
    return "healthy"


def r4(v):
    return None if v is None else round(float(v), 4)


# ============================================================================
# Sinks (the only place that "writes")
# ============================================================================
class ConsoleSink:
    """Prints one JSON line per row. Swap for a DB sink in the next stage."""

    def emit(self, table: str, row: dict) -> None:
        print(json.dumps({"table": table, **row}, separators=(",", ":")), flush=True)


# ============================================================================
# Sources: each yields (pod_id, sample_idx, (x_g, y_g, z_g))
# ============================================================================
def parse_line(line: str, counts_per_g: float):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = line.split(",")
    if len(parts) != 5:
        return None
    try:
        pod, idx = int(parts[0]), int(parts[1])
        vals = parts[2:]
        if any("." in v for v in vals):            # host OUTPUT_IN_G = 1, or a recording
            xyz = tuple(float(v) for v in vals)
        else:                                       # raw int16 counts
            xyz = tuple(int(v) / counts_per_g for v in vals)
    except ValueError:
        return None
    return pod, idx, xyz


def serial_source(port: str, baud: int, counts_per_g: float):
    import serial  # pip install pyserial
    with serial.Serial(port, baud, timeout=1) as ser:
        while True:
            raw = ser.readline()
            if not raw:
                continue
            parsed = parse_line(raw.decode(errors="ignore"), counts_per_g)
            if parsed:
                yield parsed


def file_source(path: str, counts_per_g: float, fs: float, speed: float):
    t_start = time.time()
    first_idx = None
    with open(path) as fh:
        for line in fh:
            parsed = parse_line(line, counts_per_g)
            if not parsed:
                continue
            if speed > 0:
                if first_idx is None:
                    first_idx = parsed[1]
                due = t_start + (parsed[1] - first_idx) / fs / speed
                delay = due - time.time()
                if delay > 0:
                    time.sleep(delay)
            yield parsed


def sim_source(pod: int, fs: float, seconds: float, fault_at: float, speed: float, seed: int = 0):
    """Healthy motor at ~48 Hz; after fault_at the obstruction ramps in over 20 s:
    shaft slows to ~44 Hz, vibration grows, and a once-per-revolution impact appears."""
    rng = np.random.default_rng(seed)
    phase = 0.0
    ring = 0.0
    t_start = time.time()
    for idx in range(int(seconds * fs)):
        t = idx / fs
        r = min(1.0, max(0.0, (t - fault_at) / 20.0))
        f_shaft = 48.0 - 4.0 * r
        amp = 0.03 * (1 + 1.5 * r)
        prev = phase
        phase += 2 * math.pi * f_shaft / fs
        if r > 0 and int(phase // (2 * math.pi)) != int(prev // (2 * math.pi)):
            ring = 0.35 * r                        # impact once per revolution
        impact = ring * math.sin(2 * math.pi * 300 * t)
        ring *= 0.8
        x = amp * math.sin(phase) + 0.4 * amp * math.sin(2 * phase) + impact + rng.normal(0, 0.01)
        y = 0.7 * amp * math.cos(phase) + 0.5 * impact + rng.normal(0, 0.01)
        z = 1.0 + 0.5 * amp * math.sin(phase + 1.0) + rng.normal(0, 0.01)
        if speed > 0 and idx % 50 == 0:
            delay = t_start + t / speed - time.time()
            if delay > 0:
                time.sleep(delay)
        yield pod, idx, (x, y, z)


# ============================================================================
# Windowing
# ============================================================================
class WindowBuilder:
    """Collects contiguous samples per pod; a gap in sample_idx drops the partial window."""

    def __init__(self, n: int):
        self.n = n
        self.buf: list = []
        self.first_idx = None
        self.expected = None
        self.gaps = 0

    def add(self, idx: int, xyz):
        if self.expected is not None and idx != self.expected:
            self.gaps += 1
            self.buf, self.first_idx = [], None
        if self.first_idx is None:
            self.first_idx = idx
        self.buf.append(xyz)
        self.expected = idx + 1
        if len(self.buf) >= self.n:
            out = (self.first_idx, np.asarray(self.buf, dtype=float))
            self.buf, self.first_idx = [], None
            return out
        return None


# ============================================================================
# Per-slot state
# ============================================================================
@dataclass
class SlotState:
    slot_id: str
    pod_id: int
    mode: str = "idle"                 # idle (no baseline) | baseline | monitoring
    baseline: dsp.Baseline | None = None
    bl_rows: list = field(default_factory=list)
    bl_rejected: int = 0
    status: str = "healthy"
    dev_run: int = 0
    ok_run: int = 0
    health_hist: deque = field(default_factory=lambda: deque(maxlen=ETA_FIT_S))
    shift_rows: list = field(default_factory=list)
    shift_index: int = 0

    @property
    def monitoring(self) -> bool:
        return self.mode == "monitoring"


# ============================================================================
# Alert gate (one active alert per slot)
# ============================================================================
class AlertGate:
    def __init__(self, sink, store):
        self.sink = sink
        self.store = store
        self.active: dict[str, dict] = dict(store.data.get("active_alerts", {}))

    def _save_emit(self, alert: dict):
        if alert["alert_status"] == "active":
            self.active[alert["slot_id"]] = alert
        else:
            self.active.pop(alert["slot_id"], None)
        self.store.set("active_alerts", self.active)
        self.sink.emit("alerts", alert)

    def update(self, st: SlotState, ts: float, evidence: dict):
        if st.status == "healthy":
            return                                         # closing is the agents'/UI's call
        alert = self.active.get(st.slot_id)
        changed = False
        if alert is None:
            alert = {
                "alert_id": uuid.uuid4().hex[:12],
                "slot_id": st.slot_id,
                "alert_status": "active",
                "severity": st.status,
                "agent_notification_stage": "pending",
                "opened_ts": ts,
            }
            changed = True
        elif rank(st.status) > rank(alert["severity"]):
            alert["severity"] = st.status
            changed = True
        # Python only ever moves pending -> open. Agents own the field after that.
        if alert["agent_notification_stage"] == "pending" and rank(alert["severity"]) >= rank(NOTIFY_AT):
            alert["agent_notification_stage"] = "open"
            changed = True
        if changed:
            alert["updated_ts"] = ts
            alert["evidence"] = evidence
            self._save_emit(dict(alert))

    def resolve(self, slot_id: str, ts: float, reason: str):
        alert = self.active.get(slot_id)
        if not alert:
            return False
        alert = dict(alert, alert_status="resolved", resolved_ts=ts, resolved_reason=reason, updated_ts=ts)
        self._save_emit(alert)
        return True


# ============================================================================
# Local state file (stand-in for "load baselines + active alerts from the DB on startup")
# ============================================================================
class StateStore:
    def __init__(self, path: str | None):
        self.path = Path(path) if path else None
        self.data = {}
        if self.path and self.path.exists():
            self.data = json.loads(self.path.read_text())

    def set(self, key, value):
        self.data[key] = value
        if self.path:
            self.path.write_text(json.dumps(self.data, indent=1))


# ============================================================================
# Time helpers (simulated shift time -> calendar)
# ============================================================================
def runtime_to_calendar(start: datetime, runtime_hours: float) -> datetime:
    """Walk forward through 9-5 weekday shifts until runtime_hours are used up."""
    cur, remaining = start, runtime_hours
    for _ in range(3650):
        if cur.weekday() in WORKDAYS:
            day_start = cur.replace(hour=SHIFT_START_H, minute=0, second=0, microsecond=0)
            day_end = cur.replace(hour=SHIFT_END_H, minute=0, second=0, microsecond=0)
            if cur < day_start:
                cur = day_start
            if cur < day_end:
                avail = (day_end - cur).total_seconds() / 3600
                if remaining <= avail:
                    return cur + timedelta(hours=remaining)
                remaining -= avail
        cur = (cur + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return cur


# ============================================================================
# Pipeline
# ============================================================================
class Pipeline:
    def __init__(self, sink, store, fs: float, every: int):
        self.sink, self.store, self.fs, self.every = sink, store, fs, max(1, every)
        self.alerts = AlertGate(sink, store)
        self.slots: dict[int, SlotState] = {}
        self.window_count = 0
        saved = store.data.get("baselines", {})
        for pod, slot_id in SLOTS.items():
            st = SlotState(slot_id=slot_id, pod_id=pod)
            if slot_id in saved:                            # loaded at startup
                st.baseline = dsp.Baseline.from_dict(saved[slot_id])
                st.mode = "monitoring"
            self.slots[pod] = st
            self._emit_slot(st)

    # ---------------- rows ----------------
    def _emit_slot(self, st: SlotState):
        self.sink.emit("slots", {
            "slot_id": st.slot_id, "pod_id": st.pod_id,
            "mode": st.mode, "monitoring": st.monitoring,
            "has_baseline": st.baseline is not None,
        })

    def _vs_baseline(self, feats: dict, sc: dsp.Score | None, b: dsp.Baseline | None):
        if b is None:
            return None
        return {n: {"baseline": r4(b.mean[n]), "now": r4(feats[n]),
                    "z": r4(sc.z[n]) if sc else None} for n in dsp.CORE_FEATURES}

    # ---------------- commands ----------------
    def command(self, cmd: str, now: float):
        parts = cmd.strip().split()
        if not parts:
            return
        verb = parts[0].lower()
        pod = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else next(iter(self.slots))
        st = self.slots.get(pod)
        if st is None:
            print(f"# unknown pod {pod}", file=sys.stderr)
            return
        if verb == "b":
            self.start_baseline(st, now, force=False)
        elif verb == "replace":
            self.alerts.resolve(st.slot_id, now, "part_replaced")
            self.start_baseline(st, now, force=True)
        elif verb == "r":
            if not self.alerts.resolve(st.slot_id, now, "manual"):
                print(f"# no active alert on {st.slot_id}", file=sys.stderr)

    def start_baseline(self, st: SlotState, now: float, force: bool):
        if st.slot_id in self.alerts.active and not force:
            print(f"# baseline blocked: {st.slot_id} has an active alert. "
                  f"Use 'replace' if the part was swapped.", file=sys.stderr)
            return
        st.mode, st.bl_rows, st.bl_rejected = "baseline", [], 0
        self._emit_slot(st)

    # ---------------- per window ----------------
    def process(self, pod: int, first_idx: int, win: np.ndarray, ts: float):
        st = self.slots.get(pod)
        if st is None:
            return
        self.window_count += 1
        feats = dsp.extract_features(win, self.fs)
        row = {"slot_id": st.slot_id, "pod_id": pod, "ts": round(ts, 3), "window_idx": first_idx,
               "monitoring": st.monitoring, "mode": st.mode}
        force_print = False

        if st.mode == "baseline":
            self._baseline_step(st, feats, ts)
            row.update(status="baselining", health=None, deviating_s=0, top_deviations=[],
                       vs_baseline=None, eta=None)
        elif st.mode == "monitoring":
            prev = st.status
            sc = dsp.score(feats, st.baseline)
            self._status_step(st, sc)
            st.health_hist.append((ts, sc.health))
            eta = self._eta(st, sc.health) if st.status != "healthy" else None
            row.update(status=st.status, health=round(sc.health, 1), deviating_s=st.dev_run,
                       top_deviations=[[n, r4(z)] for n, z in sc.top],
                       vs_baseline=self._vs_baseline(feats, sc, st.baseline), eta=eta)
            self.alerts.update(st, ts, {"health": row["health"], "top_deviations": row["top_deviations"],
                                        "vs_baseline": row["vs_baseline"], "eta": eta})
            self._shift_step(st, feats, row, ts)
            force_print = st.status != prev
        else:
            row.update(status="no_baseline", health=None, deviating_s=0, top_deviations=[],
                       vs_baseline=None, eta=None)

        row["features"] = {k: r4(v) for k, v in feats.items()}
        if force_print or self.window_count % self.every == 0:
            self.sink.emit("part_health", row)

    def _baseline_step(self, st: SlotState, feats: dict, ts: float):
        if feats["rms_g"] < MIN_BASELINE_RMS_G:
            st.bl_rejected += 1
            if st.bl_rejected > MAX_BASELINE_REJECT * BASELINE_S:
                st.mode = "monitoring" if st.baseline else "idle"
                self.sink.emit("baseline_events", {"slot_id": st.slot_id, "ts": ts, "result": "aborted",
                                                   "reason": "vibration too low: motor off or pod loose"})
                self._emit_slot(st)
            return
        st.bl_rows.append(feats)
        if len(st.bl_rows) < BASELINE_S:
            return
        b = dsp.Baseline.fit(st.bl_rows, self.fs, ts)
        st.baseline = b
        st.mode, st.status, st.dev_run, st.ok_run = "monitoring", "healthy", 0, 0
        st.health_hist.clear()
        st.shift_rows, st.shift_index = [], 0
        saved = dict(self.store.data.get("baselines", {}))
        saved[st.slot_id] = b.to_dict()
        self.store.set("baselines", saved)
        self.sink.emit("baselines", {"slot_id": st.slot_id, "active": True, "created_ts": ts,
                                     "n_windows": b.n_windows, "fs": b.fs,
                                     "mean": {k: r4(v) for k, v in b.mean.items()},
                                     "std": {k: r4(v) for k, v in b.std.items()}})
        self._emit_slot(st)

    def _status_step(self, st: SlotState, sc: dsp.Score):
        if sc.deviating:
            st.dev_run += 1
            st.ok_run = 0
        else:
            st.ok_run += 1
            if st.ok_run >= RECOVER_S:
                st.dev_run = 0
        if st.dev_run == 0:
            st.status = "healthy"
        else:
            level = level_for(st.dev_run)
            if rank(level) > rank(st.status):
                st.status = level

    def _eta(self, st: SlotState, health_now: float):
        if health_now <= FAIL_HEALTH:
            demo_s = 0.0
        elif len(st.health_hist) < 5:
            return None
        else:
            t = np.array([p[0] for p in st.health_hist])
            h = np.array([p[1] for p in st.health_hist])
            slope = np.polyfit(t - t[0], h, 1)[0]          # health per demo second
            if slope >= -0.05:
                return None                                 # not trending down
            demo_s = float((health_now - FAIL_HEALTH) / -slope)
        runtime_h = demo_s * TIME_SCALE / 3600
        return {
            "demo_s": round(demo_s, 1),
            "runtime_hours": round(runtime_h, 1),
            "shifts": round(runtime_h / SHIFT_HOURS, 2),
            "calendar_estimate": runtime_to_calendar(datetime.now(), runtime_h).isoformat(timespec="minutes"),
            "label": "rough linear estimate, simulated shift time",
        }

    def _shift_step(self, st: SlotState, feats: dict, row: dict, ts: float):
        st.shift_rows.append((feats, row["health"], row["status"], ts))
        if len(st.shift_rows) < SHIFT_S:
            return
        f = {n: np.array([r[0][n] for r in st.shift_rows]) for n in dsp.CORE_FEATURES}
        health = np.array([r[1] for r in st.shift_rows])
        worst = max((r[2] for r in st.shift_rows), key=rank)
        self.sink.emit("shift_summaries", {
            "slot_id": st.slot_id, "shift_index": st.shift_index,
            "start_ts": round(st.shift_rows[0][3], 3), "end_ts": round(ts, 3),
            "monitoring": True, "worst_status": worst,
            "health_mean": round(float(health.mean()), 1), "health_min": round(float(health.min()), 1),
            "mean": {n: r4(v.mean()) for n, v in f.items()},
            "max": {n: r4(v.max()) for n, v in f.items()},
        })
        st.shift_rows, st.shift_index = [], st.shift_index + 1


# ============================================================================
# Main
# ============================================================================
def stdin_commands(q: queue.Queue):
    for line in sys.stdin:
        q.put(line)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--port", help="host ESP32 serial port")
    src.add_argument("--file", help="replay a recorded CSV")
    src.add_argument("--sim", action="store_true", help="synthetic healthy -> faulty motor")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--fs", type=float, default=1000.0, help="measured sample rate (host 'rate=' line)")
    ap.add_argument("--counts-per-g", type=float, default=8192.0, help="8192 for +-4 g")
    ap.add_argument("--every", type=int, default=1, help="print part_health every N windows (status changes always print)")
    ap.add_argument("--record", help="save every sample (in g) to this CSV for replay/calibration")
    ap.add_argument("--state", default="pdm_state.json", help="local stand-in for DB baselines/alerts ('' = none)")
    ap.add_argument("--auto-baseline", action="store_true", help="start a baseline immediately on every slot")
    ap.add_argument("--speed", type=float, default=1.0, help="replay/sim speed (0 = as fast as possible)")
    ap.add_argument("--sim-seconds", type=float, default=200)
    ap.add_argument("--fault-at", type=float, default=120, help="sim: seconds before the obstruction starts")
    args = ap.parse_args()

    sink = ConsoleSink()
    store = StateStore(args.state or None)
    pipe = Pipeline(sink, store, args.fs, args.every)
    if args.auto_baseline:
        for st in pipe.slots.values():
            pipe.start_baseline(st, time.time(), force=True)

    if args.port:
        source = serial_source(args.port, args.baud, args.counts_per_g)
    elif args.file:
        source = file_source(args.file, args.counts_per_g, args.fs, args.speed)
    else:
        source = sim_source(next(iter(SLOTS)), args.fs, args.sim_seconds, args.fault_at, args.speed)

    cmds: queue.Queue = queue.Queue()
    threading.Thread(target=stdin_commands, args=(cmds,), daemon=True).start()

    n = int(round(args.fs * WINDOW_S))
    builders: dict[int, WindowBuilder] = {}
    t0: dict[int, float] = {}
    rec = open(args.record, "w") if args.record else None
    warned: set[int] = set()

    try:
        for pod, idx, xyz in source:
            if rec:
                rec.write(f"{pod},{idx},{xyz[0]:.5f},{xyz[1]:.5f},{xyz[2]:.5f}\n")
            if pod not in pipe.slots:
                if pod not in warned:
                    print(f"# pod {pod} has no slot in SLOTS; ignoring", file=sys.stderr)
                    warned.add(pod)
                continue
            if pod not in t0:
                t0[pod] = time.time() - idx / args.fs        # pod's sample 0 in wall time
            b = builders.setdefault(pod, WindowBuilder(n))
            out = b.add(idx, xyz)
            if out is None:
                continue
            first_idx, win = out
            while not cmds.empty():
                cmd = cmds.get().strip()
                if cmd.lower() == "q":
                    return
                pipe.command(cmd, time.time())
            pipe.process(pod, first_idx, win, t0[pod] + first_idx / args.fs)
    except KeyboardInterrupt:
        pass
    finally:
        if rec:
            rec.close()
        gaps = {p: b.gaps for p, b in builders.items() if b.gaps}
        if gaps:
            print(f"# sample gaps per pod: {gaps} (host PRINT_DECIMATE must be 1, CSV mode)", file=sys.stderr)


if __name__ == "__main__":
    main()
