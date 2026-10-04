"""Analyst agent (Layer 2): diagnoses, and is the ONLY agent that talks to the operator's phone.

Phone channel (Photon / iMessage): the Analyst never touches Photon directly. It writes outgoing
texts to Spacetime (queue_text -> outbound_message) and reads replies from inbound_message; the
Photon bridge (photon_bridge/bridge.ts) is a dumb pipe that sends and receives. No other agent
writes to outbound_message.

One incident conversation, start to finish:
  PartAlert from Watcher  -> text the operator: what's wrong, go look, text back what you see
  operator's observation  -> save it, diagnose (sensor + what they said), text numbered options
  pick a no-cost option   -> "do it, text RESOLVED when it's running clean"
  pick repair/replace     -> ask max price, max wait, price-or-speed -> SourceRequest to Buyer
  Buyer escalates         -> text the counteroffer/substitute, relay their pick back
  Buyer's SourceResult    -> text the comparison, "reply YES to order or NO"
  YES                     -> decide_order_from_text (reducer checks the real text says yes)
  PaymentCompleted        -> "ordered, install it, text RESOLVED"
  RESOLVED (any time)     -> resolve_incident (reducer checks it's a real operator text)
                             -> Watcher sees it and retrains the baseline for 60s

Incidents only close on the operator's text. Nothing else in the system can close one.

ASI:One chat is READ-ONLY: status, "why", risk, order history. It cannot approve, reject, or
resolve anything, and says so if asked.

Diagnosis is keyword rules over the operator's free text plus the sensor numbers (diagnose()).
It's deliberately one function so it can be swapped for an LLM call later without touching the
conversation flow."""
import json
import os
import re
from datetime import datetime
from typing import Optional

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, MetadataContent, TextContent, chat_protocol_spec,
)

from store import Store
from suppliers import average
from messages import (
    PartAlert, SourceRequest, SourceResult, PaymentCompleted, EscalationNeeded, EscalationAnswer,
    ANALYST_SEED, ANALYST_PORT, BUYER_ADDRESS,
)

store = Store()
PARTS = ["drive_gear", "belt", "motor_shaft"]

agent = Agent(name="analyst", seed=ANALYST_SEED, port=ANALYST_PORT,
              mailbox=os.environ.get("PDM_NO_MAILBOX") != "1", publish_agent_details=True)
print("ANALYST_ADDRESS", agent.address)
chat_proto = Protocol(spec=chat_protocol_spec)

# incident_id -> latest PartAlert for it
alerts: dict[int, PartAlert] = {}
# The phone conversation is about one incident at a time; others wait in `queued`.
convo: dict = {"incident_id": None, "stage": None}
queued: list[int] = []


def text(body: str):
    """Send a text to the operator's phone (via Spacetime -> Photon bridge)."""
    store.queue_text(body)


# ---------------------------------------------------------------------------
# Small parsers
# ---------------------------------------------------------------------------

_YES = {"yes", "y", "yep", "yeah", "approve", "approved", "ok", "okay"}
_NO = {"no", "n", "nope", "reject", "rejected"}   # keep both sets in sync with decide_order_from_text


def _first_word(t: str) -> str:
    m = re.match(r"\s*([a-z]+)", t.lower())
    return m.group(1) if m else ""


def _numbers(t: str) -> list[float]:
    return [float(x) for x in re.findall(r"\d+(?:\.\d+)?", t)]


def _priority(t: str) -> Optional[str]:
    t = t.lower()
    if any(w in t for w in ("speed", "fast", "quick", "asap", "time")):
        return "speed"
    if any(w in t for w in ("price", "cheap", "cost", "money")):
        return "price"
    return None


def _is_resolution(t: str) -> bool:
    t = t.lower()
    if re.search(r"\b(not|isn'?t|wasn'?t|hasn'?t|still)\b", t):
        return False
    return bool(re.search(r"\b(resolved|fixed|repaired|replaced|cleared|all good|running clean)\b", t))


def _mentioned_part(t: str) -> Optional[str]:
    t = t.lower()
    return next((p for p in PARTS if p in t or p.replace("_", " ") in t), None)


def _evidence(alert: PartAlert) -> str:
    """Top deviating features from pipeline.py, or the single drifting band from the old health.py path."""
    if alert.evidence:
        return alert.evidence
    return f"{alert.drift_lo_hz:.0f}-{alert.drift_hi_hz:.0f} Hz band {alert.drift_z:+.1f} std dev"


