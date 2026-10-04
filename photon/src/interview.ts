/** The structured 6-topic operator interview, strictly one-on-one. Starts when a new
 * observation_request opens (see proactive.ts), tracks which topics are answered, parses free
 * text (LLM if ANTHROPIC_API_KEY is set, keyword fallback otherwise - both work with zero
 * external accounts per hard rule 5) - multiple topics per message and out-of-order answers both
 * work since extraction isn't tied to "the last question asked." Stops and marks the
 * observation_request complete when all 6 topics are answered or after a timeout, with one nudge
 * before the hard timeout so the workflow never stalls. */
import Anthropic from "@anthropic-ai/sdk";
import * as st from "./spacetime.js";
import * as tools from "./tools.js";
import * as local from "./localstore.js";
import { ANTHROPIC_API_KEY, LLM_MODEL, INTERVIEW_TIMEOUT_MS, INTERVIEW_NUDGE_BEFORE_MS } from "./config.js";

export const TOPICS = ["sound", "heat", "obstruction", "belt_behavior", "smell_visual", "recent_changes"] as const;
export type Topic = (typeof TOPICS)[number];

const TOPIC_QUESTIONS: Record<Topic, string> = {
  sound: "Any unusual sound - grinding, squealing, clicking, rattling, or is it quiet?",
  heat: "Does the motor, gear housing, or bearing feel hot? Any temperature reading?",
  obstruction: "Anything stuck, jammed, or wrapped around the belt, rollers, or gear teeth?",
  belt_behavior: "Is the belt slipping, drifting sideways, jerking, or slowing down?",
  smell_visual: "Any burning smell, smoke, leaking oil/grease, metal shavings, or visible wear/cracks?",
  recent_changes: "Any recent changes - new load, a recently replaced part, a bump or spill, or a restart?",
};

interface OpenInterview {
  observationRequestId: bigint;
  part: string;
  answered: Set<Topic>;
  lastPromptAt: number;
  nudged: boolean;
  spaceId: string;
  send: (text: string) => Promise<void>;
}

const openInterviews = new Map<string, OpenInterview>(); // keyed by observationRequestId.toString()

function unansweredTopics(iv: OpenInterview): Topic[] {
  return TOPICS.filter((t) => !iv.answered.has(t));
}

export function interviewFor(spaceId: string): OpenInterview | null {
  // One-on-one, but more than one part could be mid-incident at once - prefer whichever this
  // space was most recently prompted about.
  let best: OpenInterview | null = null;
  for (const iv of openInterviews.values()) {
    if (iv.spaceId !== spaceId) continue;
    if (!best || iv.lastPromptAt > best.lastPromptAt) best = iv;
  }
  return best;
}

async function askNext(iv: OpenInterview) {
  const remaining = unansweredTopics(iv);
  if (!remaining.length) {
    await finish(iv);
    return;
  }
  const batch = remaining.slice(0, 2);
  iv.lastPromptAt = Date.now();
  iv.nudged = false;
  await iv.send(batch.map((t) => TOPIC_QUESTIONS[t]).join(" "));
}

async function finish(iv: OpenInterview) {
  openInterviews.delete(iv.observationRequestId.toString());
  try {
    await st.completeObservationRequest(iv.observationRequestId);
  } catch (e) {
    console.error("[interview] failed to mark observation_request complete:", e);
  }
}

export async function startInterview(
  observationRequestId: bigint, part: string, state: string, health: number, etaS: number,
  spaceId: string, send: (text: string) => Promise<void>
) {
  const key = observationRequestId.toString();
  if (openInterviews.has(key)) return; // already running - proactive.ts can fire more than once
  const iv: OpenInterview = { observationRequestId, part, answered: new Set(), lastPromptAt: Date.now(), nudged: false, spaceId, send };
  openInterviews.set(key, iv);
  const etaBit = etaS !== -1 ? ` Roughly ${Math.max(0, Math.round(etaS / 60))} min left (an estimate).` : "";
  await send(`Heads up: the ${part} is at ${health.toFixed(0)}% health (${state}).${etaBit} Can you tell me what you're seeing at the machine? Anything helps.`);
  await askNext(iv);
}

const EXTRACT_TOOL: Anthropic.Tool = {
  name: "extract_observations",
  description: "Extract any of the maintenance-interview topics present in the operator's message. Omit topics not mentioned.",
  input_schema: {
    type: "object",
    properties: Object.fromEntries(TOPICS.map((t) => [t, { type: "string", description: `Short value for ${t}` }])),
  },
};

