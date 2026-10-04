# Photon / Plant Concierge — setup

Strictly one-on-one: this bridge talks to exactly one operator (`OWNER_PHONE` in `imessage`
mode; the single terminal user in dry-run). No group chat, no roles, no multi-person approvals.

## Run it right now, zero accounts needed

```bash
cd photon
npm install
cp .env.example .env
npm run dev
```

This runs against the **local** Spacetime server (`spacetime start`, matching the rest of the
project) using Spectrum's real `terminal` provider — a genuine part of the spectrum-ts library,
not a fake stub. It needs no Photon account at all.

Note: the terminal provider spawns an actual interactive TUI binary (`tuichat`) over a local
socket for a human to type into - it's not something you can drive by piping plain text into
stdin. For automated testing, the conversation engine (`src/engine.ts`, `src/interview.ts`) was
exercised directly against the real local Spacetime connection with a fake `send` callback - same
approach used earlier for the two-person-rule logic. That verified the real logic end-to-end;
only the TUI rendering itself (Spectrum's own library code, not ours) wasn't driven.

If you change `spacetimedb/src/index.ts`, regenerate the client bindings (gitignored, not
committed, since they're generated output):

```bash
cd pdm
spacetime generate --lang typescript --module-path spacetimedb --out-dir photon/src/module_bindings
```

## What's real vs. stubbed right now

- **Real**: Spacetime connection, proactive alerts, the structured 6-topic operator interview
  (sound/heat/obstruction/belt behavior/smell-visual/recent changes - LLM extraction if
  `ANTHROPIC_API_KEY` is set, keyword fallback otherwise, both tested end-to-end with
  out-of-order, multi-topic, free-form replies), `human_observation` and `escalation` writes (real
  Spacetime tables now, not local JSON), approve/reject (`set_order_status`, enforced at the
  reducer to `channel = imessage` only - verified by testing that a non-imessage channel is
  rejected outright), ambiguous-reply clarification ("maybe" asks instead of guessing), per-person
  memory (local JSON - name/detail-level/past decisions only), photo analysis (if you set
  `ANTHROPIC_API_KEY` - kept as a working bonus from the earlier build, not required by the current
  spec but harmless).
- **Stubbed**: voice note transcription isn't implemented (the concierge asks you to describe it
  in text). Mid-negotiation escalation is written by the Buyer agent (not built yet - that's a
  later step); `answer_escalation` is ready and tested on the Photon side.
- **Untested** (works in the code, but I could not exercise it in this environment): the real-LLM
  tool-calling and extraction path (no API key available here - the keyword fallback was what got
  tested, and it's what runs by default).

## Manual steps for YOU before this can run for real on iMessage

1. **Get a Photon/Spectrum project.** Go to the Photon dashboard (ask the sponsor booth at the
   hackathon if you don't have a link) and create a project. You need two values:
   `SPECTRUM_PROJECT_ID` and `SPECTRUM_PROJECT_SECRET`. Put them in `.env` — never commit them.
2. **Figure out how the project gets a phone line.** This genuinely isn't documented publicly as
   of writing. Ask the Photon booth directly — this is the one real unknown blocking a live
   iMessage demo.
3. **Set `OWNER_PHONE`** in `.env` to the one number this bridge should serve. Anyone else gets a
   polite decline.
4. **Send the first message yourself.** Likely pattern: you text the line first from your own
   phone, the bridge captures that as a `Space`, and pushes alerts into it afterward. Confirm this
   with the booth too.
5. **Switch the mode**: set `SPECTRUM_PROVIDER=imessage` in `.env` once you have the above.
6. **Alternative**: if the cloud line setup is unclear or slow, there's a local macOS path
   (`@spectrum-ts/imessage-local`) that reads your own Mac's Messages app directly — needs no
   Spectrum account at all, just Full Disk Access permission for Messages.

## Config you control

- `OWNER_PHONE` — the one person this bridge talks to (imessage mode only).
- `INTERVIEW_TIMEOUT_MS` / `INTERVIEW_NUDGE_BEFORE_MS` — how long an open interview waits before
  nudging once, then marking itself complete regardless so the workflow never stalls.
- `ANTHROPIC_API_KEY` / `LLM_MODEL` — optional. Without it, the concierge uses rule-based/keyword
  responders that still call every real tool correctly. With it, free-text understanding and
  interview-topic extraction get richer, and photo observations get analyzed automatically.
