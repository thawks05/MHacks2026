/** Thin wrapper around the generated SpacetimeDB client bindings (src/module_bindings), verified
 * against the real installed `spacetimedb` package types - not guessed. Connects once at startup,
 * keeps a live subscription open, and exposes plain read helpers plus the write paths Photon is
 * allowed to use directly (approve/reject an order, log an interview answer, close out an
 * interview, answer an escalation). report_health/write_spectrum_reading/propose_order/
 * create_escalation stay exclusively the Python agents' job - Photon never writes those. */
import { DbConnection, type EventContext } from "./module_bindings/index.js";
import { SPACETIME_HOST, SPACETIME_DB } from "./config.js";

type Listener = () => void;
const listeners: Listener[] = [];

export function onAnyChange(cb: Listener) {
  listeners.push(cb);
}

function notify() {
  for (const cb of listeners) cb();
}

function wsUri(host: string): string {
  return host.replace(/^http:/, "ws:").replace(/^https:/, "wss:");
}

let connection: DbConnection | null = null;

export async function connectSpacetime(): Promise<DbConnection> {
  if (connection) return connection;
  connection = await new Promise<DbConnection>((resolve, reject) => {
    const conn = DbConnection.builder()
      .withUri(wsUri(SPACETIME_HOST))
      .withDatabaseName(SPACETIME_DB)
      .onConnectError((_ctx, error) => reject(error))
      .onConnect((conn) => {
        conn.subscriptionBuilder()
          .onApplied(() => {
            console.log("[spacetime] subscription applied - client cache ready");
            resolve(conn);
          })
          .subscribeToAllTables();
      })
      .build();

    conn.db.partHealth.onInsert(() => notify());
    conn.db.partHealth.onUpdate(() => notify());
    conn.db.orderProposal.onInsert(() => notify());
    conn.db.orderProposal.onUpdate(() => notify());
    conn.db.observationRequest.onInsert(() => notify());
    conn.db.observationRequest.onUpdate(() => notify());
  });
  return connection;
}

export function getConnection(): DbConnection {
  if (!connection) throw new Error("Spacetime not connected yet - call connectSpacetime() first");
  return connection;
}

// ---- read helpers, off the live client cache, no HTTP polling ----

export function latestHealth() {
  return [...getConnection().db.partHealth.iter()];
}

export function healthFor(part: string) {
  return latestHealth().find((r) => r.part === part) ?? null;
}

export function allOrders() {
  return [...getConnection().db.orderProposal.iter()].sort((a, b) => Number(b.id - a.id));
}

export function pendingOrders() {
  return allOrders().filter((o) => o.status === "needs_approval");
}

export function orderById(id: bigint) {
  return allOrders().find((o) => o.id === id) ?? null;
}

export function openObservationRequests() {
  return [...getConnection().db.observationRequest.iter()].filter((r) => r.status !== "complete");
}

export function observationRequestById(id: bigint) {
  return [...getConnection().db.observationRequest.iter()].find((r) => r.id === id) ?? null;
}

export function humanObservationsFor(observationRequestId: bigint) {
  return [...getConnection().db.humanObservation.iter()].filter(
    (r) => r.observationRequestId === observationRequestId
  );
}

export function openEscalations() {
  return [...getConnection().db.escalation.iter()].filter((r) => r.status === "open");
}

export function escalationById(id: bigint) {
  return [...getConnection().db.escalation.iter()].find((r) => r.id === id) ?? null;
}

export function agentStatuses() {
  return [...getConnection().db.agentStatus.iter()];
}

// ---- write helpers Photon is allowed to call directly ----

/** Resolves once the targeted row actually changes (or the call throws / times out), and always
 * removes its own listener - a permanent listener here would leak one per call, which is exactly
 * the bug this replaced (found during testing). */
