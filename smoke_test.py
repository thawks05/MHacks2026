"""Run AFTER publishing the module:  python smoke_test.py
Writes two health reports + one order that needs approval, approves it, and prints what's on the whiteboard."""
import time
from store import Store
from contracts import PartHealth, OrderProposal

s = Store()                      # defaults: http://localhost:3000, database name "pdm"
s.reset_demo()
now = time.time()
s.report_health(PartHealth("drive_gear", 100.0, 80, 126, None, now))
s.report_health(PartHealth("drive_gear", 46.4, 80, 126, 1.8, now + 5))
s.propose_order(OrderProposal("drive_gear", "PrecisionMesh", 1, 58.0, 1, "99% on-time, $58/unit, 1d lead", "needs_approval", now + 6))
print("part_health:", s.latest_health())
print("pending:", s.pending_orders())
order_id = s.pending_orders()[0]["id"]   # use the real id (auto-increment ids are not guaranteed to start at 1)
s.set_order_status(order_id, "approved", "Jose (smoke test)", "imessage")  # simulates what Photon does
print("after approve:", s.all_orders())
