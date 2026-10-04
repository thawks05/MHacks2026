"""Analyst agent (Layer 2): diagnoses, and is the front door. Receives a PartAlert from the
Watcher, and the next time a human talks to it (via ASI:One chat) walks them through: here's
what's wrong and why (citing operator reports if any exist yet) -> pick an option (some need no
purchase at all) -> if a purchase is needed, what's your max price/wait/priority -> Buyer sources
it -> shows the full comparison -> order is proposed as needs_approval.

ASI:One is READ-ONLY on approval. There is no code path anywhere in this file that can approve,
reject, or cancel an order - that is only ever possible through Photon/iMessage, enforced at the
Spacetime reducer level too (set_order_status requires channel=imessage). If asked to approve
here, the Analyst says so plainly instead of pretending to.

Architecture note (the "one shared brain" requirement): all the actual conversation logic lives in
handle_turn(), which returns a plain list of {"text", "card_kind", "card_payload"} dicts - it does
NOT build ChatMessage/MetadataContent objects itself. The ASI:One chat_proto handler below converts
that list into real ChatMessages with Interactive Cards. Photon (built separately) calls the same
underlying Spacetime tables/tools, never duplicating this logic - same brain, two front doors.

All conversation state lives in the `sessions` dict below, keyed by the sender's id (an ASI:One
agent address) - nothing here writes to Spacetime except propose_order/write_diagnosis (never
set_order_status - see above)."""
import json
import re
import time
from datetime import datetime
from typing import Optional

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, EndSessionContent, MetadataContent, TextContent, chat_protocol_spec,
)

from store import Store
from suppliers import average
from messages import (
    PartAlert, SourceRequest, SourceResult, PaymentCompleted,
    ANALYST_SEED, ANALYST_PORT, BUYER_ADDRESS,
)

store = Store()
PARTS = ["drive_gear", "belt", "motor_shaft"]

# One pending failure at a time, for this demo - set by the Watcher, cleared once resolved.
active_alert: Optional[PartAlert] = None

# order_id -> the sender address that sourced it, so a later PaymentCompleted push and "what's the
# status of my order" questions both work even after the session has gone back to idle.
_order_owner: dict[int, str] = {}

# Per-human conversation state. stage is one of:
#   None              -> nothing in progress, answer free-form questions from Spacetime
#   "choosing_option" -> showing the diagnosis + options carousel, waiting for a pick
#   "ask_price"        -> waiting for their max price (or a Form card submission with all 3 at once)
#   "ask_wait"         -> waiting for their max wait (days)
#   "ask_priority"     -> waiting for "price" or "speed"
#   "awaiting_buyer"   -> sent SourceRequest, waiting for the Buyer's SourceResult
#   "choosing"         -> showing the supplier Carousel, waiting for a pick (or anything = recommended)
sessions: dict[str, dict] = {}

agent = Agent(name="analyst", seed=ANALYST_SEED, port=ANALYST_PORT,
              mailbox=True, publish_agent_details=True)
print("ANALYST_ADDRESS", agent.address)

chat_proto = Protocol(spec=chat_protocol_spec)


@agent.on_interval(period=10.0)
async def heartbeat(ctx: Context):
    store.heartbeat("analyst")


# ---------------------------------------------------------------------------
# Interactive Card payloads (schema verified against innovationlab.fetch.ai docs + our installed
# uagents_core.contrib.protocols.chat.MetadataContent). Photon ignores these - text-only there.
# ---------------------------------------------------------------------------

def _detail_card(alert: PartAlert) -> dict:
    rows = [
        {"label": "Health", "value": f"{alert.health:.0f}%"},
        {"label": "Drifting band", "value": f"{alert.drift_lo_hz:.0f}-{alert.drift_hi_hz:.0f} Hz"},
        {"label": "Deviation", "value": f"{alert.drift_z:+.1f} std dev from the healthy baseline"},
        {"label": "Est. time to failure", "value": f"~{alert.eta_s:.0f}s (estimate)" if alert.eta_s else "n/a"},
    ]
    return {"title": f"{alert.part} - health alert", "summary_rows": rows}


def _diagnosis_card(diagnosis: dict) -> dict:
    rows = [
        {"label": "Likely cause", "value": diagnosis["likely_cause"]},
        {"label": "Confidence", "value": f"{diagnosis['confidence'] * 100:.0f}%"},
        {"label": "Evidence", "value": "; ".join(diagnosis["evidence"])},
    ]
    return {"title": f"Diagnosis - {diagnosis['part']}", "summary_rows": rows}


