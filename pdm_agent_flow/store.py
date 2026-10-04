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

    def report_pod_health(self, ph: PartHealth, state: str):
        # Like report_health, but `state` comes from pipeline.py's hold rule (watch/degraded/critical).
        self.call("report_pod_health", ph.part, float(ph.health), float(ph.drift_lo_hz), float(ph.drift_hi_hz),
                  -1.0 if ph.eta_s is None else float(ph.eta_s), ph.updated_at or time.time(), state)

    def propose_order(self, o: OrderProposal):
        self.call("propose_order", o.part, o.supplier, int(o.qty), float(o.unit_price), int(o.lead_days),
                  o.reason, o.status, o.updated_at or time.time(), o.product_url)

    def set_order_status(self, order_id: int, status: str, approved_by: str, channel: str):
        # "approved" or "rejected". channel MUST be "imessage" - the reducer rejects anything else.
        # No Python agent should ever call this for real; it exists for store.py completeness and
        # for smoke_test.py to exercise the reducer's own contract honestly.
        self.call("set_order_status", int(order_id), status, time.time(), approved_by, channel)

    def write_spectrum_reading(self, part: str, bands_json: str, z_scores_json: str, updated_at: float = None):
        self.call("write_spectrum_reading", part, bands_json, z_scores_json, updated_at or time.time())

    def open_observation_request(self, part: str, state: str, health: float, eta_s: float):
        self.call("open_observation_request", part, state, float(health), float(eta_s), time.time())

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

    def approved_imessage_orders(self):
        # What the Buyer polls for - the ONLY way an order's payment handshake ever starts, since
        # Python can never call set_order_status itself (only decide_order_from_text, backed by a real operator text, can approve).
        return self.rows("SELECT * FROM order_proposal WHERE status = 'approved' AND channel = 'imessage'")

    def open_observation_request_for(self, part: str):
        rows = self.rows(f"SELECT * FROM observation_request WHERE part = '{part}' AND status != 'complete'")
        return rows[0] if rows else None

    def latest_observation_request_for(self, part: str):
        rows = self.rows(f"SELECT * FROM observation_request WHERE part = '{part}'")
        return max(rows, key=lambda r: r["id"]) if rows else None

    def human_observations_for(self, observation_request_id: int):
        rows = self.rows(f"SELECT * FROM human_observation WHERE observation_request_id = {int(observation_request_id)}")
        return sorted(rows, key=lambda r: r["id"])

    def write_diagnosis(self, part: str, observation_request_id: int, likely_cause: str,
                         confidence: float, evidence_json: str, recommended_action: str):
        self.call("write_diagnosis", part, int(observation_request_id), likely_cause,
                  float(confidence), evidence_json, recommended_action, time.time())

    def create_rfq(self, part: str, max_price: float, max_wait_days: float, priority: str) -> int:
        self.call("create_rfq", part, float(max_price), float(max_wait_days), priority, time.time())
        matches = [r for r in self.rows(f"SELECT * FROM rfq WHERE part = '{part}' AND status = 'collecting'")]
        return max(matches, key=lambda r: r["id"])["id"]

    def write_supplier_quote(self, rfq_id: int, supplier: str, available: bool, unit_price: float,
                              lead_days: int, shipping_cost: float, counter_note: str, substitute_part: str):
        self.call("write_supplier_quote", int(rfq_id), supplier, bool(available), float(unit_price),
                  int(lead_days), float(shipping_cost), counter_note, substitute_part, time.time())

    def complete_rfq(self, rfq_id: int, status: str):
        self.call("complete_rfq", int(rfq_id), status, time.time())

    def supplier_quotes_for(self, rfq_id: int):
        return self.rows(f"SELECT * FROM supplier_quote WHERE rfq_id = {int(rfq_id)}")

    def create_escalation(self, rfq_id: int, order_id: int, supplier: str, kind: str,
                           offer_text: str, options_json: str):
        self.call("create_escalation", int(rfq_id), int(order_id), supplier, kind,
                  offer_text, options_json, time.time())

    def open_escalations_for_rfq(self, rfq_id: int):
        return self.rows(f"SELECT * FROM escalation WHERE rfq_id = {int(rfq_id)} AND status = 'open'")

    def answered_escalations(self):
        return self.rows("SELECT * FROM escalation WHERE status = 'answered'")

    def heartbeat(self, agent_name: str, note: str = ""):
        self.call("heartbeat", agent_name, note, time.time())

    def log_event(self, agent: str, event_type: str, detail: str = "", part: str = "",
                   order_id: int = -1, rfq_id: int = -1):
        self.call("log_event", agent, event_type, part, int(order_id), int(rfq_id), detail, time.time())

    # ------------------------------------------------------------------
    # Incidents, phone channel, pod mode (tables in spacetimedb/incident_tables.ts)
    # ------------------------------------------------------------------

    @staticmethod
    def _q(text: str) -> str:   # SQL string literal escaping for values we put in WHERE clauses
        return "'" + str(text).replace("'", "''") + "'"

    def open_incident(self, part: str, state: str, health: float) -> int:
        """Idempotent: opens a new incident for this part, or updates the open one (state/worst health).
        Returns the incident id."""
        self.call("open_incident", part, state, float(health), time.time())
        return self.open_incident_for(part)["id"]

    def open_incident_for(self, part: str):
        rows = self.rows(f"SELECT * FROM incident WHERE part = {self._q(part)} AND status = 'open'")
        return rows[0] if rows else None

    def incident(self, incident_id: int):
        rows = self.rows(f"SELECT * FROM incident WHERE id = {int(incident_id)}")
        return rows[0] if rows else None

    def open_incidents(self):
        return self.rows("SELECT * FROM incident WHERE status = 'open'")

    def resolve_incident(self, incident_id: int, inbound_id: int, resolution: str):
        # The reducer refuses unless inbound_id is a real text from the operator - the ONLY way to resolve.
        self.call("resolve_incident", int(incident_id), int(inbound_id), resolution, time.time())

    def link_incident(self, incident_id: int, rfq_id: int = -1, order_id: int = -1):
        self.call("link_incident", int(incident_id), int(rfq_id), int(order_id))

    def add_operator_note(self, incident_id: int, inbound_id: int, text: str):
        self.call("add_operator_note", int(incident_id), int(inbound_id), text, time.time())

    def operator_notes_for(self, incident_id: int):
        rows = self.rows(f"SELECT * FROM operator_note WHERE incident_id = {int(incident_id)}")
        return sorted(rows, key=lambda r: r["id"])

    def queue_text(self, text: str):
        """Analyst -> operator's phone. Only the Analyst calls this; the Photon bridge sends it."""
        self.call("queue_text", text, time.time())

    def unhandled_texts(self):
        rows = self.rows("SELECT * FROM inbound_message WHERE handled = false")
        return sorted(rows, key=lambda r: r["id"])

    def mark_text_handled(self, inbound_id: int):
        self.call("mark_text_handled", int(inbound_id))

    def decide_order_from_text(self, order_id: int, inbound_id: int, status: str):
        # "approved" | "rejected". The reducer checks inbound_id is a real operator text that says
        # yes (for approved) / no (for rejected) before it touches the order.
        self.call("decide_order_from_text", int(order_id), int(inbound_id), status, time.time())

    def set_pod_mode(self, mode: str, note: str = ""):
        # "training" | "monitoring" | "idle" (baseline aborted, e.g. motor off)
        self.call("set_pod_mode", mode, note, time.time())