def _eta(alert: PartAlert) -> str:
    if alert.eta_text:
        return alert.eta_text
    return f"~{alert.eta_s:.0f}s (rough estimate)" if alert.eta_s else "no clear trend yet"


# ---------------------------------------------------------------------------
# Diagnosis: sensor numbers + operator's free-text notes -> cause, confidence, options
# ---------------------------------------------------------------------------

_SIGNS = {
    "obstruction": ("stuck", "caught", "jam", "debris", "glue", "plastic", "obstruct", "blocked", "something on"),
    "noise": ("grind", "creak", "squeal", "squeak", "click", "knock", "rattle", "noise", "sound", "loud"),
    "heat": ("hot", "heat", "warm", "smell", "burn", "smoke"),
    "slip": ("slow", "slip", "loose", "wobble", "sag", "misalign"),
}


def _signs_in(notes: list[str]) -> set[str]:
    found = set()
    for note in notes:
        n = note.lower()
        for sign, words in _SIGNS.items():
            for w in words:
                i = n.find(w)
                if i >= 0 and not re.search(r"\b(no|nothing|not|isn'?t|without)\b\W+(\w+\W+){0,2}$", n[:i]):
                    found.add(sign)
    return found


def diagnose(alert: PartAlert, notes: list[str]) -> dict:
    part, signs = alert.part, _signs_in(notes)
    if "obstruction" in signs:
        cause, conf, first = "something caught in the mechanism", 0.75, "clear"
    elif "slip" in signs:
        cause, conf, first = "loose or misaligned drive (slipping/tension problem)", 0.6, "adjust"
    elif {"noise", "heat"} <= signs:
        cause, conf, first = f"worn {part} contact surface (grinding plus heat)", 0.65, "replace"
    elif "noise" in signs:
        cause, conf, first = f"wear on the {part} contact surface (abnormal noise)", 0.5, "repair"
    elif "heat" in signs:
        cause, conf, first = "excess friction, likely lubrication or alignment", 0.5, "adjust"
    elif notes:
        cause, conf, first = f"{part} wear consistent with the vibration drift; your notes don't point to a specific cause", 0.35, "repair"
    else:
        cause, conf, first = f"{part} wear consistent with the vibration drift (sensor data only)", 0.3, "repair"

    catalog = {
        "clear": ("Clear the obstruction", False),
        "adjust": ("Adjust tension/alignment", False),
        "repair": ("Repair with a sourced part", True),
        "replace": (f"Replace the {part}", True),
    }
    order = [first] + [k for k in ("clear", "adjust", "repair", "replace") if k != first]
    if "obstruction" not in signs:
        order = [k for k in order if k != "clear"]
    options = [{"id": k, "label": catalog[k][0], "needs_purchase": catalog[k][1]} for k in order]

    evidence = [f"sensor: {_evidence(alert)} from baseline"]
    evidence += [f'operator: "{n}"' for n in notes]
    return {"part": part, "likely_cause": cause, "confidence": conf, "evidence": evidence,
            "recommended_action": options[0]["label"], "options": options}


# ---------------------------------------------------------------------------
# Conversation steps (each sends texts and sets the next stage)
# ---------------------------------------------------------------------------

def _start_incident(alert: PartAlert):
    convo.clear()
    convo.update(incident_id=alert.incident_id, stage="observing", part=alert.part)
    text(f"⚠️ {alert.state.upper()}: {alert.part} is at {alert.health:.0f}% health (incident #{alert.incident_id}).\n"
         f"What moved vs its healthy baseline: {_evidence(alert)}. Time to failure: {_eta(alert)}.\n"
         f"Please go take a look and text me what you see, hear or feel (noise, heat, something stuck, running slow...). "
         f"Text RESOLVED once it's fixed.")


