"""Buyer agent (Layer 3): sources via a live RFQ fan-out to the 7-agent supplier swarm
(suppliers_swarm.py), waits up to RFQ_WINDOW_S for replies (scatter-gather, not a single static
catalog lookup), and either proposes a needs_approval order or escalates a negotiation point
(counteroffer / over-budget / substitute part) to the Analyst, which texts the operator (the Analyst
is the ONLY agent that talks to the phone) and sends back an EscalationAnswer to resume.

Approval can ONLY happen from the operator's iMessage reply (the decide_order_from_text reducer
refuses unless it's pointed at a real inbound text that says yes) - so the Buyer polls Spacetime
for orders that flipped to approved with channel=imessage, and runs the (simulated) Payment
Protocol handshake with the winning Supplier for those."""
import json
import time
import uuid
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.payment import (
    CancelPayment, CommitPayment, CompletePayment, RequestPayment, payment_protocol_spec,
)

from suppliers import score_quotes, PRODUCT_URLS
from store import Store
from contracts import OrderProposal
from messages import (
    SourceRequest, SourceResult, SupplierOption, PurchaseIntent, PaymentCompleted,
    EscalationNeeded, EscalationAnswer,
    RFQRequest, RFQQuote, SUPPLIER_CONFIGS, SUPPLIER_ADDRESSES,
    BUYER_SEED, BUYER_PORT,
)

store = Store()
agent = Agent(name="buyer", seed=BUYER_SEED, port=BUYER_PORT, endpoint=[f"http://127.0.0.1:{BUYER_PORT}/submit"])
print("BUYER_ADDRESS", agent.address)

payment_proto = Protocol(spec=payment_protocol_spec, role="buyer")


@agent.on_interval(period=10.0)
async def heartbeat(ctx: Context):
    store.heartbeat("buyer")

RFQ_WINDOW_S = 8.0  # ShaftPro ("slow") replies at ~5s, so this covers it

# rfq_id -> {part, max_price, max_wait_days, priority, analyst, deadline, quotes: {supplier: dict}}
_open_rfqs: dict[int, dict] = {}
# rfq_id -> analyst address, kept even after _open_rfqs is cleared so an escalation resume later
# still knows who to tell
_analyst_for_rfq: dict[int, str] = {}
# rfq_ids currently paused on an open escalation, so each answer is only acted on once
_escalated_rfqs: set[int] = set()
# rfq_id -> incident_id, so every rfq/order gets linked to the incident for the dashboard
_incident_for_rfq: dict[int, int] = {}

_pending_by_reference: dict[str, dict] = {}
_pending_by_txn: dict[str, dict] = {}
_payment_started_for: set[int] = set()
_analyst_for_order: dict[int, str] = {}


def _address_for_supplier(name: str) -> str:
    return next(c["address"] for c in SUPPLIER_CONFIGS if c["name"] == name)


def _find_order_row(part: str, supplier: str) -> dict:
    pending = store.pending_orders() + store.all_orders()
    matches = [o for o in pending if o["part"] == part and o["supplier"] == supplier]
    return max(matches, key=lambda o: o["id"])


@agent.on_message(model=SourceRequest)
async def handle_request(ctx: Context, sender: str, msg: SourceRequest):
    rfq_id = store.create_rfq(msg.part, msg.max_price, msg.max_wait_days, msg.priority)
    _open_rfqs[rfq_id] = {
        "part": msg.part, "max_price": msg.max_price, "max_wait_days": msg.max_wait_days,
        "priority": msg.priority, "analyst": sender, "deadline": time.time() + RFQ_WINDOW_S,
        "quotes": {},
    }
    _analyst_for_rfq[rfq_id] = sender
    _incident_for_rfq[rfq_id] = msg.incident_id
    if msg.incident_id >= 0:
        store.link_incident(msg.incident_id, rfq_id=rfq_id)
    ctx.logger.info(f"rfq #{rfq_id} ({msg.part}): asking {len(SUPPLIER_CONFIGS)} suppliers, priority={msg.priority}")
    for addr in SUPPLIER_ADDRESSES:
        await ctx.send(addr, RFQRequest(rfq_id=rfq_id, part=msg.part, max_price=msg.max_price,
                                         max_wait_days=msg.max_wait_days, priority=msg.priority))


@agent.on_message(model=RFQQuote)
async def handle_quote(ctx: Context, sender: str, msg: RFQQuote):
    rfq = _open_rfqs.get(msg.rfq_id)
    if rfq is None:
        return  # already finalized or unknown rfq - ignore late/duplicate replies
    rfq["quotes"][msg.supplier] = {
        "supplier": msg.supplier, "available": msg.available, "unit_price": msg.unit_price,
        "lead_days": msg.lead_days, "shipping_cost": msg.shipping_cost,
        "counter_note": msg.counter_note, "substitute_part": msg.substitute_part,
    }
    store.write_supplier_quote(msg.rfq_id, msg.supplier, msg.available, msg.unit_price,
                                msg.lead_days, msg.shipping_cost, msg.counter_note, msg.substitute_part)
    ctx.logger.info(f"rfq #{msg.rfq_id}: quote from {msg.supplier} (available={msg.available})")


