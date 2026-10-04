"""Shared message contracts between agents. AGREE ON THESE FIRST, then both teammates code against them.
Plain dataclasses on purpose: they port 1:1 to Fetch uAgents `Model` classes and to Spacetime table columns (same field names).
Do not rename or add fields without telling the group."""
from dataclasses import dataclass, asdict
import json

@dataclass
class PartHealth:            # watcher/analyst agents -> procurement agent + dashboard
    part: str                # e.g. "drive_gear"
    health: float            # 0-100, 100 = matches the healthy baseline
    drift_lo_hz: float       # low end of the frequency band that drifted most
    drift_hi_hz: float       # high end of that band
    eta_s: float | None      # rough seconds until health crosses the replace threshold (an ESTIMATE), None if not trending
    updated_at: float = 0.0  # unix seconds; lets the dashboard show what just changed
    def to_json(self): return json.dumps(asdict(self))

@dataclass
class OrderProposal:         # procurement agent -> dashboard / Photon (iMessage) / human
    part: str
    supplier: str
    qty: int
    unit_price: float        # MOCK catalog data
    lead_days: int
    reason: str              # plain-language why this supplier
    status: str              # "needs_approval" | "approved" | "rejected" - no auto_approved, ever
    updated_at: float = 0.0  # unix seconds
    product_url: str = ""    # MOCK link shown to the human before they approve (e.g. a fake Amazon product page)
    def to_json(self): return json.dumps(asdict(self))