def _options_carousel_card(part: str, options: list[dict]) -> dict:
    items = [{
        "id": o["id"], "title": o["label"],
        "subtitle": o["detail"],
        "badges": [] if o["needs_purchase"] else [{"label": "No purchase needed", "variant": "success"}],
        "primary_cta": {"label": "Choose", "selection": {"action": "select_option", "option": o["id"]}},
    } for o in options]
    return {"title": f"Options for {part}", "items": items}


def _form_card(part: str, avg: dict) -> dict:
    return {
        "title": f"Sourcing preferences for {part}",
        "fields": [
            {"name": "max_price", "kind": "text",
             "label": f"Max price you'll pay, in $ (avg ${avg['avg_price']:.2f})", "required": True},
            {"name": "max_wait_days", "kind": "text",
             "label": f"Max days you'll wait (avg {avg['avg_lead_days']:.1f}d)", "required": True},
            {"name": "priority", "kind": "select", "label": "What matters more?",
             "options": [{"value": "price", "label": "Price"}, {"value": "speed", "label": "Speed"}]},
        ],
        "submit_cta": {"label": "Find a supplier", "selection": {"action": "submit_preferences"}},
    }


def _carousel_card(part: str, options: list, reason: str) -> dict:
    items = []
    for o in options:
        badges = []
        if o.picked:
            badges.append({"label": "Recommended", "variant": "success"})
        if not o.meets_requirements:
            badges.append({"label": "Misses your limit", "variant": "warning"})
        items.append({
            "id": o.supplier, "title": o.supplier,
            "subtitle": f"{o.lead_days}d lead, {o.on_time * 100:.0f}% on-time",
            "badges": badges, "secondary_text": f"${o.unit_price:.0f}/unit",
            "primary_cta": {"label": "Select", "selection": {"action": "select_supplier", "supplier": o.supplier}},
        })
    return {"title": f"Suppliers for {part} (MOCK data)", "subtitle": reason, "items": items}


def _status_card(order: dict) -> dict:
    """Read-only. No approve/reject anywhere on this card, by design - see module docstring."""
    rows = [
        {"label": "Part", "value": order["part"]}, {"label": "Supplier (mock)", "value": order["supplier"]},
        {"label": "Price", "value": f"${order['unit_price']:.0f}"}, {"label": "Lead time", "value": f"{order['lead_days']}d"},
        {"label": "Status", "value": order["status"]},
    ]
    title = "Awaiting your approval on iMessage" if order["status"] == "needs_approval" else f"Order #{order['id']} - {order['status']}"
    return {"title": title, "summary_rows": rows, "ctas": [{"label": "OK", "selection": {"action": "ack"}, "primary": True}]}


# ---------------------------------------------------------------------------
# Diagnosis - combines sensor data with whatever human_observation rows exist right now.
# ---------------------------------------------------------------------------

def _build_diagnosis(alert: PartAlert) -> dict:
    part = alert.part
    obs_req = store.open_observation_request_for(part) or store.latest_observation_request_for(part)
    observations = store.human_observations_for(obs_req["id"]) if obs_req else []

    evidence = [f"sensor: {alert.drift_lo_hz:.0f}-{alert.drift_hi_hz:.0f}Hz band at "
                f"{abs(alert.drift_z):.1f} std dev ({alert.drift_z:+.1f})"]
    fields_seen = {}
    for o in observations:
        when = datetime.fromtimestamp(o["created_at"]).strftime("%H:%M")
        evidence.append(f'{o["reported_by"]} reported {o["field"]}: "{o["raw_text"]}" at {when}')
        fields_seen[o["field"]] = o["value"].lower()

    obstruction_reported = "obstruction" in fields_seen and not any(
        w in fields_seen["obstruction"] for w in ("nothing", "none", "clear")
    )
    if obstruction_reported:
        cause, confidence, recommended = "an obstruction is interfering with the mechanism", 0.7, "Clear the obstruction, no purchase needed"
    elif {"sound", "heat"} & set(fields_seen) and not all(
        "nothing" in fields_seen.get(f, "") for f in ("sound", "heat") if f in fields_seen
    ):
        cause, confidence, recommended = "worn or damaged bearing/gear surface (grinding/heat pattern reported)", 0.6, "Inspect and likely replace the part"
    elif observations:
        cause, confidence, recommended = f"{part} wear consistent with the sensor drift; operator reports don't point to a specific cause", 0.4, "Inspect and likely replace the part"
    else:
        cause, confidence, recommended = f"{part} wear consistent with the sensor drift alone - no operator reports yet", 0.3, "Inspect and likely replace the part"

    return {
        "part": part, "observation_request_id": obs_req["id"] if obs_req else -1,
        "likely_cause": cause, "confidence": confidence, "evidence": evidence,
        "recommended_action": recommended, "has_observations": bool(observations),
        "obstruction_reported": obstruction_reported,
    }


