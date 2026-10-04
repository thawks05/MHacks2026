/** Mid-negotiation escalations - the Buyer writes one of these when a supplier counters, is over
 * budget, or offers a substitute. Photon presents it in plain language and parses the operator's
 * answer back via the real answer_escalation reducer (Step 2's tools.answer_escalation). Only
 * intercepts a message when it clearly answers the open escalation (accept/next/cancel) - anything
 * else falls through to the normal engine so an open escalation doesn't hijack unrelated chat. */
import * as st from "./spacetime.js";
import * as tools from "./tools.js";

export function openEscalation() {
  return st.openEscalations()[0] ?? null;
}

export function escalationPrompt(esc: ReturnType<typeof st.openEscalations>[number]): string {
  const options = JSON.parse(esc.optionsJson || "[]") as string[];
  return `${esc.offerText} What would you like to do? (${options.join(" / ")})`;
}

export async function handleEscalationReply(senderName: string, text: string): Promise<string | null> {
  const esc = openEscalation();
  if (!esc) return null;
  const t = text.toLowerCase();
  if (/\b(accept|yes|ok|go ahead|do it)\b/.test(t)) {
    await tools.answer_escalation(Number(esc.id), "accept", senderName);
    return "Got it - accepting that offer.";
  }
  if (/\b(next|try|another|different supplier|reject)\b/.test(t)) {
    await tools.answer_escalation(Number(esc.id), "next", senderName);
    return "Got it - trying the next best option.";
  }
  if (/\b(cancel|stop|skip|never mind)\b/.test(t)) {
    await tools.answer_escalation(Number(esc.id), "cancel", senderName);
    return "Got it - cancelling sourcing for this part.";
  }
  return null; // doesn't clearly answer it - let the normal engine handle this message
}
