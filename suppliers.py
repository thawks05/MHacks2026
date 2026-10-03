"""Procurement logic. ALL DATA BELOW IS MOCK - say so in the pitch. Replace/extend with real supplier history."""
CATALOG = {
    "drive_gear": [
        {"supplier": "NorthGear Co",  "price": 42.0, "lead_days": 2, "on_time": 0.97, "defect": 0.01},
        {"supplier": "BulkParts Ltd", "price": 31.0, "lead_days": 7, "on_time": 0.82, "defect": 0.05},
        {"supplier": "PrecisionMesh", "price": 58.0, "lead_days": 1, "on_time": 0.99, "defect": 0.00},
    ],
    "belt": [
        {"supplier": "BeltWorks",  "price": 24.0, "lead_days": 3, "on_time": 0.94, "defect": 0.02},
        {"supplier": "FlexDrive",  "price": 19.0, "lead_days": 6, "on_time": 0.85, "defect": 0.04},
    ],
    "motor_shaft": [
        {"supplier": "ShaftPro", "price": 65.0, "lead_days": 4, "on_time": 0.95, "defect": 0.01},
    ],
}

def rank(part, need_by_days, qty=1):
    opts = CATALOG[part]
    floor = min(o["price"] for o in opts)
    out = []
    for o in opts:
        value = o["on_time"] * (1 - o["defect"]) / (o["price"] / floor) ** 0.5
        late = o["lead_days"] > need_by_days
        out.append({**o, "qty": qty, "late": late, "score": value - (1.0 if late else 0.0),
                    "reason": f"{int(o['on_time']*100)}% on-time, {o['defect']*100:.0f}% defects, "
                              f"${o['price']:.0f}/unit, {o['lead_days']}d lead"
                              + (" - MISSES need-by date" if late else "")})
    return sorted(out, key=lambda r: r["score"], reverse=True)

def propose(part, eta_s, auto_limit=150.0, qty=1):
    need_by_days = max(1, int((eta_s or 7 * 86400) / 86400))   # failing sooner -> shorter lead time required
    best = rank(part, need_by_days, qty)[0]
    total = best["price"] * qty
    return {"part": part, "supplier": best["supplier"], "qty": qty, "unit_price": best["price"],
            "lead_days": best["lead_days"], "reason": best["reason"],
            "status": "auto_approved" if total <= auto_limit else "needs_approval"}
