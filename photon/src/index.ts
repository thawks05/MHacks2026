/** Plant Concierge - Photon/Spectrum entry point. Real spectrum-ts app (terminal provider by
 * default = zero external accounts, swap to imessage once real Photon credentials exist). Shares
 * the same Spacetime rows as the Python agents - never duplicates the interview/approval logic,
 * just offers it through a different, more conversational channel. Strictly one-on-one: this
 * bridge talks to exactly one operator (OWNER_PHONE in imessage mode; the single terminal user in
 * dry-run) - no group chat, no roles, no multi-person approvals. */
import type Anthropic from "@anthropic-ai/sdk";
import { Spectrum } from "spectrum-ts";
import { terminal } from "spectrum-ts/providers/terminal";
import { imessage } from "spectrum-ts/providers/imessage";
import { connectSpacetime, heartbeat } from "./spacetime.js";
import { startProactiveWatch, registerSpace, checkStaleOrders } from "./proactive.js";
import { reply, type Turn } from "./engine.js";
import { handleInterviewReply, checkTimeouts } from "./interview.js";
import { handleEscalationReply } from "./escalation.js";
import * as local from "./localstore.js";
import { SPECTRUM_PROVIDER, SPECTRUM_PROJECT_ID, SPECTRUM_PROJECT_SECRET, OWNER_PHONE } from "./config.js";

async function main() {
  console.log(`[photon] connecting to Spacetime...`);
  await connectSpacetime();
  console.log(`[photon] connected. provider=${SPECTRUM_PROVIDER}`);

  const app =
    SPECTRUM_PROVIDER === "imessage"
      ? await Spectrum({
          projectId: SPECTRUM_PROJECT_ID,
          projectSecret: SPECTRUM_PROJECT_SECRET,
          providers: [imessage.config()],
        })
      : await Spectrum({ providers: [terminal.config()] });

  startProactiveWatch();
  setInterval(() => {
    checkTimeouts().catch((e) => console.error("[photon] interview timeout check failed:", e));
  }, 15_000);
  setInterval(() => checkStaleOrders(), 15_000);
  setInterval(() => heartbeat("photon"), 10_000);
  heartbeat("photon");
  console.log(`[photon] ready. ${SPECTRUM_PROVIDER === "terminal" ? "Type in this terminal to chat with the concierge." : "Listening for iMessages."}`);

  for await (const [space, message] of app.messages) {
    registerSpace(space);
    try {
      await handleMessage(space, message);
    } catch (e) {
      console.error("[photon] error handling message:", e);
      try {
        await space.send("Sorry, something went wrong on my end - try again?");
      } catch {
        /* best effort */
      }
    }
  }
}

async function handleMessage(space: any, message: any) {
  const senderId: string = message.sender?.id ?? "unknown";
  const spaceId: string = space.id;

  // One-on-one gate: in imessage mode, only OWNER_PHONE is served. The terminal provider has no
  // phone concept at all, so the single terminal user is always treated as the owner.
  if (SPECTRUM_PROVIDER === "imessage" && OWNER_PHONE && senderId !== OWNER_PHONE) {
    try {
      await space.send("Sorry, this line is only for the plant operator.");
    } catch {
      /* best effort */
    }
    return;
  }

  if (message.content.type === "text" || message.content.type === "markdown") {
    const text: string = message.content.text;
    const consumedByInterview = await handleInterviewReply(spaceId, senderId, text);
    if (consumedByInterview) return;
    const operatorName = local.getPersonMemory(senderId).name ?? "Operator";
    const escalationReply = await handleEscalationReply(operatorName, text);
    if (escalationReply) {
      await space.send(escalationReply);
      return;
    }
    const turn: Turn = { senderId, spaceId, text };
    const r = await reply(turn);
    if (r) await space.send(r);
    return;
  }

  if (message.content.type === "attachment") {
    await handlePhoto(space, senderId, message.content);
    return;
  }

  if (message.content.type === "voice") {
    await space.send("Got your voice note - I can't transcribe audio in this demo yet, could you describe it in text?");
    return;
  }
}

/** "Humans as sensors": a photo of a part gets sent to a vision-capable LLM to extract part /
 * symptom / severity, then logged via record_observation. Falls back to asking for text if no
 * ANTHROPIC_API_KEY is configured (no external account = no vision call possible, said honestly).
 * Kept as a working bonus from the earlier build - not part of this spec's required scope, but it
 * doesn't conflict with any rule here either. */
async function handlePhoto(space: any, senderId: string, content: { name: string; mimeType: string; read: () => Promise<Buffer> }) {
  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) {
    await space.send("Got your photo, but I can't analyze images without an LLM key configured in this demo - can you describe what you're seeing?");
    return;
  }
  const { default: Anthropic } = await import("@anthropic-ai/sdk");
  const client = new Anthropic({ apiKey });
  const bytes = await content.read();
  const resp = await client.messages.create({
    model: process.env.LLM_MODEL ?? "claude-sonnet-4-5",
    max_tokens: 200,
    messages: [{
      role: "user",
      content: [
        { type: "image", source: { type: "base64", media_type: content.mimeType as any, data: bytes.toString("base64") } },
        { type: "text", text: "This is a photo from a factory floor. In one short JSON object with keys part (one of drive_gear, belt, motor_shaft, or null), symptom (short description), severity (low/medium/high), describe what's shown. Only output the JSON." },
      ],
    }],
  });
  const text = resp.content.find((c): c is Anthropic.TextBlock => c.type === "text")?.text ?? "{}";
  try {
    const parsed = JSON.parse(text.match(/\{[\s\S]*\}/)?.[0] ?? "{}");
    if (!parsed.part) {
      await space.send("I see something but can't tell which part - which one is it?");
      return;
    }
    const name = local.getPersonMemory(senderId).name ?? "Operator";
    const st = await import("./spacetime.js");
    const openReq = st.openObservationRequests().find((r) => r.part === parsed.part);
    if (!openReq) {
      await space.send(`I see ${parsed.symptom ?? "something"} on the ${parsed.part}, but there's no active incident open for it right now, so I won't file a report.`);
      return;
    }
    const tools = await import("./tools.js");
    await tools.record_observation(Number(openReq.id), parsed.part, "smell_visual", parsed.symptom ?? "photo observation", parsed.symptom ?? "photo observation", name);
    await space.send(`Logged: ${parsed.part} - ${parsed.symptom} (severity: ${parsed.severity}).`);
  } catch {
    await space.send("Got the photo but couldn't make sense of it - can you describe what you're seeing?");
  }
}

main().catch((e) => {
  console.error("[photon] fatal error:", e);
  process.exit(1);
});
