"""End-to-end test of the whole chain WITHOUT Spacetime or a phone:  python3 test_flow.py

Runs the real Watcher, Analyst, Buyer and all 7 suppliers in one process, against an in-memory
FakeStore that enforces the same rules as the Spacetime reducers (resolve and approve both need a
real inbound text). A scripted "operator" answers the Analyst's texts. Passes if one incident goes
all the way: alert -> observation -> diagnosis -> sourcing -> escalation -> YES -> payment ->
RESOLVED -> Watcher retrains -> back to monitoring with no new incident.
SCENARIO=clear python3 test_flow.py runs the no-purchase path instead."""
import asyncio
import itertools
import os
import re
import sys
import time

os.environ.setdefault("SIM_SPEED", "20")      # pipeline sim at 20x: 60-window baseline in 3 s
os.environ.setdefault("SIM_FAULT_AT", "70")   # obstruction starts right after the first baseline
os.environ["PDM_NO_MAILBOX"] = "1"
os.environ.setdefault("SENSOR_SOURCE", "sim")

YES = {"yes", "y", "yep", "yeah", "approve", "approved", "ok", "okay"}
NO = {"no", "n", "nope", "reject", "rejected"}


class FakeStore:
    """Same method names as store.Store, backed by dicts. Shared by every agent in this process."""
    T: dict = {k: [] for k in ("part_health", "order_proposal", "rfq", "supplier_quote", "escalation",
                                "incident", "operator_note", "outbound", "inbound", "events", "diagnosis")}
    ids = itertools.count(1)
    pod = {"mode": None}

    def __init__(self, *a, **k): pass

    # -- generic --
    def heartbeat(self, *a, **k): pass
    def log_event(self, agent, event_type, detail="", **k):
        self.T["events"].append((agent, event_type, detail))
    def rows(self, q):
        m = re.match(r"SELECT \* FROM rfq WHERE id = (\d+)", q)
        return [r for r in self.T["rfq"] if r["id"] == int(m.group(1))] if m else []

    # -- health --
    def report_health(self, ph):
        self.T["part_health"] = [r for r in self.T["part_health"] if r["part"] != ph.part] + [
            {"part": ph.part, "health": ph.health, "eta_s": -1 if ph.eta_s is None else ph.eta_s}]
    def report_pod_health(self, ph, state):
        self.report_health(ph); self.T["part_health"][-1]["state"] = state
    def write_spectrum_reading(self, *a): pass
    def latest_health(self): return list(self.T["part_health"])
    def set_pod_mode(self, mode, note=""):
        self.pod["mode"] = mode; self.log_event("pod", mode, note)

    # -- incidents (mirror the reducers' checks) --
    def open_incident(self, part, state, health):
        inc = self.open_incident_for(part)
        if inc:
            inc["state"], inc["worst_health"] = state, min(inc["worst_health"], health)
        else:
            inc = {"id": next(self.ids), "part": part, "state": state, "status": "open",
                   "worst_health": health, "rfq_id": -1, "order_id": -1, "resolution": ""}
            self.T["incident"].append(inc)
        return inc["id"]
    def open_incident_for(self, part):
        return next((i for i in self.T["incident"] if i["part"] == part and i["status"] == "open"), None)
    def open_incidents(self): return [i for i in self.T["incident"] if i["status"] == "open"]
    def resolve_incident(self, incident_id, inbound_id, resolution):
        inc = next(i for i in self.T["incident"] if i["id"] == incident_id)
        assert inc["status"] == "open", "already resolved"
        assert any(m["id"] == inbound_id for m in self.T["inbound"]), "resolve without a real text"
        inc.update(status="resolved", resolution=resolution)
    def link_incident(self, incident_id, rfq_id=-1, order_id=-1):
        inc = next(i for i in self.T["incident"] if i["id"] == incident_id)
        if rfq_id >= 0: inc["rfq_id"] = rfq_id
        if order_id >= 0: inc["order_id"] = order_id
    def add_operator_note(self, incident_id, inbound_id, text):
        self.T["operator_note"].append({"id": next(self.ids), "incident_id": incident_id, "text": text})
    def operator_notes_for(self, incident_id):
        return [n for n in self.T["operator_note"] if n["incident_id"] == incident_id]
    def write_diagnosis(self, part, inc_id, cause, conf, ev, rec):
        self.T["diagnosis"].append({"part": part, "incident_id": inc_id, "cause": cause, "recommended": rec})

    # -- phone --
    def queue_text(self, text): self.T["outbound"].append({"id": next(self.ids), "text": text, "sent": False})
    def receive_text(self, text):   # what the Photon bridge would call
        self.T["inbound"].append({"id": next(self.ids), "text": text, "handled": False})
    def unhandled_texts(self): return [m for m in self.T["inbound"] if not m["handled"]]
    def mark_text_handled(self, i): next(m for m in self.T["inbound"] if m["id"] == i)["handled"] = True
    def decide_order_from_text(self, order_id, inbound_id, status):
        msg = next(m for m in self.T["inbound"] if m["id"] == inbound_id)
        w = (re.match(r"\s*([a-z]+)", msg["text"].lower()) or [None, ""])[1]
        assert (status == "approved" and w in YES) or (status == "rejected" and w in NO), "text doesn't say it"
        o = next(o for o in self.T["order_proposal"] if o["id"] == order_id)
        assert o["status"] == "needs_approval"
        o.update(status=status, channel="imessage", approved_by="operator", updated_at=time.time())

    # -- procurement --
    def propose_order(self, o):
        self.T["order_proposal"].append({"id": next(self.ids), "part": o.part, "supplier": o.supplier, "qty": o.qty,
            "unit_price": o.unit_price, "lead_days": o.lead_days, "reason": o.reason, "status": o.status,
            "updated_at": time.time(), "channel": "", "approved_by": ""})
    def all_orders(self): return list(self.T["order_proposal"])
    def pending_orders(self): return [o for o in self.T["order_proposal"] if o["status"] == "needs_approval"]
    def approved_imessage_orders(self):
        return [o for o in self.T["order_proposal"] if o["status"] == "approved" and o["channel"] == "imessage"]
    def create_rfq(self, part, max_price, max_wait_days, priority):
        r = {"id": next(self.ids), "part": part, "max_price": max_price, "max_wait_days": max_wait_days,
             "priority": priority, "status": "collecting"}
        self.T["rfq"].append(r); return r["id"]
    def write_supplier_quote(self, rfq_id, supplier, available, unit_price, lead_days, shipping_cost, counter_note, substitute_part):
        self.T["supplier_quote"].append(dict(rfq_id=rfq_id, supplier=supplier, available=available, unit_price=unit_price,
            lead_days=lead_days, shipping_cost=shipping_cost, counter_note=counter_note, substitute_part=substitute_part))
    def complete_rfq(self, rfq_id, status): next(r for r in self.T["rfq"] if r["id"] == rfq_id)["status"] = status
    def supplier_quotes_for(self, rfq_id): return [q for q in self.T["supplier_quote"] if q["rfq_id"] == rfq_id]
    def create_escalation(self, *a): self.T["escalation"].append(a)


