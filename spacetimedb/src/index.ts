// Spacetime "whiteboard" for the predictive-maintenance demo.
// Fields mirror contracts.py (PartHealth, OrderProposal). Keep them in sync.
import { schema, table, t, SenderError } from 'spacetimedb/server';

// Fixed thresholds - keep in sync with health.py's derive_state(), which is the authoritative copy.
function deriveState(health: number): string {
  if (health >= 70) return 'healthy';
  if (health >= 40) return 'warning';
  return 'critical';
}

// Current health of each part: ONE row per part, updated in place. The dashboard subscribes to this.
const part_health = table(
  { name: 'part_health', public: true },
  {
    part: t.string().primaryKey(),
    health: t.f64(),          // 0-100
    drift_lo_hz: t.f64(),
    drift_hi_hz: t.f64(),
    eta_s: t.f64(),           // rough estimate; -1 means "no estimate" (contracts.py uses None)
    updated_at: t.f64(),      // unix seconds
    state: t.string().default("healthy"),  // healthy | warning | critical - derived, never written directly by agents
  }
);

// History of every report, for charts / replay.
const health_log = table(
  { name: 'health_log', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    part: t.string(),
    health: t.f64(),
    drift_lo_hz: t.f64(),
    drift_hi_hz: t.f64(),
    eta_s: t.f64(),
    updated_at: t.f64(),
    state: t.string().default("healthy"),
  }
);

// Raw per-band frequency data for one Watcher tick. Lets the Analyst/dashboard show the full
// spectrum, not just the single worst band that part_health carries.
const spectrum_reading = table(
  { name: 'spectrum_reading', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    part: t.string(),
    bands_json: t.string(),       // JSON [[lo,hi], ...] - the fixed band edges for this run
    z_scores_json: t.string(),    // JSON [z, ...] - one z-score per band, this tick
    updated_at: t.f64(),
  }
);

// Opened when a part transitions into warning/critical. Photon subscribes to this to start the
// operator interview. ONE open row per part at a time (open_observation_request reducer is
// idempotent about this - see below), closed by Photon when the interview is done/times out.
const observation_request = table(
  { name: 'observation_request', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    part: t.string(),
    state: t.string(),      // warning | critical - snapshot from the part_health row that opened/refreshed it
    health: t.f64(),
    eta_s: t.f64(),
    opened_at: t.f64(),
    status: t.string(),     // open | collecting | complete
    completed_at: t.f64(),  // -1 until Photon marks it done
  }
);

// Orders proposed by the procurement agent. status: needs_approval | approved | rejected - there
// is NO auto-approved status, at any price. Only Photon (channel = imessage) can ever move a row
// out of needs_approval - see set_order_status below.
const order_proposal = table(
  { name: 'order_proposal', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    part: t.string(),
    supplier: t.string(),
    qty: t.u32(),
    unit_price: t.f64(),      // MOCK catalog data
    lead_days: t.u32(),
    reason: t.string(),
    status: t.string(),
    updated_at: t.f64(),
    product_url: t.string().default(""),  // MOCK link shown to the human before they approve
    approved_by: t.string().default(""),  // operator's name, set only by set_order_status
    channel: t.string().default(""),      // set to "imessage" only by set_order_status
  }
);

// One answered interview topic. Written by Photon as the operator answers each question.
const human_observation = table(
  { name: 'human_observation', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    observation_request_id: t.u64(),
    part: t.string(),
    field: t.string(),       // sound | heat | obstruction | belt_behavior | smell_visual | recent_changes
    value: t.string(),       // short structured value, e.g. "grinding"
    raw_text: t.string(),    // the operator's actual words
    reported_by: t.string(),
    created_at: t.f64(),
  }
);

// One heartbeat per agent. "Stale" is derived at read time (now - last_heartbeat), not stored -
// there's no scheduled job flipping a status column, any reader just compares against now.
const agent_status = table(
  { name: 'agent_status', public: true },
  {
    agent_name: t.string().primaryKey(),
    last_heartbeat: t.f64(),
    note: t.string(),
  }
);

// A live timeline of every agent's major steps, across both channels.
const agent_event = table(
  { name: 'agent_event', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    agent: t.string(),
    event_type: t.string(),
    part: t.string(),
    order_id: t.i64(),   // -1 if not applicable
    rfq_id: t.i64(),     // -1 if not applicable
    detail: t.string(),
    created_at: t.f64(),
  }
);

