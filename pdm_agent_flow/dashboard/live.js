// Live mode: every number on the page comes from SpacetimeDB, through dashboard_server.py.
// Read-only by design: nothing here can approve, resolve, or change anything. The operator does that
// by text, and the agents write the results. Loaded after app.js and reuses its helpers (els,
// showFeatureWindow, setRuntimeState, clamp, formatTime). Turn off "Live data" for app.js's
// browser-only demo, which is a fallback if the backend isn't running.

const LIVE = !new URLSearchParams(location.search).has("local");
const FAST_MS = 1000;
const SLOW_MS = 2500;
const STALE_S = 10;          // pod_window older than this = Watcher isn't writing
const AGENT_STALE_S = 30;    // heartbeat older than this = agent offline

const live = { fast: null, slow: null, lastOk: 0, traceTs: 0 };

const FEATURE_MAP = { rms: "rms_g", peak: "peak_g", frequency: "dom_freq_hz", kurtosis: "kurtosis" };
const DIGITS = { rms: 3, peak: 3, frequency: 1, kurtosis: 2 };
const STATUS_COLOR = { healthy: "var(--green)", watch: "var(--yellow)", degraded: "var(--red)", critical: "var(--red)" };
const PERSONALITY = {
  "NorthGear Co": "Plain quote", "PrecisionMesh": "Plain quote", "BulkParts Ltd": "Plain quote",
  "FlexDrive": "Counter if over budget", "ShaftPro": "5 s reply", "SouthBearing": "No belts",
  "MetroSupply": "Substitute offer",
};

