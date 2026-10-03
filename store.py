"""Python helper for the Spacetime 'whiteboard' over its HTTP API (stdlib only, no installs).
Used by health.py / the Fetch agents to WRITE and by agents to READ. The dashboard uses the TypeScript SDK instead.
Keep writes to ~1 per second: the HTTP API is meant for simple use, not high-volume streaming."""
import json, os, time, urllib.error, urllib.request
from contracts import PartHealth, OrderProposal

class Store:
    def __init__(self, host=None, db=None):
        # Defaults to your local server. To use Maincloud, set env vars instead of editing code:
        #   $env:SPACETIME_HOST="https://maincloud.spacetimedb.com"   (PowerShell)
        self.host = (host or os.environ.get("SPACETIME_HOST", "http://localhost:3000")).rstrip("/")
        self.db = db or os.environ.get("SPACETIME_DB", "pdm")

    def _post(self, path, body, content_type="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        req = urllib.request.Request(f"{self.host}/v1/database/{self.db}/{path}", data=data,
                                     headers={"Content-Type": content_type}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                txt = r.read().decode()
                return json.loads(txt) if txt.strip() else None
        except urllib.error.HTTPError as e:   # show the server's own message (e.g. "no such order")
            raise RuntimeError(f"HTTP {e.code} from {path}: {e.read().decode(errors='replace')}") from None

    def call(self, reducer, *args):          # args go in the SAME ORDER as the reducer's fields in index.ts
        return self._post(f"call/{reducer}", list(args))

    def sql(self, query):
        return self._post("sql", query, "text/plain")

    def report_health(self, ph: PartHealth):  # eta_s None -> -1 ("no estimate") in the database
        self.call("report_health", ph.part, float(ph.health), float(ph.drift_lo_hz), float(ph.drift_hi_hz),
                  -1.0 if ph.eta_s is None else float(ph.eta_s), ph.updated_at or time.time())

    def propose_order(self, o: OrderProposal):
        self.call("propose_order", o.part, o.supplier, int(o.qty), float(o.unit_price), int(o.lead_days),
                  o.reason, o.status, o.updated_at or time.time())

    def set_order_status(self, order_id: int, status: str):   # "approved" or "rejected"
        self.call("set_order_status", int(order_id), status, time.time())

    def reset_demo(self):
        self.call("reset_demo")

    def rows(self, query):
        """Run SQL and return a clean list of dicts, e.g. [{'part': 'drive_gear', 'health': 46.4, ...}]."""
        out = []
        for res in self.sql(query) or []:
            names = [e["name"]["some"] for e in res["schema"]["elements"]]
            out += [dict(zip(names, row)) for row in res["rows"]]
        return out

    def latest_health(self):  return self.rows("SELECT * FROM part_health")
    def pending_orders(self): return self.rows("SELECT * FROM order_proposal WHERE status = 'needs_approval'")
    def all_orders(self):     return self.rows("SELECT * FROM order_proposal")