function waitForRowChange<Row extends { id: bigint }>(
  table: { onUpdate: (cb: (ctx: EventContext, oldRow: Row, newRow: Row) => void) => void; removeOnUpdate: (cb: any) => void },
  id: bigint,
  fire: () => void,
  timeoutMs = 10_000
): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    let settled = false;
    const onUpdate = (_ctx: EventContext, _old: Row, row: Row) => {
      if (row.id !== id || settled) return;
      cleanup();
      resolve();
    };
    const timeout = setTimeout(() => {
      if (settled) return;
      cleanup();
      reject(new Error("Spacetime call timed out waiting for the row to update"));
    }, timeoutMs);
    function cleanup() {
      settled = true;
      clearTimeout(timeout);
      table.removeOnUpdate(onUpdate);
    }
    table.onUpdate(onUpdate);
    fire();
  });
}

/** Same idea as waitForRowChange but for a fresh insert, matched by predicate instead of id
 * (inserted rows don't have a known id until the server assigns one). Needed because reducer
 * calls are fire-and-forget over the wire - a caller that reads right after calling without
 * waiting for the round-trip will see nothing yet (found during testing: writeHumanObservation
 * calls followed immediately by a read came back empty every time). */
function waitForInsert<Row>(
  table: { onInsert: (cb: (ctx: EventContext, row: Row) => void) => void; removeOnInsert: (cb: any) => void },
  matches: (row: Row) => boolean,
  fire: () => void,
  timeoutMs = 10_000
): Promise<Row> {
  return new Promise<Row>((resolve, reject) => {
    let settled = false;
    const onInsert = (_ctx: EventContext, row: Row) => {
      if (settled || !matches(row)) return;
      cleanup();
      resolve(row);
    };
    const timeout = setTimeout(() => {
      if (settled) return;
      cleanup();
      reject(new Error("Spacetime insert timed out"));
    }, timeoutMs);
    function cleanup() {
      settled = true;
      clearTimeout(timeout);
      table.removeOnInsert(onInsert);
    }
    table.onInsert(onInsert);
    fire();
  });
}

/** The approval boundary: this is the ONLY place in the whole system that is allowed to call
 * set_order_status, and it always passes channel = "imessage" - the reducer rejects any other
 * value, so ASI:One is structurally incapable of approving/rejecting even with a code bug. */
export async function setOrderStatus(id: bigint, status: "approved" | "rejected", approvedBy: string): Promise<void> {
  // Reducer calls take ONE params object, not positional args - confirmed from the real installed
  // type (InferTypeOfParams resolves to a single object type). A positional call silently drops
  // every argument after the first, which is a real bug this fixed (found via testing - the first
  // argument alone got serialized as if it were the whole params object).
  const conn = getConnection();
  await waitForRowChange(conn.db.orderProposal, id, () => {
    conn.reducers.setOrderStatus({ id, status, updatedAt: Date.now() / 1000, approvedBy, channel: "imessage" });
  });
}

export async function writeHumanObservation(observationRequestId: bigint, part: string, field: string, value: string, rawText: string, reportedBy: string): Promise<void> {
  const conn = getConnection();
  const createdAt = Date.now() / 1000;
  await waitForInsert(
    conn.db.humanObservation,
    (row) => row.observationRequestId === observationRequestId && row.field === field && row.createdAt === createdAt,
    () => conn.reducers.writeHumanObservation({ observationRequestId, part, field, value, rawText, reportedBy, createdAt })
  );
}

export function heartbeat(agentName: string, note: string = "") {
  getConnection().reducers.heartbeat({ agentName, note, now: Date.now() / 1000 });
}

export async function completeObservationRequest(id: bigint): Promise<void> {
  const conn = getConnection();
  await waitForRowChange(conn.db.observationRequest, id, () => {
    conn.reducers.completeObservationRequest({ id, completedAt: Date.now() / 1000 });
  });
}

export async function answerEscalation(id: bigint, answer: string, answeredBy: string): Promise<void> {
  const conn = getConnection();
  await waitForRowChange(conn.db.escalation, id, () => {
    conn.reducers.answerEscalation({ id, answer, answeredBy, answeredAt: Date.now() / 1000 });
  });
}
