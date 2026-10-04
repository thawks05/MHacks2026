/** Tool functions the conversation engine (rule-based or LLM) calls. Every answer comes from a
 * real Spacetime read - never invented numbers. Each tool returns a plain JS object; both engines
 * call these same functions, so there is exactly one implementation of each, not two. */
import * as st from "./spacetime.js";
import * as local from "./localstore.js";

function observationsForPart(part: string) {
  const reqs = [...st.getConnection().db.observationRequest.iter()].filter((r) => r.part === part);
  if (!reqs.length) return [];
  const target = reqs.find((r) => r.status !== "complete") ?? [...reqs].sort((a, b) => b.openedAt - a.openedAt)[0];
  return st.humanObservationsFor(target.id);
}

export function get_machine_status() {
  const rows = st.latestHealth();
  return {
    parts: rows.map((r) => ({ part: r.part, health: r.health, state: r.state, driftLoHz: r.driftLoHz, driftHiHz: r.driftHiHz, etaS: r.etaS === -1 ? null : r.etaS })),
  };
}

export function get_part_health(part: string) {
  const row = st.healthFor(part);
  if (!row) return { found: false, part };
  return {
    found: true, part: row.part, health: row.health, state: row.state,
    driftLoHz: row.driftLoHz, driftHiHz: row.driftHiHz,
    etaS: row.etaS === -1 ? null : row.etaS,
    observations: observationsForPart(part).map((o) => ({ field: o.field, value: o.value, rawText: o.rawText, reportedBy: o.reportedBy })),
  };
}

export function get_health_history(part: string, limit = 10) {
  const rows = [...st.getConnection().db.healthLog.iter()]
    .filter((r) => r.part === part)
    .sort((a, b) => Number(b.id - a.id))
    .slice(0, limit);
  return { part, rows: rows.map((r) => ({ health: r.health, state: r.state, updatedAt: r.updatedAt })) };
}

export function list_orders(status?: string) {
  const all = st.allOrders();
  const rows = status ? all.filter((o) => o.status === status) : all;
  return { orders: rows.map(orderToPlain) };
}

export function get_order(id: number) {
  const row = st.orderById(BigInt(id));
  return row ? { found: true, order: orderToPlain(row) } : { found: false, id };
}

export function explain_diagnosis(part: string) {
  const row = st.healthFor(part);
  if (!row) return { explained: false, text: `No data for ${part} yet.` };
  const obs = observationsForPart(part);
  let text = `${part} is at ${row.health.toFixed(0)}% health (${row.state}), drifting in the ${row.driftLoHz.toFixed(0)}-${row.driftHiHz.toFixed(0)}Hz band.`;
  if (obs.length) {
    text += ` Operator also reported: ${obs.map((o) => `${o.field}: "${o.rawText}"`).join("; ")}.`;
  } else {
    text += " No operator observations yet - this is from sensor data only.";
  }
  return { explained: true, text };
}

export async function approve_order(id: number, approvedBy: string) {
  await st.setOrderStatus(BigInt(id), "approved", approvedBy);
  local.addPastDecision(approvedBy, `approved order #${id}`);
  return { ok: true, id, status: "approved" };
}

export async function reject_order(id: number, rejectedBy: string) {
  await st.setOrderStatus(BigInt(id), "rejected", rejectedBy);
  local.addPastDecision(rejectedBy, `rejected order #${id}`);
  return { ok: true, id, status: "rejected" };
}

export async function record_observation(observationRequestId: number, part: string, field: string, value: string, rawText: string, reportedBy: string) {
  await st.writeHumanObservation(BigInt(observationRequestId), part, field, value, rawText, reportedBy);
  return { ok: true, field, value };
}

export async function answer_escalation(id: number, answer: string, answeredBy: string) {
  await st.answerEscalation(BigInt(id), answer, answeredBy);
  return { ok: true, id, answer };
}

const STALE_AFTER_S = 30;

export function get_agent_status() {
  const rows = st.agentStatuses();
  const now = Date.now() / 1000;
  return {
    agents: rows.map((r) => ({
      agent: r.agentName,
      alive: now - r.lastHeartbeat < STALE_AFTER_S,
      secondsSinceHeartbeat: now - r.lastHeartbeat,
    })),
  };
}

export function get_overnight_digest() {
  const rows = st.latestHealth();
  const orders = st.allOrders().slice(0, 10);
  const lowest = [...rows].sort((a, b) => a.health - b.health)[0];
  return {
    summary: `${rows.length} parts tracked. Lowest health: ${lowest ? `${lowest.part} at ${lowest.health.toFixed(0)}% (${lowest.state})` : "n/a"}.`,
    recentOrders: orders.map(orderToPlain),
    openEscalations: st.openEscalations().map(escalationToPlain),
  };
}

function orderToPlain(o: ReturnType<typeof st.allOrders>[number]) {
  return {
    id: Number(o.id), part: o.part, supplier: o.supplier, qty: o.qty,
    unitPrice: o.unitPrice, leadDays: o.leadDays, reason: o.reason,
    status: o.status, productUrl: o.productUrl, approvedBy: o.approvedBy, channel: o.channel,
  };
}

function escalationToPlain(e: ReturnType<typeof st.openEscalations>[number]) {
  return {
    id: Number(e.id), supplier: e.supplier, kind: e.kind, offerText: e.offerText,
    options: JSON.parse(e.optionsJson || "[]") as string[],
  };
}