def _send_diagnosis(ctx: Context):
    inc_id = convo["incident_id"]
    alert = alerts[inc_id]
    notes = [n["text"] for n in store.operator_notes_for(inc_id)]
    d = diagnose(alert, notes)
    convo.update(stage="choosing_option", options=d["options"])
    try:
        store.write_diagnosis(d["part"], inc_id, d["likely_cause"], d["confidence"],
                              json.dumps(d["evidence"]), d["recommended_action"])
        store.log_event("analyst", "diagnosis_written", d["likely_cause"], part=d["part"])
    except Exception as e:
        ctx.logger.error(f"write_diagnosis failed: {e}")
    lines = [f"{i}. {o['label']}" + ("" if o["needs_purchase"] else " (no purchase)")
             for i, o in enumerate(d["options"], 1)]
    text(f"Diagnosis for {d['part']}: likely {d['likely_cause']} ({d['confidence'] * 100:.0f}% confidence).\n"
         f"Evidence: " + "; ".join(d["evidence"]) + "\n\nOptions:\n" + "\n".join(lines) +
         "\n\nReply with a number, or tell me more.")


def _pick_option(t: str) -> Optional[dict]:
    options = convo.get("options", [])
    nums = _numbers(t)
    if nums and 1 <= int(nums[0]) <= len(options) and len(t.strip()) <= 3:
        return options[int(nums[0]) - 1]
    return next((o for o in options if o["label"].lower() in t.lower()), None)


def _ask_price():
    avg = average(convo["part"])
    convo["stage"] = "ask_price"
    text(f"{avg['count']} suppliers stock it: ${avg['min_price']:.0f}-${avg['max_price']:.0f}/unit "
         f"(avg ${avg['avg_price']:.2f}), {avg['min_lead_days']:.0f}-{avg['max_lead_days']:.0f} days "
         f"(avg {avg['avg_lead_days']:.1f}). Supplier data is MOCK.\n"
         f"What's the most you'll pay per unit? (You can also answer all at once, e.g. \"60, 3 days, speed\")")


async def _send_source_request(ctx: Context):
    convo["stage"] = "awaiting_buyer"
    await ctx.send(BUYER_ADDRESS, SourceRequest(
        incident_id=convo["incident_id"], part=convo["part"], max_price=convo["max_price"],
        max_wait_days=convo["max_wait_days"], priority=convo["priority"],
    ))
    text(f"Asking all 7 suppliers now (max ${convo['max_price']:.0f}, {convo['max_wait_days']:.0f} days, "
         f"prioritizing {convo['priority']})...")


def _resolve(ctx: Context, inbound_id: int, raw: str):
    inc_id, part = convo["incident_id"], convo.get("part")
    store.resolve_incident(inc_id, inbound_id, raw)
    store.log_event("analyst", "incident_resolved", raw, part=part or "")
    ctx.logger.info(f"incident #{inc_id} RESOLVED by operator: {raw!r}")
    alerts.pop(inc_id, None)
    text(f"✅ Incident #{inc_id} ({part}) marked RESOLVED. The pod is retraining its healthy baseline "
         f"(about 60s) before it starts watching again.")
    convo.clear()
    convo.update(incident_id=None, stage=None)
    while queued:
        nxt = queued.pop(0)
        if nxt in alerts:
            _start_incident(alerts[nxt])
            break


def _status_text() -> str:
    inc_id = convo.get("incident_id")
    if inc_id is None:
        return _health_summary() + " No open incidents."
    a = alerts[inc_id]
    extra = f" {len(queued)} more incident(s) waiting." if queued else ""
    return (f"Incident #{inc_id}: {a.part} {a.state}, {a.health:.0f}% health. "
            f"Current step: {convo['stage'].replace('_', ' ')}.{extra}")