def _diagnosis_options(diagnosis: dict) -> list[dict]:
    opts = []
    if diagnosis["obstruction_reported"]:
        opts.append({"id": "clear_obstruction", "label": "Clear the obstruction", "needs_purchase": False,
                     "detail": "No replacement needed - just clear what's caught and reset."})
    opts.append({"id": "adjust_tension", "label": "Adjust tension/alignment", "needs_purchase": False,
                 "detail": "A mechanical adjustment, no part purchase."})
    opts.append({"id": "repair", "label": "Repair with a sourced part", "needs_purchase": True,
                 "detail": "Source a replacement part and repair in place."})
    opts.append({"id": "replace", "label": f"Replace the {diagnosis['part']}", "needs_purchase": True,
                 "detail": "Full replacement part, sourced and ordered."})
    return opts[:4]


# ---------------------------------------------------------------------------
# Reasoning helpers - read-only Spacetime queries, no state.
# ---------------------------------------------------------------------------

def _first_number(text: str) -> Optional[float]:
    m = re.search(r"[\d.]+", text)
    return float(m.group()) if m else None


def _priority(text: str) -> Optional[str]:
    t = text.lower()
    if any(w in t for w in ("speed", "fast", "quick", "time")):
        return "speed"
    if any(w in t for w in ("price", "cheap", "cost", "money")):
        return "price"
    return None


def _mentioned_part(text: str) -> Optional[str]:
    t = text.lower()
    for p in PARTS:
        if p in t or p.replace("_", " ") in t:
            return p
    return None


def _is_approval_attempt(text: str) -> bool:
    t = text.lower()
    return bool(re.search(r"\b(approve|reject|cancel)\b", t)) or t.strip() in ("yes", "y", "no", "n")


def _is_resend_request(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in ("resend", "remind", "nudge", "text them again", "text him again", "text her again"))


def _last_order_for(orders: list, part: str) -> Optional[dict]:
    matches = [o for o in orders if o["part"] == part]
    return max(matches, key=lambda o: o["id"]) if matches else None


def _order_status_line(o: dict) -> str:
    if o["status"] == "approved":
        when = datetime.fromtimestamp(o["updated_at"]).strftime("%H:%M")
        return f"approved on iMessage at {when} by {o['approved_by']}"
    if o["status"] == "rejected":
        return "rejected"
    if o["status"] == "needs_approval":
        return "still waiting on iMessage approval"
    return o["status"]


def _health_summary() -> str:
    rows = store.latest_health()
    if not rows:
        return "I don't have any health data yet - is the Watcher running?"
    bits = [f"{r['part']} at {r['health']:.0f}% ({r.get('state', 'unknown')})" for r in rows]
    return "Current health: " + ", ".join(bits) + "."


def _part_status(part: str) -> str:
    rows = store.latest_health()
    row = next((r for r in rows if r["part"] == part), None)
    if not row:
        return f"No data for {part} yet."
    bit = f"{part} is at {row['health']:.0f}% health ({row.get('state', 'unknown')})"
    if active_alert and active_alert.part == part:
        bit += f" (drifting band {active_alert.drift_lo_hz:.0f}-{active_alert.drift_hi_hz:.0f}Hz, {active_alert.drift_z:+.1f} std dev)"
    match = _last_order_for(store.all_orders(), part)
    if match:
        bit += f". Order #{match['id']} is {_order_status_line(match)}"
    return bit + "."


def _why_reasoning(part: Optional[str]) -> str:
    if active_alert is None or (part and active_alert.part != part):
        return "Nothing's currently flagged for that part - no diagnosis to explain right now."
    a = active_alert
    direction = "higher" if a.drift_z > 0 else "lower"
    base = (
        f"The {a.drift_lo_hz:.0f}-{a.drift_hi_hz:.0f}Hz band is reading {abs(a.drift_z):.1f} standard "
        f"deviations {direction} than the recorded healthy baseline for {a.part} - that's well outside "
        f"normal noise (|z|<=2 is considered normal), which is why health dropped to {a.health:.0f}%."
    )
    obs_req = store.open_observation_request_for(a.part) or store.latest_observation_request_for(a.part)
    observations = store.human_observations_for(obs_req["id"]) if obs_req else []
    if observations:
        cites = "; ".join(f'{o["reported_by"]} reported {o["field"]} at {datetime.fromtimestamp(o["created_at"]).strftime("%H:%M")}' for o in observations)
        return base + f" Operator also reported: {cites}."
    return base + " No operator observations have come in yet - this is from sensor data only."