@agent.on_interval(period=1.0)
async def finalize_rfqs(ctx: Context):
    now = time.time()
    for rfq_id, rfq in list(_open_rfqs.items()):
        all_in = len(rfq["quotes"]) >= len(SUPPLIER_CONFIGS)
        if not all_in and now < rfq["deadline"]:
            continue
        del _open_rfqs[rfq_id]
        await _finalize(ctx, rfq_id, rfq)


@agent.on_message(model=EscalationAnswer)
async def handle_escalation_answer(ctx: Context, sender: str, msg: EscalationAnswer):
    if msg.rfq_id in _escalated_rfqs:
        await _resume_after_escalation(ctx, msg.rfq_id, {"answer": msg.answer})


async def _finalize(ctx: Context, rfq_id: int, rfq: dict):
    quotes = list(rfq["quotes"].values())
    ranked, best, needs_negotiation = score_quotes(quotes, rfq["max_price"], rfq["max_wait_days"], rfq["priority"])
    ctx.logger.info(f"rfq #{rfq_id}: {len(quotes)}/{len(SUPPLIER_CONFIGS)} replied, "
                     f"{len(ranked)} available, needs_negotiation={needs_negotiation}")

    if best is None:
        store.complete_rfq(rfq_id, "complete")
        await ctx.send(rfq["analyst"], SourceResult(
            part=rfq["part"], supplier="none", unit_price=0.0, lead_days=0,
            reason="No supplier had stock for this part.", product_url="", order_id=-1, options=[],
        ))
        return

    if needs_negotiation:
        store.complete_rfq(rfq_id, "escalated")
        _escalated_rfqs.add(rfq_id)
        kind = "substitute" if best["substitute_part"] else ("counter" if best["counter_note"] else "over_budget")
        bits = []
        if best["counter_note"]:
            bits.append(best["counter_note"])
        if best["substitute_part"]:
            bits.append(f"Substitute offered: {best['substitute_part']}")
        if not bits:
            bits.append(f"Best available is ${best['unit_price']:.0f}/unit, {best['lead_days']}d - over your limit.")
        offer_text = f"{best['supplier']}: " + " ".join(bits)
        options = ["Accept this offer", "Try the next best option instead", "Cancel sourcing for this part"]
        store.create_escalation(rfq_id, -1, best["supplier"], kind, offer_text, json.dumps(options))
        store.log_event("buyer", "escalation_created", offer_text, part=rfq["part"], rfq_id=rfq_id)
        ctx.logger.info(f"rfq #{rfq_id}: escalating - {offer_text}")
        await ctx.send(rfq["analyst"], EscalationNeeded(
            rfq_id=rfq_id, part=rfq["part"], supplier=best["supplier"], kind=kind, offer_text=offer_text,
        ))
        return

    store.complete_rfq(rfq_id, "complete")
    await _propose(ctx, rfq_id, rfq, best, ranked)


async def _propose(ctx: Context, rfq_id: int, rfq: dict, best: dict, ranked: list):
    part = rfq["part"]
    product_url = PRODUCT_URLS[part]
    reason = (f"${best['unit_price']:.0f}/unit, {best['lead_days']}d lead - best fit for "
              f"{rfq['priority']} among {len(ranked)} available of {len(SUPPLIER_CONFIGS)} suppliers asked.")
    order = OrderProposal(part=part, supplier=best["supplier"], qty=1, unit_price=best["unit_price"],
                           lead_days=best["lead_days"], reason=reason, status="needs_approval",
                           updated_at=time.time(), product_url=product_url)
    store.propose_order(order)
    order_id = _find_order_row(part, best["supplier"])["id"]
    _analyst_for_order[order_id] = rfq["analyst"]
    incident_id = _incident_for_rfq.get(rfq_id, -1)
    if incident_id >= 0:
        store.link_incident(incident_id, order_id=order_id)
    store.log_event("buyer", "order_proposed", reason, part=part, order_id=order_id, rfq_id=rfq_id)

    all_quotes = store.supplier_quotes_for(rfq_id)
    options = [SupplierOption(
        supplier=q["supplier"], unit_price=q["unit_price"], lead_days=q["lead_days"],
        on_time=0.95, defect=0.02, product_url=product_url,
        meets_requirements=q["available"] and q["unit_price"] <= rfq["max_price"] and q["lead_days"] <= rfq["max_wait_days"],
        picked=q["supplier"] == best["supplier"],
    ) for q in all_quotes]

    await ctx.send(rfq["analyst"], SourceResult(
        part=part, supplier=best["supplier"], unit_price=best["unit_price"], lead_days=best["lead_days"],
        reason=reason, product_url=product_url, order_id=order_id, options=options,
    ))