async function extractWithLLM(text: string): Promise<Partial<Record<Topic, string>>> {
  const client = new Anthropic({ apiKey: ANTHROPIC_API_KEY });
  const resp = await client.messages.create({
    model: LLM_MODEL, max_tokens: 300,
    tools: [EXTRACT_TOOL], tool_choice: { type: "tool", name: "extract_observations" },
    system: "Extract only topics the operator actually mentioned, in their own words. If they say "
      + "'nothing unusual'/'nothing'/'all normal' in a way that answers whichever topic was just "
      + "asked, still record that topic with the value 'nothing unusual' - it's a valid answer.",
    messages: [{ role: "user", content: text }],
  });
  const toolUse = resp.content.find((c): c is Anthropic.ToolUseBlock => c.type === "tool_use");
  return (toolUse?.input as Partial<Record<Topic, string>>) ?? {};
}

const KEYWORD_FALLBACK: Record<Topic, RegExp> = {
  sound: /\b(grind|squeal|click|rattl|noise|loud|quiet|silent)\w*/i,
  heat: /\b(hot|warm|heat|temperature|cool|overheat)\w*/i,
  obstruction: /\b(stuck|jam|wrap|block|clear|obstruct|caught)\w*/i,
  belt_behavior: /\b(slip|drift|jerk|slow(?:ing)?|wobbl|belt)\w*/i,
  smell_visual: /\b(smell|smoke|burn|leak|oil|grease|shaving|crack|wear|visible)\w*/i,
  recent_changes: /\b(new load|replaced|swapped?|installed?|new belt|new gear|new part|bump|spill|restart|changed|change)\w*/i,
};

// Only matches when the WHOLE message is a terse negative reply (e.g. "nothing unusual") - not
// merely a message that happens to contain the word "nothing" alongside unrelated real content.
// Found via testing: "nothing stuck that I can see" legitimately answers obstruction via its own
// keyword, but a loose \bnothing\b search was then ALSO blanket-filling belt_behavior (the other
// topic asked in the same batch) with "nothing unusual", which the operator never actually said
// anything about - a real false-positive this fixed.
const NOTHING_PHRASE = /^(nothing( unusual| special| different)?|none|all normal|all good|looks fine|seems fine)\.?$/i;

function extractWithKeywords(text: string, askedTopics: Topic[]): Partial<Record<Topic, string>> {
  const out: Partial<Record<Topic, string>> = {};
  for (const topic of TOPICS) {
    if (KEYWORD_FALLBACK[topic].test(text)) out[topic] = text.trim();
  }
  if (NOTHING_PHRASE.test(text.trim())) {
    for (const topic of askedTopics) if (!(topic in out)) out[topic] = "nothing unusual";
  }
  return out;
}

/** Returns true if this turn was consumed by an open interview (caller should not fall through to
 * the general-purpose engine). */
export async function handleInterviewReply(spaceId: string, senderId: string, text: string): Promise<boolean> {
  const iv = interviewFor(spaceId);
  if (!iv) return false;

  const askedTopics = unansweredTopics(iv).slice(0, 2); // whichever topic(s) we most recently asked
  let extracted: Partial<Record<Topic, string>>;
  if (ANTHROPIC_API_KEY) {
    try {
      extracted = await extractWithLLM(text);
    } catch (e) {
      console.error("[interview] LLM extraction failed, falling back to keywords:", e);
      extracted = extractWithKeywords(text, askedTopics);
    }
  } else {
    extracted = extractWithKeywords(text, askedTopics);
  }

  const reportedBy = local.getPersonMemory(senderId).name ?? "Operator";
  const newlyAnswered: Topic[] = [];
  for (const topic of TOPICS) {
    const value = extracted[topic];
    if (!value || iv.answered.has(topic)) continue;
    await tools.record_observation(Number(iv.observationRequestId), iv.part, topic, value, text, reportedBy);
    iv.answered.add(topic);
    newlyAnswered.push(topic);
  }

  if (!newlyAnswered.length) {
    await iv.send("Got it - anything else you're noticing? (sound, heat, anything stuck, belt behavior, smell/visuals, or recent changes)");
    return true;
  }
  await askNext(iv);
  return true;
}

/** Periodic timeout/nudge check - call this on an interval from index.ts. */
export async function checkTimeouts() {
  const now = Date.now();
  for (const iv of [...openInterviews.values()]) {
    const elapsed = now - iv.lastPromptAt;
    if (elapsed >= INTERVIEW_TIMEOUT_MS) {
      await finish(iv);
    } else if (!iv.nudged && elapsed >= INTERVIEW_TIMEOUT_MS - INTERVIEW_NUDGE_BEFORE_MS) {
      iv.nudged = true;
      await iv.send("Still there? Let me know what you're seeing when you get a chance - I'll go ahead with what I have in a bit either way.");
    }
  }
}