// The Analyst's reasoning for one incident - combines sensor data and whatever human_observation
// rows exist at the time it was written (may be none yet - that's honestly reflected upstream).
const diagnosis = table(
  { name: 'diagnosis', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    part: t.string(),
    observation_request_id: t.i64(),  // -1 if there wasn't one (shouldn't normally happen)
    likely_cause: t.string(),
    confidence: t.f64(),              // 0-1, heuristic, never invented beyond what evidence supports
    evidence_json: t.string(),        // JSON [string, ...] - the actual citations
    recommended_action: t.string(),
    created_at: t.f64(),
  }
);

// One sourcing round. The Buyer creates this, fans RFQRequest out to the supplier swarm (uAgent
// messages, not Spacetime), and writes a supplier_quote row for each reply it collects.
const rfq = table(
  { name: 'rfq', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    part: t.string(),
    max_price: t.f64(),
    max_wait_days: t.f64(),
    priority: t.string(),
    status: t.string(),       // collecting | complete | escalated
    created_at: t.f64(),
    completed_at: t.f64(),    // -1 until complete
  }
);

// One supplier's response to an rfq - written by the Buyer as each RFQQuote uAgent message arrives.
const supplier_quote = table(
  { name: 'supplier_quote', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    rfq_id: t.u64(),
    supplier: t.string(),
    available: t.bool(),
    unit_price: t.f64(),
    lead_days: t.u32(),
    shipping_cost: t.f64(),
    counter_note: t.string(),
    substitute_part: t.string(),
    created_at: t.f64(),
  }
);

// A negotiation decision point the Buyer can't resolve alone (counteroffer, over-budget quote, a
// substitute part). Photon is the only channel that answers these, same as order approval.
const escalation = table(
  { name: 'escalation', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    rfq_id: t.u64(),
    order_id: t.i64(),          // -1 if no order_proposal exists yet for this RFQ
    supplier: t.string(),
    kind: t.string(),           // counter | over_budget | substitute
    offer_text: t.string(),
    options_json: t.string(),   // JSON [string, ...] - plain-language choices Photon presents
    status: t.string(),         // open | answered
    answer: t.string(),
    answered_by: t.string(),
    created_at: t.f64(),
    answered_at: t.f64(),       // -1 until answered
  }
);

const spacetimedb = schema({
  part_health, health_log, order_proposal, spectrum_reading, observation_request,
  human_observation, escalation, diagnosis, rfq, supplier_quote, agent_status, agent_event,
});
export default spacetimedb;

// Called by the watcher/analyst (via Python over HTTP). Argument ORDER matters over HTTP:
// [part, health, drift_lo_hz, drift_hi_hz, eta_s, updated_at]
export const report_health = spacetimedb.reducer(
  {
    part: t.string(), health: t.f64(), drift_lo_hz: t.f64(),
    drift_hi_hz: t.f64(), eta_s: t.f64(), updated_at: t.f64(),
  },
  (ctx, row) => {
    const full = { ...row, state: deriveState(row.health) };
    const existing = ctx.db.part_health.part.find(row.part);
    if (existing) ctx.db.part_health.part.update({ ...full });
    else ctx.db.part_health.insert({ ...full });
    ctx.db.health_log.insert({ id: 0n, ...full });
  }
);

// Called by the watcher. Args: [part, bands_json, z_scores_json, updated_at]
export const write_spectrum_reading = spacetimedb.reducer(
  { part: t.string(), bands_json: t.string(), z_scores_json: t.string(), updated_at: t.f64() },
  (ctx, row) => {
    ctx.db.spectrum_reading.insert({ id: 0n, ...row });
  }
);

// Called by the watcher on a state transition into/within warning or critical. Idempotent: if an
// observation_request is already open (status != complete) for this part, refreshes it in place
// instead of opening a second one - this is the real debounce, the watcher's own tracking is just
// a (redundant but harmless) first line of defense. Args: [part, state, health, eta_s, opened_at]
export const open_observation_request = spacetimedb.reducer(
  { part: t.string(), state: t.string(), health: t.f64(), eta_s: t.f64(), opened_at: t.f64() },
  (ctx, row) => {
    const existing = [...ctx.db.observation_request.iter()].find(
      (r) => r.part === row.part && r.status !== 'complete'
    );
    if (existing) {
      ctx.db.observation_request.id.update({ ...existing, state: row.state, health: row.health, eta_s: row.eta_s });
    } else {
      ctx.db.observation_request.insert({
        id: 0n, part: row.part, state: row.state, health: row.health, eta_s: row.eta_s,
        opened_at: row.opened_at, status: 'open', completed_at: -1,
      });
    }
  }
);

