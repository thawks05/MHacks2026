"""The supplier swarm: 7 local uAgents (fixed seeds/ports, NOT on ASI:One), run together in one
process via uAgents' Bureau. Each has its own catalog entry (suppliers.SUPPLIER_CATALOG) and a
personality covering every archetype the spec names:

  always_quote       - NorthGear Co, PrecisionMesh, BulkParts Ltd: reply immediately, straight quote
  counter             - FlexDrive: if its price is above the operator's max, counters with a longer
                        lead time instead of just quoting over budget
  slow                - ShaftPro: replies ~5s later, via its own on_interval tick (never a blocking
                        sleep inside a message handler)
  out_of_stock        - SouthBearing: always reports no stock for belt specifically
  counter_substitute  - MetroSupply: offers a cheaper substitute-part note for drive_gear

Each still independently plays the Payment Protocol SELLER role for whichever order it wins -
same pattern as the original single supplier_agent.py, just parametrized per instance."""
import time

from uagents import Agent, Bureau, Context, Protocol
from uagents_core.contrib.protocols.payment import (
    CommitPayment, CompletePayment, Funds, RejectPayment, RequestPayment, payment_protocol_spec,
)

from suppliers import SUPPLIER_CATALOG
from messages import SUPPLIER_CONFIGS, RFQRequest, RFQQuote, PurchaseIntent

PLATFORM_FEE_PCT = 0.05  # referral fee the platform would take in a real deployment - logged only
SLOW_DELAY_S = 5.0

# Shared across all 7 agents in this one process - each entry tagged with which supplier it's for.
# (send_at, rfq_id, part, max_price, max_wait_days, sender, supplier_name)
_delayed_replies: list[tuple] = []


def _build_quote(name: str, personality: str, part: str, max_price: float, max_wait_days: float) -> dict:
    cat = SUPPLIER_CATALOG[name][part]
    if not cat["in_stock"]:
        return {"supplier": name, "available": False, "unit_price": 0.0, "lead_days": 0,
                "shipping_cost": 0.0, "counter_note": "", "substitute_part": ""}
    price, lead_days, shipping = cat["price"], cat["lead_days"], cat["shipping"]
    counter_note, substitute_part = "", ""
    if personality == "counter" and price > max_price:
        new_lead = lead_days + 2
        counter_note = f"Can match ${max_price:.0f} if you accept {new_lead}d lead instead of {lead_days}d"
        price, lead_days = max_price, new_lead
    elif personality == "counter_substitute" and part == "drive_gear":
        price = round(price * 0.85, 2)
        substitute_part = "compatible aftermarket gear (not an exact OEM match)"
    return {"supplier": name, "available": True, "unit_price": price, "lead_days": lead_days,
            "shipping_cost": shipping, "counter_note": counter_note, "substitute_part": substitute_part}


def make_supplier(cfg: dict) -> Agent:
    # port/endpoint are NOT set per-agent here - uAgents' Bureau overwrites each member agent's
    # endpoints with its own single shared one (confirmed by reading Bureau._update_agent), so all
    # 7 agents are actually reachable at the Bureau's one endpoint below, routed by their own
    # distinct addresses (still derived from each agent's own seed).
    name, seed, personality = cfg["name"], cfg["seed"], cfg["personality"]
    agent = Agent(name=name.replace(" ", "_").replace(".", ""), seed=seed)
    payment_proto = Protocol(spec=payment_protocol_spec, role="seller")
    _pending: dict[str, PurchaseIntent] = {}

    @agent.on_message(model=RFQRequest)
    async def handle_rfq(ctx: Context, sender: str, msg: RFQRequest):
        if msg.part not in SUPPLIER_CATALOG[name]:
            return
        if personality == "slow":
            _delayed_replies.append((time.time() + SLOW_DELAY_S, msg.rfq_id, msg.part,
                                      msg.max_price, msg.max_wait_days, sender, name))
            return
        quote = _build_quote(name, personality, msg.part, msg.max_price, msg.max_wait_days)
        await ctx.send(sender, RFQQuote(rfq_id=msg.rfq_id, **quote))

    @agent.on_interval(period=1.0)
    async def send_delayed(ctx: Context):
        now = time.time()
        due = [r for r in _delayed_replies if r[0] <= now and r[6] == name]
        for r in due:
            _delayed_replies.remove(r)
            _, rfq_id, part, max_price, max_wait_days, sender, _ = r
            quote = _build_quote(name, personality, part, max_price, max_wait_days)
            await ctx.send(sender, RFQQuote(rfq_id=rfq_id, **quote))

    @agent.on_message(model=PurchaseIntent)
    async def handle_purchase_intent(ctx: Context, sender: str, msg: PurchaseIntent):
        total = round(msg.unit_price * msg.qty, 2)
        reference = str(msg.order_id)
        _pending[reference] = msg
        fee = round(total * PLATFORM_FEE_PCT, 2)
        ctx.logger.info(f"{name}: order #{msg.order_id} requesting ${total} (platform fee would be ${fee}, simulated)")
        await ctx.send(sender, RequestPayment(
            accepted_funds=[Funds(currency="USD", amount=str(total), payment_method="mock")],
            recipient=str(agent.address), deadline_seconds=60, reference=reference,
            description=f"{msg.qty}x {msg.part} from {name} (SIMULATED - no real payment)",
            metadata={"order_id": str(msg.order_id)},
        ))

    @payment_proto.on_message(CommitPayment)
    async def handle_commit(ctx: Context, sender: str, msg: CommitPayment):
        _pending.pop(msg.reference, None)
        ctx.logger.info(f"{name}: payment committed (txn {msg.transaction_id}) - completing (simulated)")
        await ctx.send(sender, CompletePayment(transaction_id=msg.transaction_id))

    @payment_proto.on_message(RejectPayment)
    async def handle_reject(ctx: Context, sender: str, msg: RejectPayment):
        ctx.logger.info(f"{name}: buyer rejected payment: {msg.reason}")

    agent.include(payment_proto)
    return agent


SWARM_PORT = 8100

if __name__ == "__main__":
    bureau = Bureau(port=SWARM_PORT, endpoint=f"http://127.0.0.1:{SWARM_PORT}/submit")
    for cfg in SUPPLIER_CONFIGS:
        supplier_agent = make_supplier(cfg)
        assert supplier_agent.address == cfg["address"], (
            f"{cfg['name']}: computed address {supplier_agent.address} != "
            f"messages.py's precomputed {cfg['address']} - seed changed?"
        )
        print(f"{cfg['name']:<14} personality={cfg['personality']:<18} address={supplier_agent.address}")
        bureau.add(supplier_agent)
    bureau.run()
