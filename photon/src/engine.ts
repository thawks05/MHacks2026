/** The conversation engine. Two modes:
 *  - Rule-based (default, zero external accounts): keyword/intent matching, calls the same tools.
 *  - Real LLM tool-calling (when ANTHROPIC_API_KEY is set): Claude decides which tool(s) to call.
 * Both only ever answer from tool results - never from invented numbers, matching the spec.
 * Strictly one-on-one: by the time a Turn reaches here, index.ts has already confirmed the sender
 * is the one operator this bridge talks to, so there is no role/permission check left to do. */
import Anthropic from "@anthropic-ai/sdk";
import * as tools from "./tools.js";
import * as local from "./localstore.js";
import * as st from "./spacetime.js";
import { ANTHROPIC_API_KEY, LLM_MODEL } from "./config.js";

export interface Turn {
  senderId: string;
  spaceId: string;
  text: string;
}

const PARTS = ["drive_gear", "belt", "motor_shaft"];

function mentionedPart(text: string): string | null {
  const t = text.toLowerCase();
  return PARTS.find((p) => t.includes(p) || t.includes(p.replace("_", " "))) ?? null;
}

function operatorName(senderId: string): string {
  return local.getPersonMemory(senderId).name ?? "Operator";
}

async function handleApproval(turn: Turn, orderId: number, vote: "approve" | "reject"): Promise<string> {
  const orderResult = tools.get_order(orderId);
  if (!orderResult.found || !orderResult.order) return `I don't see order #${orderId}.`;
  const order = orderResult.order;
  const name = operatorName(turn.senderId);
  if (vote === "approve") {
    await tools.approve_order(orderId, name);
    return `Order #${orderId} (${order.supplier}, $${order.unitPrice.toFixed(0)}) approved (simulated) by ${name}.`;
  } else {
    await tools.reject_order(orderId, name);
    return `Order #${orderId} rejected by ${name}.`;
  }
}

async function handleAmbiguousReply(): Promise<string> {
  const { orders } = tools.list_orders("needs_approval");
  if (orders.length === 1) {
    const o = orders[0];
    return `Just to confirm - do you want to approve or reject order #${o.id} (${o.part}/${o.supplier}, $${o.unitPrice.toFixed(0)})? A clear yes or no helps me get it right.`;
  }
  if (orders.length > 1) {
    return `I have ${orders.length} orders waiting on approval (#${orders.map((o) => o.id).join(", #")}) - which one, and approve or reject?`;
  }
  return "Not sure I caught that - could you say that another way?";
}