async def _resume_after_escalation(ctx: Context, rfq_id: int, ans: dict):
    _escalated_rfqs.discard(rfq_id)
    analyst = _analyst_for_rfq.get(rfq_id)
    rfq_rows = store.rows(f"SELECT * FROM rfq WHERE id = {rfq_id}")
    if not rfq_rows or analyst is None:
        ctx.logger.error(f"rfq #{rfq_id}: can't resume - missing rfq row or analyst address")
        return
    rfq_row = rfq_rows[0]
    part, max_price, max_wait_days, priority = (
        rfq_row["part"], rfq_row["max_price"], rfq_row["max_wait_days"], rfq_row["priority"]
    )
    quotes = store.supplier_quotes_for(rfq_id)
    quote_dicts = [{"supplier": q["supplier"], "available": q["available"], "unit_price": q["unit_price"],
                     "lead_days": q["lead_days"], "shipping_cost": q["shipping_cost"],
                     "counter_note": q["counter_note"], "substitute_part": q["substitute_part"]} for q in quotes]
    ranked, best, _ = score_quotes(quote_dicts, max_price, max_wait_days, priority)
    rfq_dict = {"part": part, "max_price": max_price, "max_wait_days": max_wait_days,
                "priority": priority, "analyst": analyst}

    answer = ans["answer"].lower()
    ctx.logger.info(f"rfq #{rfq_id}: escalation answered ({answer!r}) - resuming")

    if best is None or "cancel" in answer:
        if best is None:
            await ctx.send(analyst, SourceResult(part=part, supplier="none", unit_price=0.0, lead_days=0,
                                                  reason="No supplier had stock.", product_url="", order_id=-1, options=[]))
        return

    if "accept" in answer:
        await _propose(ctx, rfq_id, rfq_dict, best, ranked)
        return

    # anything else ("reject"/"next"/"no") -> try the next-best option excluding this supplier
    remaining = [q for q in quote_dicts if q["supplier"] != best["supplier"]]
    ranked2, best2, needs_negotiation2 = score_quotes(remaining, max_price, max_wait_days, priority)
    if best2 is None or needs_negotiation2:
        await ctx.send(analyst, SourceResult(
            part=part, supplier="none", unit_price=0.0, lead_days=0,
            reason="No other clean option available - sourcing cancelled for this part.",
            product_url="", order_id=-1, options=[],
        ))
        return
    await _propose(ctx, rfq_id, rfq_dict, best2, ranked2)


@agent.on_interval(period=3.0)
async def poll_for_approvals(ctx: Context):
    for o in store.approved_imessage_orders():
        order_id = o["id"]
        if order_id in _payment_started_for:
            continue
        _payment_started_for.add(order_id)
        ctx.logger.info(f"order #{order_id} approved on iMessage by {o['approved_by']!r} - starting payment handshake")
        await _start_payment(ctx, order_id, o["part"], o["supplier"], o["unit_price"], o["qty"])


async def _start_payment(ctx: Context, order_id: int, part: str, supplier: str, unit_price: float, qty: int):
    _pending_by_reference[str(order_id)] = {"order_id": order_id, "part": part, "supplier": supplier,
                                             "unit_price": unit_price, "qty": qty}
    await ctx.send(_address_for_supplier(supplier), PurchaseIntent(
        order_id=order_id, part=part, supplier=supplier, unit_price=unit_price, qty=qty,
    ))


@payment_proto.on_message(RequestPayment)
async def handle_request_payment(ctx: Context, sender: str, msg: RequestPayment):
    info = _pending_by_reference.pop(msg.reference, None)
    if info is None:
        ctx.logger.warning(f"RequestPayment for unknown reference {msg.reference!r}")
        return
    ctx.logger.info(f"got payment request: {msg.description}")
    txn_id = str(uuid.uuid4())
    _pending_by_txn[txn_id] = info
    funds = msg.accepted_funds[0]
    await ctx.send(sender, CommitPayment(
        funds=funds, recipient=msg.recipient, transaction_id=txn_id,
        reference=msg.reference, description="simulated commit - no real payment",
    ))


@payment_proto.on_message(CompletePayment)
async def handle_complete_payment(ctx: Context, sender: str, msg: CompletePayment):
    info = _pending_by_txn.pop(msg.transaction_id, None)
    if info is None:
        ctx.logger.warning(f"CompletePayment for unknown transaction {msg.transaction_id}")
        return
    order_id = info["order_id"]
    ctx.logger.info(f"payment complete for order #{order_id} (txn {msg.transaction_id}, simulated)")
    store.log_event("buyer", "payment_completed", f"txn {msg.transaction_id}", order_id=order_id)
    analyst = _analyst_for_order.get(order_id)
    if analyst:
        await ctx.send(analyst, PaymentCompleted(
            order_id=order_id, part=info["part"], supplier=info["supplier"],
            unit_price=info["unit_price"], qty=info["qty"], transaction_id=msg.transaction_id,
        ))


@payment_proto.on_message(CancelPayment)
async def handle_cancel_payment(ctx: Context, sender: str, msg: CancelPayment):
    info = _pending_by_txn.pop(msg.transaction_id, None)
    ctx.logger.warning(f"supplier cancelled payment (txn {msg.transaction_id}): {msg.reason}")
    if info:
        ctx.logger.error(f"TODO: order #{info['order_id']} needs a human told about this cancellation - not wired up yet")


agent.include(payment_proto)

if __name__ == "__main__":
    agent.run()