async def handle_text(ctx: Context, inbound_id: int, raw: str):
    t = raw.strip()
    tl = t.lower()
    stage = convo.get("stage")

    if tl in ("status", "?") or tl.startswith("status"):
        text(_status_text())
        return
    if tl in ("help", "commands"):
        text("I watch the machine and text you when a part needs attention. Reply with what you observe, "
             "pick options by number, YES/NO to approve orders, and RESOLVED once it's fixed. Text STATUS anytime.")
        return
    if convo.get("incident_id") is None:
        text(_health_summary() + " Nothing needs you right now.")
        return

    # RESOLVED works at every stage - the operator's word is what closes an incident.
    if stage != "approving" and _is_resolution(tl):
        _resolve(ctx, inbound_id, t)
        return

    inc_id = convo["incident_id"]

    if stage == "observing":
        store.add_operator_note(inc_id, inbound_id, t)
        _send_diagnosis(ctx)
        return

    if stage == "choosing_option":
        chosen = _pick_option(t)
        if chosen is None:          # not a pick -> treat as more observation and re-diagnose
            store.add_operator_note(inc_id, inbound_id, t)
            _send_diagnosis(ctx)
            return
        if not chosen["needs_purchase"]:
            convo["stage"] = "awaiting_fix"
            store.log_event("analyst", "option_chosen", chosen["label"], part=convo["part"])
            text(f"Got it: {chosen['label'].lower()}. No purchase needed. Text RESOLVED once it's running clean.")
            return
        store.log_event("analyst", "option_chosen", chosen["label"], part=convo["part"])
        _ask_price()
        return

    if stage == "ask_price":
        nums, prio = _numbers(t), _priority(t)
        if not nums:
            text("I didn't catch a number. What's the most you'll pay per unit, in dollars?")
            return
        convo["max_price"] = nums[0]
        if len(nums) >= 2 and prio:
            convo.update(max_wait_days=nums[1], priority=prio)
            await _send_source_request(ctx)
            return
        convo["stage"] = "ask_wait"
        text("And how many days max are you willing to wait for it?")
        return

    if stage == "ask_wait":
        nums = _numbers(t)
        if not nums:
            text("I didn't catch a number. How many days max?")
            return
        convo.update(max_wait_days=nums[0], stage="ask_priority")
        text("Last one: does PRICE or SPEED matter more?")
        return

    if stage == "ask_priority":
        prio = _priority(t)
        if prio is None:
            text("PRICE or SPEED?")
            return
        convo["priority"] = prio
        await _send_source_request(ctx)
        return

    if stage == "escalation":
        n = _numbers(t)
        pick = (int(n[0]) if n else None)
        answer = ("accept" if pick == 1 or "accept" in tl else
                  "next" if pick == 2 or "next" in tl else
                  "cancel" if pick == 3 or "cancel" in tl else None)
        if answer is None:
            text("Reply 1 (accept), 2 (next best option) or 3 (cancel).")
            return
        convo["stage"] = "awaiting_buyer"
        await ctx.send(BUYER_ADDRESS, EscalationAnswer(rfq_id=convo["rfq_id"], answer=answer))
        text({"accept": "Accepting that offer...", "next": "Checking the next best option...",
              "cancel": "Cancelling sourcing."}[answer])
        return

    if stage == "approving":
        order_id = convo["order_id"]
        w = _first_word(t)
        if w in _YES:
            store.decide_order_from_text(order_id, inbound_id, "approved")
            store.log_event("analyst", "order_approved", f"by text: {t}", part=convo["part"], order_id=order_id)
            convo["stage"] = "awaiting_payment"
            text(f"Approved order #{order_id}. Placing it now (simulated payment)...")
        elif w in _NO:
            store.decide_order_from_text(order_id, inbound_id, "rejected")
            store.log_event("analyst", "order_rejected", f"by text: {t}", part=convo["part"], order_id=order_id)
            convo["stage"] = "choosing_option"
            lines = [f"{i}. {o['label']}" for i, o in enumerate(convo.get("options", []), 1)]
            text(f"Order #{order_id} rejected. Options again:\n" + "\n".join(lines))
        else:
            text(f"Reply YES to order #{order_id} or NO to reject it.")
        return

    # awaiting_buyer / awaiting_payment / awaiting_fix: anything else is extra context
    store.add_operator_note(inc_id, inbound_id, t)
    if stage == "awaiting_fix":
        text("Noted. Text RESOLVED once it's fixed.")
    else:
        text("Noted. Still working on it.")


# ---------------------------------------------------------------------------
# Agent wiring: alerts in, phone texts in, Buyer results in
# ---------------------------------------------------------------------------

@agent.on_interval(period=10.0)
async def heartbeat(ctx: Context):
    store.heartbeat("analyst")


@agent.on_message(model=PartAlert)
async def handle_alert(ctx: Context, sender: str, msg: PartAlert):
    ctx.logger.info(f"ALERT #{msg.incident_id}: {msg.part} {msg.state} health={msg.health:.0f} z={msg.drift_z:+.1f}")
    is_update = msg.incident_id in alerts
    alerts[msg.incident_id] = msg
    if convo.get("incident_id") is None:
        _start_incident(msg)
    elif convo["incident_id"] == msg.incident_id:
        if is_update:
            text(f"Update on incident #{msg.incident_id}: {msg.part} is now {msg.state.upper()} ({msg.health:.0f}%).")
    elif msg.incident_id not in queued:
        queued.append(msg.incident_id)
        text(f"Heads up: {msg.part} also went {msg.state} ({msg.health:.0f}%). I'll walk you through it "
             f"after incident #{convo['incident_id']} is resolved.")