def _risk_forecast() -> str:
    rows = [r for r in store.latest_health() if r.get("eta_s") is not None and r["eta_s"] != -1]
    if not rows:
        return "No part currently has a declining trend - nothing at imminent risk."
    rows.sort(key=lambda r: r["eta_s"])
    bits = [f"{r['part']} (~{r['eta_s']:.0f}s, estimate)" for r in rows[:3]]
    return "Most at-risk, soonest first: " + ", ".join(bits) + "."


def _order_history() -> str:
    orders = store.all_orders()
    if not orders:
        return "No orders yet."
    orders.sort(key=lambda o: o["id"], reverse=True)
    bits = [f"#{o['id']} {o['part']}/{o['supplier']} ${o['unit_price']:.0f} ({_order_status_line(o)})" for o in orders[:5]]
    return "Recent orders: " + "; ".join(bits) + "."


_MENTION_TIP = "Tip: start messages with @pdm-analyst so they reach me directly, not your general assistant."
_HELP_TEXT = (
    "I watch this machine's parts for early signs of failure. When one needs attention, I explain "
    "why, offer options (some need no purchase at all), and if a part needs sourcing I quote "
    "supplier options and get your price/time limits. Every purchase is simulated, and approval "
    "only ever happens through the plant operator's iMessage, never here - I'll tell you if you try "
    "to approve something in this chat. Ask me things like 'what's the health of the belt', 'why "
    "do you think it's the gear', 'what's at risk', or 'order history'. " + _MENTION_TIP
)


# ---------------------------------------------------------------------------
# The shared brain. Returns a list of {"text": str, "card_kind": str|None, "card_payload": dict|None}.
# Never builds ChatMessage/MetadataContent itself - that's the caller's job (ASI:One today, Photon later).
# ---------------------------------------------------------------------------