async function ruleBasedReply(turn: Turn): Promise<string | null> {
  const t = turn.text.toLowerCase().trim();

  if (t === "forget that") {
    local.forgetPerson(turn.senderId);
    return "Done - forgot what I knew about you.";
  }
  const callMe = turn.text.trim().match(/^call me (.+)$/i);
  if (callMe) {
    local.updatePersonMemory(turn.senderId, { name: callMe[1] });
    return `Got it, I'll call you ${callMe[1]}.`;
  }
  if (t.includes("keep it short")) {
    local.updatePersonMemory(turn.senderId, { detailLevel: "short" });
    return "Will do - keeping it brief from now on.";
  }

  if (/\b(maybe|perhaps|not sure|i guess|possibly|i don'?t know|dunno)\b/.test(t)) {
    return handleAmbiguousReply();
  }

  const approveMatch = t.match(/\b(approve|yes)\b.*?#?(\d+)|#?(\d+).*?\b(approve|yes)\b/);
  const rejectMatch = t.match(/\b(reject|no|cancel)\b.*?#?(\d+)|#?(\d+).*?\b(reject|no|cancel)\b/);
  if (approveMatch) {
    const id = parseInt(approveMatch[2] ?? approveMatch[3], 10);
    return handleApproval(turn, id, "approve");
  }
  if (rejectMatch) {
    const id = parseInt(rejectMatch[2] ?? rejectMatch[3], 10);
    return handleApproval(turn, id, "reject");
  }
  // Bare yes/no with exactly one order pending - still an explicit, unambiguous confirmation.
  if ((t === "yes" || t === "y" || t === "no" || t === "n") ) {
    const { orders } = tools.list_orders("needs_approval");
    if (orders.length === 1) {
      return handleApproval(turn, orders[0].id, t.startsWith("y") ? "approve" : "reject");
    }
  }

  if (t.includes("digest") || t.includes("overnight")) {
    const d = tools.get_overnight_digest();
    return d.summary + (d.openEscalations.length ? ` ${d.openEscalations.length} open escalation(s).` : "");
  }
  if (t.includes("agent status") || t.includes("are you alive") || t.includes("agent_status")) {
    const { agents } = tools.get_agent_status();
    if (!agents.length) return "No agent heartbeats recorded yet.";
    return agents.map((a) => `${a.agent} ${a.alive ? "alive" : "STALE"} (${a.secondsSinceHeartbeat.toFixed(0)}s ago)`).join(", ");
  }
  if (t.includes("pending order") || t.includes("what's pending")) {
    const { orders } = tools.list_orders("needs_approval");
    if (!orders.length) return "Nothing pending approval right now.";
    return orders.map((o) => `#${o.id} ${o.part}/${o.supplier} $${o.unitPrice.toFixed(0)}`).join("; ");
  }
  if (t.includes("order history") || t.includes("recent order")) {
    const { orders } = tools.list_orders();
    return orders.length ? orders.slice(0, 5).map((o) => `#${o.id} ${o.part}/${o.supplier} $${o.unitPrice.toFixed(0)} (${o.status})`).join("; ") : "No orders yet.";
  }
  const orderIdMatch = t.match(/order\s*#?(\d+)/);
  if (orderIdMatch) {
    const r = tools.get_order(parseInt(orderIdMatch[1], 10));
    if (!r.found || !r.order) return `No order #${orderIdMatch[1]}.`;
    return `#${r.order.id} ${r.order.part}/${r.order.supplier} $${r.order.unitPrice.toFixed(0)}, ${r.order.leadDays}d, ${r.order.status}. ${r.order.reason}`;
  }

  if (t.includes("why")) {
    const part = mentionedPart(t) ?? local.getSpaceMemory(turn.spaceId).lastPartDiscussed;
    if (!part) return "Why about which part?";
    return tools.explain_diagnosis(part).text;
  }

  const part = mentionedPart(t);
  if (part) {
    local.updateSpaceMemory(turn.spaceId, { lastPartDiscussed: part });
    if (t.includes("history")) {
      const h = tools.get_health_history(part);
      return h.rows.length ? `${part} recent readings: ${h.rows.map((r) => r.health.toFixed(0)).join(", ")}%` : `No history for ${part} yet.`;
    }
    // Free text about a part that isn't a direct status question - treat as an ad-hoc observation,
    // but only if there's an open incident for it to attach to (human_observation needs a real
    // observation_request_id - no orphan rows).
    if (!t.includes("health") && !t.includes("status") && !t.includes("how")) {
      const openReq = st.openObservationRequests().find((r) => r.part === part);
      if (openReq) {
        await tools.record_observation(Number(openReq.id), part, "general", turn.text, turn.text, operatorName(turn.senderId));
        return `Noted - logged that about ${part}. I'll factor it into the diagnosis.`;
      }
      return `Thanks, but there's no active incident open for ${part} right now, so I won't file that as a report. Let me know if something changes.`;
    }
    const r = tools.get_part_health(part);
    if (!r.found) return `No data for ${part} yet.`;
    return `${part} is at ${r.health?.toFixed(0)}% health (${r.state}, ${r.driftLoHz?.toFixed(0)}-${r.driftHiHz?.toFixed(0)}Hz band)${r.etaS ? `, ~${r.etaS.toFixed(0)}s estimate` : ""}.`;
  }

  if (t.includes("status") || t.includes("how's everything") || t.includes("how is everything")) {
    const s = tools.get_machine_status();
    return s.parts.map((p) => `${p.part} ${p.health.toFixed(0)}% (${p.state})`).join(", ") || "No data yet.";
  }

  return "I can tell you machine/part status, explain a diagnosis, list or approve orders, or log something you noticed. What do you need?";
}

const TOOL_DEFS: Anthropic.Tool[] = [
  { name: "get_machine_status", description: "Overall health of every tracked part", input_schema: { type: "object", properties: {} } },
  { name: "get_part_health", description: "Health of one specific part", input_schema: { type: "object", properties: { part: { type: "string" } }, required: ["part"] } },
  { name: "explain_diagnosis", description: "Why a part is flagged - the real reasoning", input_schema: { type: "object", properties: { part: { type: "string" } }, required: ["part"] } },
  { name: "list_orders", description: "List orders, optionally filtered by status", input_schema: { type: "object", properties: { status: { type: "string" } } } },
  { name: "get_order", description: "Get one order by id", input_schema: { type: "object", properties: { id: { type: "number" } }, required: ["id"] } },
  { name: "get_overnight_digest", description: "Summary of recent activity", input_schema: { type: "object", properties: {} } },
];

async function llmReply(turn: Turn): Promise<string> {
  const client = new Anthropic({ apiKey: ANTHROPIC_API_KEY });
  const toolImpls: Record<string, (input: any) => unknown> = {
    get_machine_status: () => tools.get_machine_status(),
    get_part_health: (i) => tools.get_part_health(i.part),
    explain_diagnosis: (i) => tools.explain_diagnosis(i.part),
    list_orders: (i) => tools.list_orders(i.status),
    get_order: (i) => tools.get_order(i.id),
    get_overnight_digest: () => tools.get_overnight_digest(),
  };
  const messages: Anthropic.MessageParam[] = [{ role: "user", content: turn.text }];
  for (let i = 0; i < 4; i++) {
    const resp = await client.messages.create({
      model: LLM_MODEL, max_tokens: 400, tools: TOOL_DEFS,
      system: "You are a plant maintenance concierge. Answer ONLY using tool results, never invent numbers. Max 3 sentences unless asked for more. You cannot approve or reject orders yourself - that always goes through the normal approve/reject flow, never make up a confirmation.",
      messages,
    });
    const toolUses = resp.content.filter((c): c is Anthropic.ToolUseBlock => c.type === "tool_use");
    if (toolUses.length === 0) {
      return resp.content.filter((c): c is Anthropic.TextBlock => c.type === "text").map((c) => c.text).join(" ");
    }
    messages.push({ role: "assistant", content: resp.content });
    messages.push({
      role: "user",
      content: toolUses.map((tu) => ({
        type: "tool_result" as const, tool_use_id: tu.id,
        content: JSON.stringify(toolImpls[tu.name]?.(tu.input) ?? { error: "unknown tool" }),
      })),
    });
  }
  return "Sorry, I'm having trouble pulling that together right now.";
}

export async function reply(turn: Turn): Promise<string | null> {
  if (ANTHROPIC_API_KEY) {
    try {
      return await llmReply(turn);
    } catch (e) {
      console.error("[engine] LLM call failed, falling back to rule-based:", e);
    }
  }
  return ruleBasedReply(turn);
}