@agent.on_interval(period=1.0)
async def poll_texts(ctx: Context):
    try:
        rows = store.unhandled_texts()
    except Exception as e:
        ctx.logger.error(f"couldn't read inbound texts: {e}")
        return
    for row in rows:
        try:
            store.mark_text_handled(row["id"])   # mark first so a crash can't make us loop on it
            ctx.logger.info(f"TEXT IN #{row['id']}: {row['text']!r}")
            await handle_text(ctx, row["id"], row["text"])
        except Exception as e:
            ctx.logger.error(f"handling text #{row['id']} failed: {e}")
            text("Sorry, something went wrong on my end handling that. Try again, or text STATUS.")


@agent.on_message(model=EscalationNeeded)
async def handle_escalation(ctx: Context, sender: str, msg: EscalationNeeded):
    if convo.get("stage") != "awaiting_buyer" or convo.get("part") != msg.part:
        ctx.logger.warning(f"escalation for rfq #{msg.rfq_id} doesn't match the conversation - ignoring")
        return
    convo.update(stage="escalation", rfq_id=msg.rfq_id)
    text(f"Quick decision needed: {msg.offer_text}\n1. Accept this offer\n2. Try the next best option\n3. Cancel sourcing")


@agent.on_message(model=SourceResult)
async def handle_source_result(ctx: Context, sender: str, msg: SourceResult):
    ctx.logger.info(f"Buyer result: {msg.supplier} ${msg.unit_price}/{msg.lead_days}d (order #{msg.order_id})")
    if convo.get("part") != msg.part or convo.get("stage") != "awaiting_buyer":
        return
    if msg.order_id < 0:
        convo["stage"] = "choosing_option"
        lines = [f"{i}. {o['label']}" for i, o in enumerate(convo.get("options", []), 1)]
        text(f"{msg.reason} Pick another option:\n" + "\n".join(lines))
        return
    ranked = sorted(msg.options, key=lambda o: (not o.picked, not o.meets_requirements, o.unit_price))
    rows = [f"{'✅' if o.picked else ('•' if o.meets_requirements else '✗')} {o.supplier}: "
            f"${o.unit_price:.0f}, {o.lead_days}d" for o in ranked[:5]]
    convo.update(stage="approving", order_id=msg.order_id)
    text(f"Best option for {msg.part}: {msg.supplier}, ${msg.unit_price:.0f}/unit, {msg.lead_days}d lead.\n"
         f"Why: {msg.reason}\n\n" + "\n".join(rows) +
         f"\n(✅ picked, • meets your limits, ✗ misses them. Suppliers are MOCK.)\n"
         f"Example of this part type: {msg.product_url}\n\n"
         f"Reply YES to order it (order #{msg.order_id}, simulated payment) or NO.")


@agent.on_message(model=PaymentCompleted)
async def handle_payment_completed(ctx: Context, sender: str, msg: PaymentCompleted):
    ctx.logger.info(f"payment completed for order #{msg.order_id} (txn {msg.transaction_id})")
    if convo.get("order_id") == msg.order_id:
        convo["stage"] = "awaiting_fix"
    text(f"📦 Ordered (simulated): {msg.qty}x {msg.part} from {msg.supplier}, ${msg.unit_price * msg.qty:.0f}. "
         f"Install it, then text RESOLVED.")


# ---------------------------------------------------------------------------
# ASI:One chat - READ-ONLY questions. Never approves, rejects, or resolves.
# ---------------------------------------------------------------------------

def _order_status_line(o: dict) -> str:
    if o["status"] == "approved":
        when = datetime.fromtimestamp(o["updated_at"]).strftime("%H:%M")
        return f"approved by text at {when} ({o.get('approved_by', 'operator')})"
    return {"needs_approval": "waiting on the operator's YES by text", "rejected": "rejected"}.get(o["status"], o["status"])


def _health_summary() -> str:
    rows = store.latest_health()
    if not rows:
        return "No health data yet (the pod may still be training its baseline)."
    return "Current health: " + ", ".join(f"{r['part']} {r['health']:.0f}%" for r in rows) + "."