async def handle_turn(ctx: Context, sender: str, text: str) -> list[dict]:
    global active_alert
    session = sessions.setdefault(sender, {"stage": None})
    stage = session["stage"]
    t = text.lower()

    selection = None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "action" in parsed:
            selection = parsed
    except (json.JSONDecodeError, TypeError):
        pass

    def msg(text, card_kind=None, card_payload=None):
        return {"text": text, "card_kind": card_kind, "card_payload": card_payload}

    if stage is None:
        if active_alert is not None:
            alert, part = active_alert, active_alert.part
            diagnosis = _build_diagnosis(alert)
            options = _diagnosis_options(diagnosis)
            try:
                store.write_diagnosis(part, diagnosis["observation_request_id"], diagnosis["likely_cause"],
                                       diagnosis["confidence"], json.dumps(diagnosis["evidence"]), diagnosis["recommended_action"])
                store.log_event("analyst", "diagnosis_written", diagnosis["likely_cause"], part=part)
            except Exception as e:
                ctx.logger.error(f"write_diagnosis failed: {e}")
            session.update(stage="choosing_option", part=part, diagnosis=diagnosis, options=options)
            obs_note = "" if diagnosis["has_observations"] else " (no operator reports yet - sensor data only)"
            return [
                msg(f"{part} is failing (health {alert.health:.0f}%).", "detail", _detail_card(alert)),
                msg(f"My read: {diagnosis['likely_cause']}{obs_note}.", "detail", _diagnosis_card(diagnosis)),
                msg("Here are your options:", "carousel", _options_carousel_card(part, options)),
            ]

        if _is_approval_attempt(t):
            pending = store.pending_orders()
            if not pending:
                return [msg("There's nothing waiting for approval right now.")]
            bits = ", ".join(f"#{o['id']} ({o['part']}/{o['supplier']}, ${o['unit_price']:.0f})" for o in pending)
            return [msg(f"Approval only happens on the plant operator's iMessage, not here - I can't approve, "
                        f"reject, or cancel anything in this chat. Waiting on: {bits}.")]
        if _is_resend_request(t):
            pending = store.pending_orders()
            if not pending:
                return [msg("Nothing's waiting on approval right now, so there's nothing to resend.")]
            return [msg("Noted - the operator should get an automatic reminder on iMessage if it's been a "
                        "few minutes. I can't trigger an iMessage myself from here.")]

        if any(w in t for w in ("what do you do", "what can you", "what are you", "made to do", "purpose", "help")):
            return [msg(_HELP_TEXT)]
        if "why" in t:
            return [msg(_why_reasoning(_mentioned_part(t)))]
        if "risk" in t or "next" in t or "likely to fail" in t:
            return [msg(_risk_forecast())]
        if "history" in t or "recent order" in t or "what have you ordered" in t:
            return [msg(_order_history())]
        if "report" in t or "full status" in t or "everything" in t:
            return [msg(f"{_health_summary()} {_order_history()}")]
        order_id_match = re.search(r"order\s*#?(\d+)", t)
        if order_id_match:
            oid = int(order_id_match.group(1))
            o = next((x for x in store.all_orders() if x["id"] == oid), None)
            if not o:
                return [msg(f"No order #{oid}.")]
            return [msg(f"#{o['id']} {o['part']}/{o['supplier']} ${o['unit_price']:.0f}, {o['lead_days']}d, "
                        f"{_order_status_line(o)}. {o['reason']}")]
        part_mentioned = _mentioned_part(t)
        if part_mentioned:
            return [msg(_part_status(part_mentioned))]

        health_rows = store.latest_health()
        unhealthy = [r for r in health_rows if r["health"] < 70]
        if unhealthy:
            orders = store.all_orders()
            bits = []
            for r in unhealthy:
                match = _last_order_for(orders, r["part"])
                if match:
                    bits.append(f"{r['part']} health {r['health']:.0f}, order #{match['id']} {_order_status_line(match)}")
                else:
                    bits.append(f"{r['part']} health {r['health']:.0f}, no order yet")
            return [msg(" ".join(bits))]
        return [msg(f"{_health_summary()} Nothing needs a decision right now. ({_MENTION_TIP})")]

    if stage == "choosing_option":
        options = session.get("options", [])
        chosen = None
        if selection and selection.get("action") == "select_option":
            chosen = next((o for o in options if o["id"] == selection.get("option")), None)
        if chosen is None:
            chosen = next((o for o in options if o["id"] in t or o["label"].lower() in t), None)
        if chosen is None:
            return [msg("Which option - " + ", ".join(f'"{o["label"]}"' for o in options) + "?")]
        if not chosen["needs_purchase"]:
            sessions[sender] = {"stage": None}
            active_alert = None
            return [msg(f"Got it - {chosen['label'].lower()}. Logged, no purchase needed. Let me know if it comes back.")]
        part = session["part"]
        avg = average(part)
        session.update(stage="ask_price")
        return [msg(
            f"Found {avg['count']} suppliers: ${avg['min_price']:.0f}-${avg['max_price']:.0f}/unit "
            f"(avg ${avg['avg_price']:.2f}), {avg['min_lead_days']:.0f}-{avg['max_lead_days']:.0f}d lead "
            f"(avg {avg['avg_lead_days']:.1f}d). What's the most you're willing to pay?",
            "form", _form_card(part, avg),
        )]

    if stage == "ask_price":
        if selection and selection.get("action") == "submit_preferences" and "max_price" in selection:
            price = _first_number(str(selection.get("max_price", "")))
            wait = _first_number(str(selection.get("max_wait_days", "")))
            priority = selection.get("priority") if selection.get("priority") in ("price", "speed") else _priority(text)
            if price is not None and wait is not None and priority:
                session.update(max_price=price, max_wait_days=wait, priority=priority, stage="awaiting_buyer")
                await ctx.send(BUYER_ADDRESS, SourceRequest(part=session["part"], max_price=price,
                                                             max_wait_days=wait, priority=priority))
                return [msg("On it - researching suppliers now, one sec.")]
        price = _first_number(text)
        if price is None:
            return [msg("I didn't catch a number - what's the most you're willing to pay, in dollars?")]
        session["max_price"] = price
        session["stage"] = "ask_wait"
        return [msg("Got it. And how many days are you willing to wait for it to arrive?")]

    if stage == "ask_wait":
        wait = _first_number(text)
        if wait is None:
            return [msg("I didn't catch a number - how many days max are you willing to wait?")]
        session["max_wait_days"] = wait
        session["stage"] = "ask_priority"
        return [msg("Last one: does price or speed matter more to you here?")]

    if stage == "ask_priority":
        priority = (selection.get("priority") if selection else None) or _priority(text)
        if priority is None:
            return [msg("Sorry, price or speed - which matters more?")]
        session["priority"] = priority
        session["stage"] = "awaiting_buyer"
        await ctx.send(BUYER_ADDRESS, SourceRequest(
            part=session["part"], max_price=session["max_price"],
            max_wait_days=session["max_wait_days"], priority=priority,
        ))
        return [msg("On it - researching suppliers now, one sec.")]

    if stage == "awaiting_buyer":
        return [msg("Still researching suppliers - one sec.")]

    if stage == "choosing":
        options = session.get("options", [])
        chosen = None
        if selection and selection.get("action") == "select_supplier":
            chosen = next((o for o in options if o["supplier"] == selection.get("supplier")), None)
        if chosen is None:
            match = next((o for o in options if o["supplier"].lower() in t), None)
            chosen = match
        if chosen is None:
            chosen = next((o for o in options if o["picked"]), options[0] if options else None)
        if chosen is None:
            return [msg("Something went wrong - no supplier options on file. Let's start over.")]
        order_id = session["order_id"]
        o = next((x for x in store.all_orders() if x["id"] == order_id), None)
        sessions[sender] = {"stage": None}
        if not o:
            return [msg(f"Proposed order #{order_id}, but couldn't re-read it - check order history.")]
        return [msg(f"Proposed: {chosen['supplier']} - ${chosen['unit_price']:.0f}, {chosen['lead_days']}d lead. "
                    f"Awaiting your approval on iMessage (order #{order_id}).", "detail", _status_card(o))]

    return [msg("Something went wrong on my end - let's start over. What's going on?")]