const now = () => Date.now() / 1000;
const num = (v) => Number(v);
const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const hhmm = (ts) => new Date(num(ts) * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const byId = (rows) => [...(rows || [])].sort((a, b) => num(a.id) - num(b.id));
const latest = (rows, pred = () => true) => byId(rows).filter(pred).pop();
const cap = (s) => String(s || "").charAt(0).toUpperCase() + String(s || "").slice(1);
const setText = (id, text) => { if (els[id]) els[id].textContent = text; };

function setLiveStatus(kind, text) {
  els["live-status"].className = `live-status ${kind}`;
  els["live-status"].textContent = text;
}

async function getSnapshot(which) {
  const r = await fetch(`/api/snapshot?set=${which}`, { cache: "no-store" });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

// ---------------------------------------------------------------------------
// Signal: pod_window + pod_status
// ---------------------------------------------------------------------------
function renderPod(fast) {
  const win = (fast.pod_window || []).find((r) => r.part === "drive_gear") || (fast.pod_window || [])[0];
  const pod = (fast.pod_status || [])[0];
  if (!win) {
    setRuntimeState("Waiting for the Watcher", "No pod_window row yet · is run_all.py running?", "var(--yellow)", "Idle");
    els["monitor-state"].textContent = "No data yet";
    return;
  }
  const data = JSON.parse(win.data_json || "{}");
  const age = now() - num(win.updated_at);
  if (data.source) setText("source-summary", data.source);

  const sample = Object.fromEntries(Object.entries(FEATURE_MAP).map(([k, f]) => [k, num(data.features?.[f])]));
  if (num(win.updated_at) !== live.traceTs && Object.values(sample).every(Number.isFinite)) {
    live.traceTs = num(win.updated_at);
    els["capture-panel"].classList.add("visible");
    showFeatureWindow(sample);
  }

  if (age > STALE_S) {
    setLiveStatus("stale", `Watcher silent ${Math.round(age)}s`);
  }

  if (win.mode === "training") {
    const n = num(data.baseline_windows) || 0, need = num(data.baseline_needed) || 60;
    els["baseline-step"].className = "run-step running";
    els["monitor-step"].className = "run-step disabled";
    els["baseline-summary"].hidden = true;
    setText("baseline-timer", formatTime(n));
    els["baseline-progress"].style.width = `${clamp(n / need * 100, 0, 100)}%`;
    setText("sample-count", `${n} healthy windows accepted` + (data.baseline_rejected ? ` · ${data.baseline_rejected} rejected (too quiet)` : ""));
    setText("capture-status", "Accepting healthy one-second windows");
    setText("monitor-state", "Waiting for baseline");
    setText("watcher-state", "Active · training");
    setText("rail-watcher", "training");
    setRuntimeState("Training healthy state", "Pod 01 · baseline", "var(--purple)", "Training");
    resetSignalView("Training the baseline. Scoring starts after 60 healthy windows.");
    return;
  }

  if (win.mode !== "monitoring" || !data.baseline) {
    els["baseline-step"].className = "run-step";
    els["monitor-step"].className = "run-step disabled";
    setText("monitor-state", "Pod idle");
    setText("rail-watcher", "idle");
    setRuntimeState("Pod idle", "Motor off, or no baseline yet", "var(--yellow)", "Idle");
    resetSignalView("No baseline. The pod is idle.");
    return;
  }

  // Monitoring
  els["baseline-step"].className = "run-step complete";
  els["monitor-step"].className = "run-step running";
  els["baseline-summary"].hidden = false;
  setText("baseline-timer", "01:00");
  els["baseline-progress"].style.width = "100%";
  setText("sample-count", "60 healthy windows accepted");
  setText("capture-status", "Scoring live one-second windows");
  Object.entries(FEATURE_MAP).forEach(([k, f]) => {
    const b = num(data.baseline[f]);
    setText(`baseline-${k}`, b.toFixed(DIGITS[k]));
    setText(`compare-${k}-base`, b.toFixed(DIGITS[k]));
    const cur = sample[k];
    setText(`compare-${k}-current`, Number.isFinite(cur) ? cur.toFixed(DIGITS[k]) : "—");
    const dev = b ? (cur - b) / Math.abs(b) * 100 : 0;
    setText(`deviation-${k}`, `${dev >= 0 ? "+" : ""}${dev.toFixed(1)}%`);
    els[`bar-${k}`].style.width = `${clamp(Math.abs(dev) * 1.5, 2, 100)}%`;
    const card = document.querySelector(`[data-metric="${k}"]`);
    card.classList.toggle("warning", Math.abs(dev) >= 8);
    card.classList.toggle("critical", Math.abs(dev) >= 25);
  });

  const status = win.status;
  const health = Math.round(num(win.health));
  const color = STATUS_COLOR[status] || "var(--muted)";
  const count = num(win.deviating_s) || 0;
  setText("health-value", health);
  setText("health-label", cap(status));
  els["health-label"].style.color = color;
  els["health-ring"].style.background = `radial-gradient(circle,var(--panel) 57%,transparent 58%),conic-gradient(${color} 0 ${health}%,#303735 ${health}% 100%)`;
  const top = (data.top || []).slice(0, 2).map(([n, z]) => `${n} ${num(z) >= 0 ? "+" : ""}${num(z).toFixed(1)}σ`).join(", ");
  setText("health-explanation", status === "healthy"
    ? (top ? `Within normal range. Largest moves: ${top}.` : "All features within normal range.")
    : `Worst deviations from baseline: ${top}.`);
  setText("deviation-count", `${count} consecutive deviating windows`);
  els["hold-progress"].style.width = `${clamp(count / 30 * 100, 0, 100)}%`;
  els["hold-progress"].style.background = color;

  const bandIds = { band_low_g: "low", band_mid_g: "mid", band_high_g: "high" };
  (data.bands || []).forEach((b) => {
    const id = bandIds[b.name];
    if (!id) return;
    const z = Math.abs(num(b.z) || 0);
    els[`band-${id}`].style.width = `${clamp(z / 25 * 100, 2, 100)}%`;
    els[`band-${id}`].style.background = z >= 10 ? "var(--red)" : z >= 4 ? "var(--yellow)" : "var(--purple)";
    setText(`band-${id}-value`, `${num(b.z) >= 0 ? "+" : ""}${num(b.z).toFixed(1)}σ`);
  });
  const eta = data.eta;
  setText("eta-value", !eta ? "Not enough decline"
    : num(eta.demo_s) === 0 ? "At the failure threshold now"
    : `~${eta.runtime_hours} machine hours (~${eta.shifts} shifts)`);

  const since = pod && pod.mode === "monitoring" ? now() - num(pod.since) : null;
  setText("monitor-state", status === "healthy" ? "Streaming and scoring" : `${cap(status)} · scoring`);
  setText("monitor-duration", since != null ? `${formatTime(since)} monitoring` : "monitoring");
  setText("watcher-state", "Active · scoring windows");
  setText("rail-watcher", "monitoring");
  setRuntimeState(status === "healthy" ? "Monitoring · healthy" : `Monitoring · ${status}`,
    "Pod 01 · monitoring", color, "Monitoring");
}

function resetSignalView(message) {
  setText("health-value", "—");
  setText("health-label", "No scored window");
  els["health-label"].style.color = "";
  setText("health-explanation", message);
  setText("deviation-count", "0 consecutive deviating windows");
  els["hold-progress"].style.width = "0%";
  setText("eta-value", "Not enough decline");
}

// ---------------------------------------------------------------------------
// Incident + lifecycle
// ---------------------------------------------------------------------------
function currentIncident(fast) {
  return latest(fast.incident, (i) => i.status === "open") || latest(fast.incident);
}

function renderIncident(fast, slow) {
  const inc = currentIncident(fast);
  const box = els["incident-console"];
  const steps = [...document.querySelectorAll(".lifecycle-step")];
  if (!inc) {
    box.classList.remove("open", "critical");
    setText("incident-state", "No open incident");
    setText("incident-title", "Monitoring, nothing flagged");
    setText("incident-detail", "At watch, the Watcher opens an incident and pushes a PartAlert to the Analyst.");
    setText("incident-id", "—");
    setText("incident-health", "—");
    steps.forEach((s) => s.classList.remove("active"));
    return;
  }
  const id = num(inc.id);
  const open = inc.status === "open";
  box.classList.toggle("open", open);
  box.classList.toggle("critical", open && inc.state === "critical");
  setText("incident-state", `${inc.state.toUpperCase()} · ${inc.status}`);
  setText("incident-title", `Incident #${id} · ${inc.part}`);
  setText("incident-id", `#${id} · ${inc.status}`);
  setText("incident-health", `${Math.round(num(inc.worst_health))}%`);

  const texts = (slow.outbound_message || []).filter((m) => num(m.created_at) >= num(inc.opened_at));
  const diag = latest(slow.diagnosis, (d) => num(d.observation_request_id) === id);
  const notes = (slow.operator_note || []).filter((n) => num(n.incident_id) === id);
  const order = num(inc.order_id) >= 0 ? (slow.order_proposal || []).find((o) => num(o.id) === num(inc.order_id)) : null;

  let detail;
  if (!open) detail = `Resolved by the operator at ${hhmm(inc.resolved_at)}: “${inc.resolution}”. The pod retrains on the new normal.`;
  else if (order) detail = `Order #${num(order.id)} (${order.supplier}, $${num(order.unit_price).toFixed(0)}) ` +
    ({ needs_approval: "is waiting for the operator's YES.", approved: "was approved by text. The operator texts RESOLVED once it's installed.",
       rejected: "was rejected. Back to the options." }[order.status] || `is ${order.status}.`);
  else if (diag) detail = `Diagnosis: ${diag.likely_cause} (${Math.round(num(diag.confidence) * 100)}% confidence). Waiting on the operator's choice.`;
  else if (notes.length) detail = `Operator reported: “${notes[notes.length - 1].text}”. Diagnosing.`;
  else if (texts.length) detail = "The Analyst texted the operator and is waiting for what they see, hear or feel.";
  else detail = "The Watcher opened this incident and alerted the Analyst.";
  setText("incident-detail", detail);

  const pod = (fast.pod_status || [])[0];
  let stage = 0;
  if (texts.length) stage = 1;
  if (diag) stage = 2;
  if (!open) stage = 3;
  if (!open && pod && num(pod.since) >= num(inc.resolved_at)) stage = 4;
  steps.forEach((s, i) => s.classList.toggle("active", i <= stage));
}

// ---------------------------------------------------------------------------
// Agents
// ---------------------------------------------------------------------------
function renderAgents(fast, slow) {
  const hb = Object.fromEntries((fast.agent_status || []).map((a) => [a.agent_name, a]));
  let alive = 0;
  for (const name of ["watcher", "analyst", "buyer"]) {
    const a = hb[name];
    const ok = a && now() - num(a.last_heartbeat) < AGENT_STALE_S;
    if (ok) alive += 1;
    els[`dot-${name}`].className = `status-dot ${ok ? "active" : "offline"}`;
    if (!ok) setText(`rail-${name}`, "offline");
  }
  const rfq = latest(slow.rfq);
  if (hb.analyst && now() - num(hb.analyst.last_heartbeat) < AGENT_STALE_S) {
    const open = (fast.incident || []).some((i) => i.status === "open");
    setText("rail-analyst", open ? "in conversation" : "ready");
    setText("analyst-state", open ? "Active · texting operator" : "Ready");
  }
  if (hb.buyer && now() - num(hb.buyer.last_heartbeat) < AGENT_STALE_S) {
    const busy = rfq && rfq.status === "collecting";
    setText("rail-buyer", busy ? "collecting quotes" : "standby");
    setText("buyer-state", busy ? "Active · RFQ out" : "Standby");
  }
  if (rfq) {
    const n = (slow.supplier_quote || []).filter((q) => num(q.rfq_id) === num(rfq.id)).length;
    setText("rail-suppliers", `${n} / 7 quoted`);
  }
  setText("network-online", `${alive} / 3 core online`);
}

// ---------------------------------------------------------------------------
// Procurement, conversation, phone
// ---------------------------------------------------------------------------
function renderProcurement(fast, slow) {
  const inc = currentIncident(fast);
  const rfq = inc && num(inc.rfq_id) >= 0
    ? (slow.rfq || []).find((r) => num(r.id) === num(inc.rfq_id))
    : latest(slow.rfq);
  const order = inc && num(inc.order_id) >= 0
    ? (slow.order_proposal || []).find((o) => num(o.id) === num(inc.order_id))
    : latest(slow.order_proposal);
  const diag = inc ? latest(slow.diagnosis, (d) => num(d.observation_request_id) === num(inc.id)) : latest(slow.diagnosis);

  setText("dec-reco", diag ? diag.recommended_action : "No diagnosis yet");
  setText("dec-reco-detail", diag ? `Likely ${diag.likely_cause} (${Math.round(num(diag.confidence) * 100)}% confidence).` : "The Analyst diagnoses after the operator texts what they observe.");
  setText("dec-limits", rfq ? `$${num(rfq.max_price).toFixed(0)} maximum` : "—");
  setText("dec-limits-detail", rfq ? `${num(rfq.max_wait_days).toFixed(0)} days · ${rfq.priority} first` : "Asked only if a part must be bought");
  setText("dec-buyer", order ? order.supplier : rfq ? (rfq.status === "collecting" ? "Collecting quotes…" : "No order") : "—");
  setText("dec-buyer-detail", order ? `$${num(order.unit_price).toFixed(0)} · ${num(order.lead_days)}-day lead · mock` : "");
  const box = els["dec-decision-box"];
  box.className = "";
  if (!order) { box.classList.add("idle-result"); setText("dec-decision", "—"); setText("dec-decision-detail", "No order proposed"); }
  else if (order.status === "approved") { box.classList.add("approved-result"); setText("dec-decision", "Approved"); setText("dec-decision-detail", "Clear YES via iMessage · payment simulated"); }
  else if (order.status === "rejected") { box.classList.add("rejected-result"); setText("dec-decision", "Rejected"); setText("dec-decision-detail", "Operator replied NO"); }
  else { box.classList.add("pending-result"); setText("dec-decision", "Awaiting YES"); setText("dec-decision-detail", `Order #${num(order.id)} needs the operator's text`); }

  const table = els["supplier-table"];
  const head = '<div class="supplier-row supplier-head" role="row"><span>Supplier</span><span>Behavior</span><span>Lead</span><span>Unit</span><span>Outcome</span></div>';
  if (!rfq) {
    setText("rfq-label", "No RFQ yet · mock supplier data");
    setText("rfq-title", "Supplier responses");
    setText("rfq-priority", "Waiting for a purchase decision");
    table.innerHTML = head + '<div class="supplier-row empty" role="row"><span>Quotes appear here when the operator chooses repair or replace.</span></div>';
  } else {
    const quotes = (slow.supplier_quote || []).filter((q) => num(q.rfq_id) === num(rfq.id));
    const speed = rfq.priority === "speed";
    const key = (q) => speed ? [num(q.lead_days), num(q.unit_price)] : [num(q.unit_price), num(q.lead_days)];
    quotes.sort((a, b) => (b.available - a.available) || key(a)[0] - key(b)[0] || key(a)[1] - key(b)[1]);
    setText("rfq-label", `RFQ #${num(rfq.id)} · mock supplier data · ${rfq.status}`);
    setText("rfq-title", `${quotes.length} of 7 responses`);
    setText("rfq-priority", speed ? "Ranked by speed, then price" : "Ranked by price, then speed");
    table.innerHTML = head + quotes.map((q) => {
      const picked = Boolean(order && order.supplier === q.supplier);
      let outcome;
      if (!q.available) outcome = "Out of stock";
      else if (picked) outcome = "Picked";
      else if (q.substitute_part) outcome = "Substitute · human review";
      else if (q.counter_note) outcome = "Counteroffer";
      else if (num(q.unit_price) <= num(rfq.max_price) && num(q.lead_days) <= num(rfq.max_wait_days)) outcome = "Meets limits";
      else outcome = "Misses limits";
      const cls = picked ? "supplier-row selected" : q.available ? "supplier-row" : "supplier-row unavailable";
      return `<div class="${cls}" role="row"><strong>${esc(q.supplier)}${picked ? "<small>Picked</small>" : ""}</strong>` +
        `<span>${esc(PERSONALITY[q.supplier] || "")}</span><span>${q.available ? `${num(q.lead_days)} day${num(q.lead_days) === 1 ? "" : "s"}` : "—"}</span>` +
        `<span>${q.available ? `$${num(q.unit_price).toFixed(0)}` : "—"}</span><b>${esc(outcome)}</b></div>`;
    }).join("");
  }
  const esc_ = rfq ? latest(slow.escalation, (e) => num(e.rfq_id) === num(rfq.id)) : null;
  setText("escalation-title", esc_
    ? `Escalated (${esc_.kind.replace("_", " ")}): ${esc_.offer_text}${esc_.status === "answered" ? ` · operator chose “${esc_.answer}”` : " · waiting on the operator"}`
    : rfq ? "No negotiation was needed for the selected quote." : "No sourcing in progress.");
}

const EVENT_HIDE = new Set(["shift_summary"]);
function renderEvents(slow) {
  const events = byId(slow.agent_event).filter((e) => !EVENT_HIDE.has(e.event_type)).slice(-7);
  setText("events-badge", "Live · agent_event");
  els["conversation-events"].innerHTML = events.length ? events.map((e) => {
    let detail = String(e.detail || "");
    if (detail.startsWith("{")) detail = "";        // JSON payloads (baseline means) aren't readable here
    if (detail.length > 160) detail = detail.slice(0, 157) + "…";
    const title = `${cap(e.agent)} · ${e.event_type.replace(/_/g, " ")}` + (e.part ? ` · ${e.part}` : "");
    return `<li><time>${hhmm(e.created_at)}</time><i></i><div><strong>${esc(title)}</strong>${detail ? `<p>${esc(detail)}</p>` : ""}</div></li>`;
  }).join("") : '<li><time>—</time><i></i><div><strong>No agent events yet</strong><p>Start run_all.py.</p></div></li>';
}

let lastPhoneKey = null;
function renderPhone(slow) {
  const out = (slow.outbound_message || []).map((m) => ({ who: "agent", text: m.text, ts: num(m.created_at), pending: !m.sent }));
  const inn = (slow.inbound_message || []).map((m) => ({ who: "operator", text: m.text, ts: num(m.received_at) }));
  const all = [...out, ...inn].sort((a, b) => a.ts - b.ts).slice(-16);
  const key = all.map((m) => `${m.ts}${m.pending}`).join("|");
  if (key === lastPhoneKey) return;
  lastPhoneKey = key;
  const box = els["phone-messages"];
  setText("message-day", all.length ? `Today ${hhmm(all[0].ts)}` : "No texts yet");
  box.innerHTML = all.length ? all.map((m) =>
    `<div class="bubble ${m.who === "agent" ? "incoming" : "outgoing"}${m.pending ? " pending" : ""}">${esc(m.text)}</div>`
  ).join("") : '<div class="bubble incoming">Texts between the Analyst and the operator appear here.</div>';
  box.scrollTop = box.scrollHeight;
}

// ---------------------------------------------------------------------------
// Polling
// ---------------------------------------------------------------------------
async function pollFast() {
  try {
    const snap = await getSnapshot("fast");
    live.fast = snap.tables;
    live.lastOk = now();
    if (snap.ok) setLiveStatus("ok", "Live · SpacetimeDB");
    else if (!Object.keys(snap.tables).length) { setLiveStatus("down", "Can't reach SpacetimeDB · check SPACETIME_HOST"); return; }
    else setLiveStatus("stale", `Partial data: ${Object.keys(snap.errors).join(", ")}`);
    renderPod(live.fast);
    if (live.slow) { renderIncident(live.fast, live.slow); renderAgents(live.fast, live.slow); renderProcurement(live.fast, live.slow); }
  } catch (e) {
    setLiveStatus("down", "Database offline · run dashboard_server.py");
  }
}

async function pollSlow() {
  try {
    const snap = await getSnapshot("slow");
    live.slow = snap.tables;
    if (live.fast) { renderIncident(live.fast, live.slow); renderAgents(live.fast, live.slow); renderProcurement(live.fast, live.slow); }
    renderEvents(live.slow);
    renderPhone(live.slow);
  } catch (e) { /* pollFast reports the outage */ }
}

function enterLiveMode() {
  document.body.classList.add("live");
  if (state.baselineTimer) clearInterval(state.baselineTimer);
  if (state.monitorTimer) clearInterval(state.monitorTimer);
  state.baselineTimer = state.monitorTimer = null;
  els["baseline-button"].disabled = true;
  els["monitor-button"].disabled = true;
  setText("rail-suppliers", "7 / 7");
  pollFast();
  pollSlow();
  setInterval(pollFast, FAST_MS);
  setInterval(pollSlow, SLOW_MS);
}

els["live-mode"].checked = LIVE;
els["live-mode"].addEventListener("change", () => {
  const url = new URL(location.href);
  if (els["live-mode"].checked) url.searchParams.delete("local"); else url.searchParams.set("local", "1");
  location.href = url.toString();
});
if (LIVE) enterLiveMode();
else setLiveStatus("stale", "Local demo · not live");
