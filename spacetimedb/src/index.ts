// Spacetime "whiteboard" for the predictive-maintenance demo.
// Fields mirror contracts.py (PartHealth, OrderProposal). Keep them in sync.
import { schema, table, t, SenderError } from 'spacetimedb/server';

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
  }
);

// Orders proposed by the procurement agent. status: auto_approved | needs_approval | approved | rejected
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
  }
);

const spacetimedb = schema({ part_health, health_log, order_proposal });
export default spacetimedb;

// Called by the watcher/analyst (via Python over HTTP). Argument ORDER matters over HTTP:
// [part, health, drift_lo_hz, drift_hi_hz, eta_s, updated_at]
export const report_health = spacetimedb.reducer(
  {
    part: t.string(), health: t.f64(), drift_lo_hz: t.f64(),
    drift_hi_hz: t.f64(), eta_s: t.f64(), updated_at: t.f64(),
  },
  (ctx, row) => {
    const existing = ctx.db.part_health.part.find(row.part);
    if (existing) ctx.db.part_health.part.update({ ...row });
    else ctx.db.part_health.insert({ ...row });
    ctx.db.health_log.insert({ id: 0n, ...row });
  }
);

const STATUSES = ['auto_approved', 'needs_approval', 'approved', 'rejected'];

// Called by the procurement agent. Args: [part, supplier, qty, unit_price, lead_days, reason, status, updated_at]
export const propose_order = spacetimedb.reducer(
  {
    part: t.string(), supplier: t.string(), qty: t.u32(), unit_price: t.f64(),
    lead_days: t.u32(), reason: t.string(), status: t.string(), updated_at: t.f64(),
  },
  (ctx, row) => {
    if (!STATUSES.includes(row.status)) throw new SenderError(`bad status: ${row.status}`);
    ctx.db.order_proposal.insert({ id: 0n, ...row });
  }
);

// Called by the dashboard button or the Photon "approve" reply. Args: [id, status, updated_at]
// Only orders waiting for a human can be approved or rejected.
export const set_order_status = spacetimedb.reducer(
  { id: t.u64(), status: t.string(), updated_at: t.f64() },
  (ctx, { id, status, updated_at }) => {
    if (status !== 'approved' && status !== 'rejected') throw new SenderError('status must be approved or rejected');
    const order = ctx.db.order_proposal.id.find(id);
    if (!order) throw new SenderError('no such order');
    if (order.status !== 'needs_approval') throw new SenderError(`order is already ${order.status}`);
    ctx.db.order_proposal.id.update({ ...order, status, updated_at });
  }
);

// Wipe everything between demo runs.
export const reset_demo = spacetimedb.reducer({}, (ctx) => {
  for (const r of [...ctx.db.part_health.iter()]) ctx.db.part_health.part.delete(r.part);
  for (const r of [...ctx.db.health_log.iter()]) ctx.db.health_log.id.delete(r.id);
  for (const r of [...ctx.db.order_proposal.iter()]) ctx.db.order_proposal.id.delete(r.id);
});