# ---------------------------------------------------------------------------
# ASI:One Chat Protocol wiring - turns handle_turn()'s plain list into real ChatMessages/cards.
# ---------------------------------------------------------------------------

def _to_chat_message(m: dict) -> ChatMessage:
    content = [TextContent(text=m["text"])]
    if m.get("card_kind"):
        content.append(MetadataContent(metadata={
            "card_protocol_version": "1",
            "requires_card_interaction": "true",
            "card_kind": m["card_kind"],
            "card_payload": json.dumps(m["card_payload"]),
            "preferred_drawer_width_px": "480",
        }))
    return ChatMessage(content=content)


@chat_proto.on_message(model=ChatMessage)
async def handle_chat_message(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(acknowledged_msg_id=msg.msg_id))
    text = " ".join(c.text for c in msg.content if isinstance(c, TextContent))
    if not text.strip():
        return
    for reply in await handle_turn(ctx, sender, text):
        await ctx.send(sender, _to_chat_message(reply))


@chat_proto.on_message(model=ChatAcknowledgement)
async def handle_chat_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass  # nothing to do - ASI:One acknowledging a message we sent


@agent.on_message(model=PartAlert)
async def handle_alert(ctx: Context, sender: str, msg: PartAlert):
    global active_alert
    ctx.logger.info(f"ALERT received: {msg.part} health={msg.health:.0f} z={msg.drift_z:+.1f}")
    active_alert = msg


@agent.on_message(model=SourceResult)
async def handle_source_result(ctx: Context, sender: str, msg: SourceResult):
    ctx.logger.info(f"Buyer found: {msg.supplier} ${msg.unit_price}/{msg.lead_days}d (order #{msg.order_id})")
    _order_owner[msg.order_id] = sender
    for addr, session in sessions.items():
        if session.get("stage") == "awaiting_buyer" and session.get("part") == msg.part:
            options = [o.dict() for o in msg.options]
            session.update(stage="choosing", options=options, order_id=msg.order_id)
            reply = _to_chat_message({
                "text": f"Found it: {msg.supplier} - {msg.reason}",
                "card_kind": "carousel", "card_payload": _carousel_card(msg.part, msg.options, msg.reason),
            })
            await ctx.send(addr, reply)
            return


@agent.on_message(model=PaymentCompleted)
async def handle_payment_completed(ctx: Context, sender: str, msg: PaymentCompleted):
    ctx.logger.info(f"payment completed for order #{msg.order_id} (txn {msg.transaction_id})")
    addr = _order_owner.get(msg.order_id)
    if addr:
        await ctx.send(addr, _to_chat_message({
            "text": f"Ordered (simulated) - payment complete. (order #{msg.order_id})",
            "card_kind": None, "card_payload": None,
        }))


agent.include(chat_proto, publish_manifest=True)

if __name__ == "__main__":
    agent.run()
