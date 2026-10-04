"""Procurement data + scoring. ALL DATA BELOW IS MOCK - say so in the pitch.

Flow: average(part) quotes the user a reference price/lead time for a part BEFORE asking
anything. The Buyer then fans an RFQ out to the 7-agent supplier swarm (messages.py's
SUPPLIER_CONFIGS) and collects real RFQQuote replies; score_quotes() picks the best fit from
whatever actually came back, never from this static catalog directly - SUPPLIER_CATALOG below is
only each mock supplier's own seed data for building its quote, not something the Buyer reads.

Policy: there is NO auto-approval, at any price. Every order always needs an explicit human
approval, which is only ever possible through the Photon/iMessage bridge - enforced at the
Spacetime reducer level (set_order_status requires channel=imessage), not just here.

product_url: the supplier names/prices/lead-times below are still MOCK, but each URL is a REAL,
working McMaster-Carr category page for that general part type (verified to load) - the same real
link for every mock supplier of a part, not a distinct real listing per fictional supplier. Once
real sensor data tells us the exact part, a real per-supplier search would replace this."""

PRODUCT_URLS = {
    "drive_gear": "https://www.mcmaster.com/products/drive-gears/",
    "belt": "https://www.mcmaster.com/products/treaded-conveyor-belts/",
    "motor_shaft": "https://www.mcmaster.com/products/connecting-shafts/",
}

# Each supplier's own seed numbers for building its RFQQuote reply (see suppliers_swarm.py).
# in_stock=False means that supplier always reports out of stock for that part (SouthBearing/belt).
SUPPLIER_CATALOG = {
    "NorthGear Co":  {"drive_gear": {"price": 42.0, "lead_days": 2, "shipping": 5.0, "in_stock": True},
                       "belt":        {"price": 24.0, "lead_days": 3, "shipping": 4.0, "in_stock": True},
                       "motor_shaft": {"price": 65.0, "lead_days": 4, "shipping": 6.0, "in_stock": True}},
    "PrecisionMesh": {"drive_gear": {"price": 58.0, "lead_days": 1, "shipping": 8.0, "in_stock": True},
                       "belt":        {"price": 29.0, "lead_days": 1, "shipping": 7.0, "in_stock": True},
                       "motor_shaft": {"price": 79.0, "lead_days": 1, "shipping": 9.0, "in_stock": True}},
    "BulkParts Ltd": {"drive_gear": {"price": 31.0, "lead_days": 7, "shipping": 2.0, "in_stock": True},
                       "belt":        {"price": 19.0, "lead_days": 6, "shipping": 2.0, "in_stock": True},
                       "motor_shaft": {"price": 52.0, "lead_days": 8, "shipping": 3.0, "in_stock": True}},
    "FlexDrive":     {"drive_gear": {"price": 47.0, "lead_days": 3, "shipping": 5.0, "in_stock": True},
                       "belt":        {"price": 22.0, "lead_days": 4, "shipping": 4.0, "in_stock": True},
                       "motor_shaft": {"price": 70.0, "lead_days": 5, "shipping": 5.0, "in_stock": True}},
    "ShaftPro":      {"drive_gear": {"price": 50.0, "lead_days": 3, "shipping": 5.0, "in_stock": True},
                       "belt":        {"price": 26.0, "lead_days": 4, "shipping": 4.0, "in_stock": True},
                       "motor_shaft": {"price": 65.0, "lead_days": 4, "shipping": 5.0, "in_stock": True}},
    "SouthBearing":  {"drive_gear": {"price": 45.0, "lead_days": 3, "shipping": 5.0, "in_stock": True},
                       "belt":        {"price": 21.0, "lead_days": 4, "shipping": 4.0, "in_stock": False},
                       "motor_shaft": {"price": 68.0, "lead_days": 3, "shipping": 5.0, "in_stock": True}},
    "MetroSupply":   {"drive_gear": {"price": 40.0, "lead_days": 4, "shipping": 4.0, "in_stock": True},
                       "belt":        {"price": 20.0, "lead_days": 5, "shipping": 4.0, "in_stock": True},
                       "motor_shaft": {"price": 60.0, "lead_days": 5, "shipping": 4.0, "in_stock": True}},
}


def average(part):
    """What the Analyst quotes BEFORE asking the human anything: avg/min/max price and lead time
    across every supplier that stocks this part, and how many there are."""
    opts = [c[part] for c in SUPPLIER_CATALOG.values() if c[part]["in_stock"]]
    prices = [o["price"] for o in opts]
    leads = [o["lead_days"] for o in opts]
    return {
        "count": len(opts),
        "avg_price": sum(prices) / len(opts), "min_price": min(prices), "max_price": max(prices),
        "avg_lead_days": sum(leads) / len(opts), "min_lead_days": min(leads), "max_lead_days": max(leads),
    }


def score_quotes(quotes: list[dict], max_price: float, max_wait_days: float, priority: str):
    """quotes: dicts with supplier/available/unit_price/lead_days/shipping_cost/counter_note/
    substitute_part (the live RFQQuote replies the Buyer collected - never the static catalog).

    Returns (ranked, best, needs_negotiation):
      ranked - every AVAILABLE quote, best-first (clean fits ranked first if any exist, otherwise
                every available quote including ones that need negotiation)
      best   - ranked[0], or None if nobody had stock
      needs_negotiation - True if `best` has a counter_note/substitute_part, or is over the
                user's own max_price/max_wait_days - the Buyer should escalate instead of ordering
    """
    available = [q for q in quotes if q["available"]]
    if not available:
        return [], None, False
    clean = [q for q in available if not q["counter_note"] and not q["substitute_part"]
             and q["unit_price"] <= max_price and q["lead_days"] <= max_wait_days]
    pool = clean if clean else available
    key = (lambda q: (q["lead_days"], q["unit_price"])) if priority == "speed" else (lambda q: (q["unit_price"], q["lead_days"]))
    ranked = sorted(pool, key=key)
    best = ranked[0]
    needs_negotiation = bool(
        best["counter_note"] or best["substitute_part"]
        or best["unit_price"] > max_price or best["lead_days"] > max_wait_days
    )
    return ranked, best, needs_negotiation
