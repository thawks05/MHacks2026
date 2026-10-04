"""Serve the Forge Flow dashboard and feed it live data from SpacetimeDB.

    export SPACETIME_HOST="https://maincloud.spacetimedb.com"
    export SPACETIME_DB="pdm"
    python3 dashboard_server.py            # then open http://localhost:8080

Read-only: it only runs SELECTs, never calls a reducer, so the dashboard can't approve, resolve or
change anything. The browser asks this server (same origin, so no CORS problems) and this server
asks SpacetimeDB over its HTTP SQL API, the same way store.py does. Standard library only.

  GET /api/snapshot?set=fast   pod_window, pod_status, incident, agent_status      (every ~1 s)
  GET /api/snapshot?set=slow   orders, quotes, rfqs, diagnosis, texts, events...   (every ~2.5 s)
"""
import json
import os
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST = os.environ.get("SPACETIME_HOST", "http://localhost:3000").rstrip("/")
DB = os.environ.get("SPACETIME_DB", "pdm")
PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))
HERE = os.path.dirname(os.path.abspath(__file__))

# Small tables only. health_log and spectrum_reading grow every second, so they're never queried.
QUERIES = {
    "fast": {
        "pod_window": "SELECT * FROM pod_window",
        "pod_status": "SELECT * FROM pod_status",
        "incident": "SELECT * FROM incident",
        "agent_status": "SELECT * FROM agent_status",
    },
    "slow": {
        "order_proposal": "SELECT * FROM order_proposal",
        "rfq": "SELECT * FROM rfq",
        "supplier_quote": "SELECT * FROM supplier_quote",
        "escalation": "SELECT * FROM escalation",
        "diagnosis": "SELECT * FROM diagnosis",
        "operator_note": "SELECT * FROM operator_note",
        "outbound_message": "SELECT * FROM outbound_message",
        "inbound_message": "SELECT * FROM inbound_message",
        "agent_event": "SELECT * FROM agent_event",
    },
}

pool = ThreadPoolExecutor(max_workers=10)


def sql_rows(query: str) -> list:
    req = urllib.request.Request(f"{HOST}/v1/database/{DB}/sql", data=query.encode(),
                                 headers={"Content-Type": "text/plain"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        txt = r.read().decode()
    out = []
    for res in (json.loads(txt) if txt.strip() else []):
        names = [e["name"]["some"] for e in res["schema"]["elements"]]
        out += [dict(zip(names, row)) for row in res["rows"]]
    return out


def snapshot(which: str) -> dict:
    queries = QUERIES.get(which, QUERIES["fast"])
    futures = {name: pool.submit(sql_rows, q) for name, q in queries.items()}
    data, errors = {}, {}
    for name, f in futures.items():
        try:
            data[name] = f.result()
        except urllib.error.HTTPError as e:
            errors[name] = f"HTTP {e.code}: {e.read().decode(errors='replace')[:200]}"
        except Exception as e:
            errors[name] = str(e)[:200]
    return {"ok": not errors, "tables": data, "errors": errors, "db": f"{HOST}/{DB}"}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/api/snapshot":
            which = parse_qs(url.query).get("set", ["fast"])[0]
            body = json.dumps(snapshot(which)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")   # always load the newest app.js / live.js
        super().end_headers()

    def log_message(self, fmt, *args):
        if "/api/snapshot" not in (args[0] if args else ""):
            sys.stderr.write("%s\n" % (fmt % args))


if __name__ == "__main__":
    print(f"Forge Flow dashboard: http://localhost:{PORT}   (data: {HOST}/{DB}, read-only)")
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        pass