// Called by the Buyer. Args: [part, supplier, qty, unit_price, lead_days, reason, status, updated_at, product_url]
// status MUST be needs_approval - this is the literal "forcing needs_approval, reject
// auto_approved" rule. There is no code path, anywhere, that proposes an already-approved order.
export const propose_order = spacetimedb.reducer(
  {
    part: t.string(), supplier: t.string(), qty: t.u32(), unit_price: t.f64(),
    lead_days: t.u32(), reason: t.string(), status: t.string(), updated_at: t.f64(),
    product_url: t.string(),
  },
  (ctx, row) => {
    if (row.status !== 'needs_approval') throw new SenderError(`orders must be proposed as needs_approval, got: ${row.status}`);
    ctx.db.order_proposal.insert({ id: 0n, ...row, approved_by: '', channel: '' });
  }
);

// Called ONLY by Photon's approve_order/reject_order tools. Args: [id, status, updated_at,
// approved_by, channel]. channel must be "imessage" - this is what makes ASI:One structurally
// incapable of approving or rejecting an order, even if a bug ever let it try.
export const set_order_status = spacetimedb.reducer(
  { id: t.u64(), status: t.string(), updated_at: t.f64(), approved_by: t.string(), channel: t.string() },
  (ctx, { id, status, updated_at, approved_by, channel }) => {
    if (status !== 'approved' && status !== 'rejected') throw new SenderError('status must be approved or rejected');
    if (channel !== 'imessage') throw new SenderError('orders can only be approved or rejected via iMessage');
    const order = ctx.db.order_proposal.id.find(id);
    if (!order) throw new SenderError('no such order');
    if (order.status !== 'needs_approval') throw new SenderError(`order is already ${order.status}`);
    ctx.db.order_proposal.id.update({ ...order, status, updated_at, approved_by, channel });
  }
);

// Called by Photon as the operator answers each interview topic. Args: [observation_request_id,
// part, field, value, raw_text, reported_by, created_at]
export const write_human_observation = spacetimedb.reducer(
  {
    observation_request_id: t.u64(), part: t.string(), field: t.string(), value: t.string(),
    raw_text: t.string(), reported_by: t.string(), created_at: t.f64(),
  },
  (ctx, row) => {
    ctx.db.human_observation.insert({ id: 0n, ...row });
  }
);

// Called by Photon when the interview is done (all topics answered or timed out). Args: [id, completed_at]
export const complete_observation_request = spacetimedb.reducer(
  { id: t.u64(), completed_at: t.f64() },
  (ctx, { id, completed_at }) => {
    const row = ctx.db.observation_request.id.find(id);
    if (!row) throw new SenderError('no such observation_request');
    ctx.db.observation_request.id.update({ ...row, status: 'complete', completed_at });
  }
);

// Called by every agent on an interval to stay marked alive. Args: [agent_name, note, now]
export const heartbeat = spacetimedb.reducer(
  { agent_name: t.string(), note: t.string(), now: t.f64() },
  (ctx, { agent_name, note, now }) => {
    const existing = ctx.db.agent_status.agent_name.find(agent_name);
    const row = { agent_name, last_heartbeat: now, note };
    if (existing) ctx.db.agent_status.agent_name.update(row);
    else ctx.db.agent_status.insert(row);
  }
);

// Called by every agent at each major step, on both channels. Args: [agent, event_type, part,
// order_id, rfq_id, detail, created_at]
export const log_event = spacetimedb.reducer(
  {
    agent: t.string(), event_type: t.string(), part: t.string(), order_id: t.i64(),
    rfq_id: t.i64(), detail: t.string(), created_at: t.f64(),
  },
  (ctx, row) => {
    ctx.db.agent_event.insert({ id: 0n, ...row });
  }
);

// Called by the Analyst. Args: [part, observation_request_id, likely_cause, confidence,
// evidence_json, recommended_action, created_at]
export const write_diagnosis = spacetimedb.reducer(
  {
    part: t.string(), observation_request_id: t.i64(), likely_cause: t.string(), confidence: t.f64(),
    evidence_json: t.string(), recommended_action: t.string(), created_at: t.f64(),
  },
  (ctx, row) => {
    ctx.db.diagnosis.insert({ id: 0n, ...row });
  }
);

