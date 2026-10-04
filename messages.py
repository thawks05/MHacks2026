"""uAgents message types + fixed seeds/ports for the three local agents.

These are native uAgents messages between Watcher/Analyst/Buyer - NOT a Spacetime
schema. Conversation state (what a human has answered so far) lives only in the
Analyst's memory, keyed by sender address; nothing here touches the shared database.

Seeds are fixed strings so each agent's address stays the same across restarts -
there's no account behind a seed, it's just a deterministic key derivation."""
from typing import List, Optional
from uagents import Model

WATCHER_SEED, WATCHER_PORT = "pdm-watcher-demo-seed-v1", 8002
BUYER_SEED, BUYER_PORT = "pdm-buyer-demo-seed-v1", 8003
ANALYST_SEED, ANALYST_PORT = "pdm-analyst-demo-seed-v1", 8001
SUPPLIER_SEED, SUPPLIER_PORT = "pdm-supplier-demo-seed-v1", 8004  # retired - see SUPPLIER_CONFIGS

# Precomputed from the seeds above (deterministic - recompute via Agent(seed=...).address if a seed ever changes).
WATCHER_ADDRESS = "agent1qt7xer9jf2ghz24k75z6j3ehu8ja8fy9ewt7zw5kv2dah9aq42vnwjqen49"
BUYER_ADDRESS = "agent1qgpvqu3jv4wttxp92uakyva0zkwd3tz7v2vyf6k006ym7z7825lhc0dzv99"
ANALYST_ADDRESS = "agent1q2v47xe0jy95wd9n4luvkdk79qu84fakx9jwmsc56nyjdnm6k2jvgswungh"
SUPPLIER_ADDRESS = "agent1qvedq9amu0crea7cnpy90ckgep0us3trwhr7h8juw9nhpupxu53w2l7w25e"  # retired - see SUPPLIER_CONFIGS

# The 7-agent supplier swarm (replaces the single generic SUPPLIER_ADDRESS above). Each entry's
# "address" is precomputed from its seed via Agent(seed=...).address - never guessed. personality
# is one of: always_quote | counter | slow | out_of_stock | counter_substitute.
SUPPLIER_CONFIGS = [
    {"name": "NorthGear Co",  "seed": "pdm-supplier-northgear-v1",     "port": 8101,
     "address": "agent1qg5knemtlxwt40ma0rtskv86mj7juw0g5r5qs0nkwc87d33mm048x62lczs",
     "personality": "always_quote"},
    {"name": "PrecisionMesh", "seed": "pdm-supplier-precisionmesh-v1", "port": 8102,
     "address": "agent1q0n57lc5dyflj29j8yuzr967y7wn5dckgrg3sxedqxfdcc0sjnvp79j4h9k",
     "personality": "always_quote"},
    {"name": "BulkParts Ltd", "seed": "pdm-supplier-bulkparts-v1",     "port": 8103,
     "address": "agent1qwtnm365w93r7h8haddxw98e8hvewzcfdyesjryhydhkmk5usckakctykyy",
     "personality": "always_quote"},
    {"name": "FlexDrive",     "seed": "pdm-supplier-flexdrive-v1",     "port": 8104,
     "address": "agent1qvge7t65qt54vsc5ck6hywuwdngmmyp7nxqgr2wgmej55lw9r3l8wg9vfwr",
     "personality": "counter"},
    {"name": "ShaftPro",      "seed": "pdm-supplier-shaftpro-v1",      "port": 8105,
     "address": "agent1qt5g2mdwham6eghgj9cvmpef9u7wrkmu48kzpxmaxhcyvn27vujs7eplpgf",
     "personality": "slow"},
    {"name": "SouthBearing",  "seed": "pdm-supplier-southbearing-v1",  "port": 8106,
     "address": "agent1qgmguqdfln993j5w6y497rvyskqkcm838qp87x5rp74v6zznym6j56eu3h8",
     "personality": "out_of_stock"},
    {"name": "MetroSupply",   "seed": "pdm-supplier-metrosupply-v1",   "port": 8107,
     "address": "agent1qdys6cnnz2m7g8wvfg7at6ashtdj5mtclhsndfg5ejpr3ny0xdms6uslevn",
     "personality": "counter_substitute"},
]
SUPPLIER_ADDRESSES = [c["address"] for c in SUPPLIER_CONFIGS]


class PartAlert(Model):      # Watcher -> Analyst: a part just crossed below the health threshold
    part: str
    health: float
    drift_lo_hz: float
    drift_hi_hz: float
    drift_z: float            # signed z-score of the worst-deviating band vs the healthy baseline (real number, not invented)
    eta_s: Optional[float]
    updated_at: float


class SourceRequest(Model):  # Analyst -> Buyer: go find the best supplier under these constraints
    part: str
    max_price: float
    max_wait_days: float
    priority: str             # "price" | "speed"


class SupplierOption(Model):  # one row of the comparison Buyer considered
    supplier: str
    unit_price: float
    lead_days: int
    on_time: float
    defect: float
    product_url: str
    meets_requirements: bool
    picked: bool               # True for the one Buyer recommends


class SourceResult(Model):   # Buyer -> Analyst: what it found and already wrote to Spacetime
    part: str
    supplier: str             # the picked supplier (kept for backwards compat / quick access)
    unit_price: float
    lead_days: int
    reason: str
    product_url: str
    order_id: int
    options: List[SupplierOption]   # full comparison, for the Carousel card


class RFQRequest(Model):     # Buyer -> each Supplier agent: quote this part under these constraints
    rfq_id: int
    part: str
    max_price: float
    max_wait_days: float
    priority: str


class RFQQuote(Model):       # Supplier -> Buyer: this supplier's answer for one rfq_id
    rfq_id: int
    supplier: str
    available: bool          # False = out of stock for this part, everything else is meaningless
    unit_price: float
    lead_days: int
    shipping_cost: float
    counter_note: str        # non-empty if this supplier wants to negotiate price/lead time
    substitute_part: str     # non-empty if this supplier is offering a substitute, not the exact part


class PurchaseIntent(Model):  # Buyer -> Supplier: start the Payment Protocol handshake for this order
    order_id: int
    part: str
    supplier: str
    unit_price: float
    qty: int


class PaymentCompleted(Model):  # Buyer -> Analyst: the (simulated) Payment Protocol handshake finished
    order_id: int
    part: str
    supplier: str
    unit_price: float
    qty: int
    transaction_id: str