import store
store.Store = FakeStore          # every agent module does `from store import Store` - patch before import

from uagents import Agent, Bureau, Context   # noqa: E402
import watcher_agent, analyst_agent, buyer_agent, suppliers_swarm   # noqa: E402
from messages import SUPPLIER_CONFIGS   # noqa: E402

fs = FakeStore()
# Scripted operator: (pattern in the Analyst's text) -> reply
SCRIPT = [
    ("Please go take a look", "there's dried glue stuck on the gear and it's grinding"),
] + ([  # SCENARIO=clear: no-purchase path - clear the obstruction, nothing gets ordered
    ("Reply with a number", "1"),
    ("No purchase needed", "it's not fixed yet"),      # must NOT resolve
    ("Noted. Text RESOLVED", "cleared it, running clean now"),
] if os.environ.get("SCENARIO") == "clear" else [

    ("Reply with a number", "4"),                 # 4 = replace (purchase path)
    ("most you'll pay", "40"),
    ("how many days", "5"),
    ("PRICE or SPEED", "price"),
    ("Quick decision needed", "1"),               # accept the escalated offer
    ("Reply YES to order", "yes"),
    ("Ordered (simulated)", "replaced it, all good"),
])
transcript = []
phone = Agent(name="fake_phone", seed="pdm-test-phone")