// Called by the Buyer. Args: [part, max_price, max_wait_days, priority, created_at]
export const create_rfq = spacetimedb.reducer(
  { part: t.string(), max_price: t.f64(), max_wait_days: t.f64(), priority: t.string(), created_at: t.f64() },
  (ctx, row) => {
    ctx.db.rfq.insert({ id: 0n, ...row, status: 'collecting', completed_at: -1 });
  }
);

// Called by the Buyer as each supplier's RFQQuote arrives. Args: [rfq_id, supplier, available,
// unit_price, lead_days, shipping_cost, counter_note, substitute_part, created_at]
export const write_supplier_quote = spacetimedb.reducer(
  {
    rfq_id: t.u64(), supplier: t.string(), available: t.bool(), unit_price: t.f64(),
    lead_days: t.u32(), shipping_cost: t.f64(), counter_note: t.string(),
    substitute_part: t.string(), created_at: t.f64(),
  },
  (ctx, row) => {
    ctx.db.supplier_quote.insert({ id: 0n, ...row });
  }
);

// Called by the Buyer once it's done collecting (all suppliers responded or the window elapsed).
// Args: [id, status, completed_at]
export const complete_rfq = spacetimedb.reducer(
  { id: t.u64(), status: t.string(), completed_at: t.f64() },
  (ctx, { id, status, completed_at }) => {
    const row = ctx.db.rfq.id.find(id);
    if (!row) throw new SenderError('no such rfq');
    ctx.db.rfq.id.update({ ...row, status, completed_at });
  }
);

// Called by the Buyer when a quote needs human judgment. Args: [rfq_id, order_id, supplier, kind,
// offer_text, options_json, created_at]
export const create_escalation = spacetimedb.reducer(
  {
    rfq_id: t.u64(), order_id: t.i64(), supplier: t.string(), kind: t.string(),
    offer_text: t.string(), options_json: t.string(), created_at: t.f64(),
  },
  (ctx, row) => {
    ctx.db.escalation.insert({ id: 0n, ...row, status: 'open', answer: '', answered_by: '', answered_at: -1 });
  }
);

// Called ONLY by Photon, after the operator resolves a negotiation point. Args: [id, answer, answered_by, answered_at]
export const answer_escalation = spacetimedb.reducer(
  { id: t.u64(), answer: t.string(), answered_by: t.string(), answered_at: t.f64() },
  (ctx, { id, answer, answered_by, answered_at }) => {
    const row = ctx.db.escalation.id.find(id);
    if (!row) throw new SenderError('no such escalation');
    if (row.status !== 'open') throw new SenderError(`escalation is already ${row.status}`);
    ctx.db.escalation.id.update({ ...row, status: 'answered', answer, answered_by, answered_at });
  }
);

// Wipe everything between demo runs.
export const reset_demo = spacetimedb.reducer({}, (ctx) => {
  for (const r of [...ctx.db.part_health.iter()]) ctx.db.part_health.part.delete(r.part);
  for (const r of [...ctx.db.health_log.iter()]) ctx.db.health_log.id.delete(r.id);
  for (const r of [...ctx.db.order_proposal.iter()]) ctx.db.order_proposal.id.delete(r.id);
  for (const r of [...ctx.db.spectrum_reading.iter()]) ctx.db.spectrum_reading.id.delete(r.id);
  for (const r of [...ctx.db.observation_request.iter()]) ctx.db.observation_request.id.delete(r.id);
  for (const r of [...ctx.db.human_observation.iter()]) ctx.db.human_observation.id.delete(r.id);
  for (const r of [...ctx.db.escalation.iter()]) ctx.db.escalation.id.delete(r.id);
  for (const r of [...ctx.db.diagnosis.iter()]) ctx.db.diagnosis.id.delete(r.id);
  for (const r of [...ctx.db.rfq.iter()]) ctx.db.rfq.id.delete(r.id);
  for (const r of [...ctx.db.supplier_quote.iter()]) ctx.db.supplier_quote.id.delete(r.id);
  for (const r of [...ctx.db.agent_event.iter()]) ctx.db.agent_event.id.delete(r.id);
  // agent_status is intentionally NOT wiped - heartbeats should keep tracking real process uptime
  // across a demo reset, not look "all stale" right after reset_demo runs.
});