def _why_reasoning(part: Optional[str]) -> str:
    inc_id = convo.get("incident_id")
    if inc_id is None or (part and alerts[inc_id].part != part):
        return "Nothing's flagged for that right now, so there's no diagnosis to explain."
    a = alerts[inc_id]
    base = (f"Compared with {a.part}'s healthy baseline, the biggest changes are: {_evidence(a)}. "
            f"Anything within 2 std dev is normal noise; the pod flagged it {a.state} after it stayed out "
            f"of range, and health dropped to {a.health:.0f}%.")
    notes = store.operator_notes_for(inc_id)
    if notes:
        return base + " The operator also reported: " + "; ".join(f'"{n["text"]}"' for n in notes) + "."
    return base + " No operator observations yet, so this is sensor data only."


def _risk_forecast() -> str:
    rows = [r for r in store.latest_health() if r.get("eta_s") not in (None, -1)]
    if not rows:
        return "No part has a declining trend right now."
    rows.sort(key=lambda r: r["eta_s"])
    return "Most at risk, soonest first: " + ", ".join(f"{r['part']} (~{r['eta_s']:.0f}s, estimate)" for r in rows[:3]) + "."


def _order_history() -> str:
    orders = sorted(store.all_orders(), key=lambda o: o["id"], reverse=True)
    if not orders:
        return "No orders yet."
    return "Recent orders: " + "; ".join(
        f"#{o['id']} {o['part']} from {o['supplier']} ${o['unit_price']:.0f} ({_order_status_line(o)})" for o in orders[:5]) + "."


_HELP_TEXT = ("I watch this machine's parts for early signs of failure. When one needs attention I text the "
              "plant operator, combine what they see with the vibration data to diagnose it, and if a part "
              "is needed I get quotes from 7 (mock) suppliers. Every order and every 'resolved' only ever "
              "comes from the operator's text, never from this chat. Ask me: 'status', 'why', 'what's at risk', "
              "'order history', or about a part like 'belt'. Start messages with @pdm-analyst.")


def answer_chat(t: str) -> str:
    t = t.lower()
    if re.search(r"\b(approve|reject|cancel|resolve|resolved)\b", t) or t.strip() in ("yes", "y", "no", "n"):
        return ("Approvals and resolving incidents only happen by the operator's text message, not here. "
                "I can't approve, reject or resolve anything in this chat. " + _status_text())
    if any(w in t for w in ("what do you do", "what can you", "what are you", "help", "purpose")):
        return _HELP_TEXT
    if "why" in t:
        return _why_reasoning(_mentioned_part(t))
    if "risk" in t or "likely to fail" in t:
        return _risk_forecast()
    m = re.search(r"order\s*#?(\d+)", t)
    if m:
        o = next((x for x in store.all_orders() if x["id"] == int(m.group(1))), None)
        return (f"#{o['id']} {o['part']} from {o['supplier']} ${o['unit_price']:.0f}, {o['lead_days']}d: "
                f"{_order_status_line(o)}. {o['reason']}") if o else "No such order."
    if "history" in t or "order" in t:
        return _order_history()
    part = _mentioned_part(t)
    if part:
        row = next((r for r in store.latest_health() if r["part"] == part), None)
        return f"{part} is at {row['health']:.0f}% health." if row else f"No data for {part} yet."
    return _status_text()


@chat_proto.on_message(model=ChatMessage)
async def handle_chat_message(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(acknowledged_msg_id=msg.msg_id))
    q = " ".join(c.text for c in msg.content if isinstance(c, TextContent))
    if not q.strip():
        return
    content = [TextContent(text=answer_chat(q))]
    inc_id = convo.get("incident_id")
    if inc_id is not None:   # read-only status card, no buttons that change anything
        a = alerts[inc_id]
        card = {"title": f"Incident #{inc_id}: {a.part}", "summary_rows": [
            {"label": "Health", "value": f"{a.health:.0f}% ({a.state})"},
            {"label": "Biggest changes", "value": _evidence(a)},
            {"label": "Step", "value": convo["stage"].replace("_", " ")},
        ]}
        content.append(MetadataContent(metadata={
            "card_protocol_version": "1", "requires_card_interaction": "false", "card_kind": "detail",
            "card_payload": json.dumps(card), "preferred_drawer_width_px": "480",
        }))
    await ctx.send(sender, ChatMessage(content=content))


@chat_proto.on_message(model=ChatAcknowledgement)
async def handle_chat_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


agent.include(chat_proto, publish_manifest=True)

if __name__ == "__main__":
    agent.run()
