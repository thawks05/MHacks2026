"""Supplier agent: plays the SELLER role in the Fetch.ai Payment Protocol. Represents whichever
mock supplier the Buyer picked (NorthGear, BulkParts, PrecisionMesh, etc. - all still MOCK data,
see suppliers.py). Its only job: when the Buyer wants to buy something, formally request payment,
and once the Buyer commits, complete it. Nothing here is a real payment - no money moves, ever.

Monetization note (for the README/pitch): in a real deployment, this platform would take a small
referral/placement fee (e.g. 5%) from the supplier for every completed order it brokers - the same
model real B2B procurement marketplaces use. That fee isn't wired into any numbers here, it's just
the credible business reason a real version of this Payment Protocol handshake would exist."""
import uuid

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.payment import (
    CancelPayment, CommitPayment, CompletePayment, Funds, RejectPayment, RequestPayment,
    payment_protocol_spec,
)

from messages import PurchaseIntent, SUPPLIER_SEED, SUPPLIER_PORT, BUYER_ADDRESS

PLATFORM_FEE_PCT = 0.05  # referral fee the platform would take in a real deployment - logged only

agent = Agent(name="supplier", seed=SUPPLIER_SEED, port=SUPPLIER_PORT,
              endpoint=[f"http://127.0.0.1:{SUPPLIER_PORT}/submit"])
print("SUPPLIER_ADDRESS", agent.address)

payment_proto = Protocol(spec=payment_protocol_spec, role="seller")

# order_id -> PurchaseIntent, so we know what a CommitPayment is actually for
_pending: dict[str, PurchaseIntent] = {}


@agent.on_message(model=PurchaseIntent)
async def handle_purchase_intent(ctx: Context, sender: str, msg: PurchaseIntent):
    total = round(msg.unit_price * msg.qty, 2)
    reference = str(msg.order_id)
    _pending[reference] = msg
    fee = round(total * PLATFORM_FEE_PCT, 2)
    ctx.logger.info(f"order #{msg.order_id}: requesting ${total} for {msg.qty}x {msg.part} "
                     f"from {msg.supplier} (platform fee would be ${fee}, simulated)")
    await ctx.send(sender, RequestPayment(
        accepted_funds=[Funds(currency="USD", amount=str(total), payment_method="mock")],
        recipient=str(agent.address),
        deadline_seconds=60,
        reference=reference,
        description=f"{msg.qty}x {msg.part} from {msg.supplier} (SIMULATED - no real payment)",
        metadata={"order_id": str(msg.order_id)},
    ))


@payment_proto.on_message(CommitPayment)
async def handle_commit(ctx: Context, sender: str, msg: CommitPayment):
    pending = _pending.pop(msg.reference, None)
    ctx.logger.info(f"payment committed for order #{msg.reference} (txn {msg.transaction_id}) - completing (simulated)")
    await ctx.send(sender, CompletePayment(transaction_id=msg.transaction_id))


@payment_proto.on_message(RejectPayment)
async def handle_reject(ctx: Context, sender: str, msg: RejectPayment):
    ctx.logger.info(f"buyer rejected payment: {msg.reason}")


agent.include(payment_proto)

if __name__ == "__main__":
    agent.run()