@phone.on_interval(period=0.5)
async def operator(ctx: Context):
    for m in [m for m in fs.T["outbound"] if not m["sent"]]:
        m["sent"] = True
        transcript.append(("AGENT", m["text"]))
        for pat, reply in SCRIPT:
            if pat in m["text"]:
                transcript.append(("OPERATOR", reply))
                fs.receive_text(reply)
                break


bureau = Bureau(port=8299, loop=asyncio.get_event_loop())
for a in [watcher_agent.agent, analyst_agent.agent, buyer_agent.agent, phone] + \
         [suppliers_swarm.make_supplier(c) for c in SUPPLIER_CONFIGS]:
    bureau.add(a)


async def main():
    task = asyncio.create_task(bureau.run_async())
    deadline = time.time() + 90
    done_at = None
    while time.time() < deadline:
        await asyncio.sleep(1)
        resolved = [i for i in fs.T["incident"] if i["status"] == "resolved"]
        if resolved and done_at is None and fs.pod["mode"] == "monitoring" and fs.pod["mode"] == "monitoring":
            done_at = time.time()
        if done_at and time.time() - done_at > 6:    # watch a few extra seconds for a bogus re-alert
            break
    task.cancel()

    print("\n================ PHONE TRANSCRIPT ================")
    for who, t in transcript:
        print(f"\n[{who}]\n{t}")
    print("\n================ CHECKS ================")
    inc = fs.T["incident"]
    if os.environ.get("SCENARIO") == "clear":
        checks = {
            "incident resolved by operator text": bool(inc) and inc[0]["status"] == "resolved",
            "'not fixed yet' did NOT resolve it": any(n["text"] == "it's not fixed yet" for n in fs.T["operator_note"]),
            "nothing was ordered": not fs.T["order_proposal"] and not fs.T["rfq"],
            "watcher retrained after resolve": sum(1 for e in fs.T["events"] if e[1] == "training_started") == 2,
            "no new incident after resolve": len(inc) == 1,
        }
        for name, ok in checks.items():
            print(f"{'PASS' if ok else 'FAIL'}  {name}")
        sys.exit(0 if all(checks.values()) else 1)
    checks = {
        "exactly one incident opened": len(inc) == 1,
        "incident resolved by operator text": bool(inc) and inc[0]["status"] == "resolved",
        "incident linked to rfq + order": bool(inc) and inc[0]["rfq_id"] > 0 and inc[0]["order_id"] > 0,
        "diagnosis cited the obstruction": any("caught" in d["cause"] for d in fs.T["diagnosis"]),
        "escalation went through the Analyst": bool(fs.T["escalation"]),
        "order approved via text": any(o["status"] == "approved" and o["channel"] == "imessage" for o in fs.T["order_proposal"]),
        "payment completed": any(e[1] == "payment_completed" for e in fs.T["events"]),
        "watcher retrained after resolve": sum(1 for e in fs.T["events"] if e[1] == "training_started") == 2,
        "back to monitoring": fs.pod["mode"] == "monitoring",
        "no new incident after resolve": len(inc) == 1,
    }
    for name, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    sys.exit(0 if all(checks.values()) else 1)


# agents grab the default loop when they're constructed, so run on that same loop
asyncio.get_event_loop().run_until_complete(main())
