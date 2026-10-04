/** Watches Spacetime directly (not via the Python agents) and proactively texts known spaces:
 * starts the operator interview the moment an observation_request opens, and announces order
 * status changes - regardless of which channel (ASI:One or Photon) caused the change, matching
 * "approval from either channel updates the other." Debounced: only fires on a state transition,
 * not on every single update. */
import type { Space } from "@spectrum-ts/core";
import * as st from "./spacetime.js";
import * as interview from "./interview.js";
import { escalationPrompt } from "./escalation.js";

const startedInterviewFor = new Set<string>(); // observation_request id -> already kicked off
const lastOrderStatus = new Map<number, string>();
const announcedEscalation = new Set<string>(); // escalation id -> already told the operator
const pendingSince = new Map<number, number>(); // order id -> when it became needs_approval (ms)
const remindedOrders = new Set<number>(); // order id -> already sent the one gentle reminder
const REMINDER_AFTER_MS = 5 * 60 * 1000;
// Keyed by space.id, not object identity - the same logical conversation can hand us a freshly
// constructed Space wrapper on every incoming message, and a naive Set<Space> would (and did,
// confirmed in testing) silently accumulate duplicates and send every proactive alert N times.
const knownSpaces = new Map<string, Space>();

export function registerSpace(space: Space) {
  knownSpaces.set(space.id, space);
}

async function broadcast(text: string) {
  console.log(`[proactive] ${text}`);
  for (const space of knownSpaces.values()) {
    try {
      await space.send(text as any);
    } catch (e) {
      console.error("[proactive] failed to send to a space:", e);
    }
  }
}

export function startProactiveWatch() {
  const conn = st.getConnection();

  conn.db.observationRequest.onInsert((_ctx, row) => checkObservationRequest(row));
  conn.db.observationRequest.onUpdate((_ctx, _old, row) => checkObservationRequest(row));
  conn.db.orderProposal.onInsert((_ctx, row) => checkOrder(row));
  conn.db.orderProposal.onUpdate((_ctx, _old, row) => checkOrder(row));
  conn.db.escalation.onInsert((_ctx, row) => checkEscalation(row));

  function checkObservationRequest(row: { id: bigint; part: string; state: string; health: number; etaS: number; status: string }) {
    if (row.status === "complete") return;
    const key = row.id.toString();
    if (startedInterviewFor.has(key)) return; // interview.ts also guards this, but avoid the loop entirely
    startedInterviewFor.add(key);
    for (const space of knownSpaces.values()) {
      interview.startInterview(row.id, row.part, row.state, row.health, row.etaS, space.id, (text) => space.send(text as any));
    }
  }

  function checkOrder(row: { id: bigint; part: string; supplier: string; unitPrice: number; status: string }) {
    const id = Number(row.id);
    const prev = lastOrderStatus.get(id);
    if (row.status === prev) return;
    lastOrderStatus.set(id, row.status);
    if (row.status === "needs_approval") {
      pendingSince.set(id, Date.now());
      broadcast(`${row.part} order needs approval: ${row.supplier} $${row.unitPrice.toFixed(0)} (order #${id}). Reply YES or NO.`);
    } else if (row.status === "approved") {
      pendingSince.delete(id);
      remindedOrders.delete(id);
      broadcast(`Order #${id} (${row.part}/${row.supplier}) approved (simulated).`);
    } else if (row.status === "rejected") {
      pendingSince.delete(id);
      remindedOrders.delete(id);
      broadcast(`Order #${id} (${row.part}/${row.supplier}) was rejected.`);
    }
  }

  function checkEscalation(row: Parameters<typeof escalationPrompt>[0]) {
    const key = row.id.toString();
    if (announcedEscalation.has(key)) return;
    announcedEscalation.add(key);
    broadcast(escalationPrompt(row));
  }
}

/** One gentle reminder per stale needs_approval order, ~5 minutes after it was proposed. Call
 * this on an interval (see index.ts) - purely client-side, no Spacetime scheduled reducer needed. */
export function checkStaleOrders() {
  const now = Date.now();
  for (const [id, since] of pendingSince) {
    if (remindedOrders.has(id) || now - since < REMINDER_AFTER_MS) continue;
    remindedOrders.add(id);
    const order = st.orderById(BigInt(id));
    if (order) broadcast(`Still waiting on you: order #${id} (${order.part}/${order.supplier}, $${order.unitPrice.toFixed(0)}). Reply YES or NO when you get a chance.`);
  }
}
