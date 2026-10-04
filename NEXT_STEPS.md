# PDM — What's done, what's next

_Last updated: 2026-10-04_

This supersedes `PROJECT_STATUS.md` (that file describes the earlier, since-reversed
auto-approve/group-chat build). The project was rebuilt against a new master spec: **no
auto-approval at any price, approval only via Photon/iMessage, strictly one-on-one operator
chat, a structured interview, and a real multi-supplier negotiation flow.**

## Done

**Step 1 — Watcher.** `ingest.py` (sensor-source seam, sim today), `health.py` state thresholds,
Spacetime gained `state` on `part_health`/`health_log`, plus `spectrum_reading` and
`observation_request` (opens once per incident, idempotent). Verified live.

**Step 2 — Photon.** Deleted all group-chat/roles code. One operator only (`OWNER_PHONE`). New
structured 6-topic interview (`photon/src/interview.ts`) with LLM extraction + keyword fallback.
Order approval moved into the database: `set_order_status` now **requires `channel=imessage`** —
ASI:One is structurally incapable of approving. Verified live against the real local DB (full
interview, channel enforcement, ambiguous-reply clarification).

**Step 3 — Analyst.** Deleted every direct approve/reject code path and the old $150 auto-approve
branch. New diagnosis step cites real operator observations (or says honestly there are none yet),
offers 2–4 options (some need no purchase). Post-sourcing, Analyst is read-only and refuses to
approve from chat. Verified via direct function tests.

**Step 4 — Buyer + 7-supplier swarm.** `suppliers_swarm.py`: 7 personality-driven agents
(always-quote ×3, counter, slow, out-of-stock, counter+substitute) replacing the single mock
supplier. `buyer_agent.py` rewritten: fans an RFQ out to all 7, waits up to 8s, scores real
replies, proposes `needs_approval` or escalates a negotiation point to Photon and resumes once
answered. **Just cut over live** (schema published, all agents restarted) — found and fixed a real
bug where uAgents' `Bureau` overwrites each member agent's individual endpoint with one shared
one, which was blocking the swarm from being reachable at all. End-to-end RFQ round-trip test was
in progress when this session ended — **not yet confirmed working**, see "Next" below.

**Step 5 — Approval docs.** README explains why approval is iMessage-only.

**Step 6 — Spacetime cleanup (mostly).** Added `agent_event` timeline + `agent_status` heartbeats,
wired into all agents + Photon. Deliberately skipped SpacetimeDB's scheduled-reducer feature (an
exotic, barely-documented API) in favor of Photon doing the same "5-minute stale order reminder"
client-side — same behavior, built faster/more reliably.

## Next

1. **Finish confirming Step 4 live.** Re-run the RFQ test (send a `SourceRequest` for `belt` to the
   Buyer, confirm a `SourceResult` comes back referencing a real order or an escalation) now that
   the Bureau endpoint bug is fixed. Command used for this:
   `python3 /path/to/test_rfq.py` (sends a SourceRequest, listens for SourceResult) — or just
   trigger it for real via ASI:One's Form card after an incident.
2. **Confirm the negotiation/escalation path for real** — FlexDrive (counters near budget) or
   MetroSupply (substitute part) should trigger an `escalation` row; Photon should proactively text
   it and resolve "accept/next/cancel".
3. **Step 7 — scripted test suite.** Not built yet: a script driving all 11 scenarios from the
   original spec (incident→one text, full 6-topic interview, diagnosis citing observations,
   7-supplier quotes, counteroffer escalation, iMessage-only approval enforcement, unknown-number
   decline, ambiguous-reply clarification, memory across sessions, cross-channel status sync),
   printing PASS/FAIL for each.
4. **Step 8 — dashboard.** Explicitly deferred. Needs its own round of design questions with the
   user before any work starts, and only after Step 7 passes.
5. **Cleanup**: `supplier_agent.py` (the old single mock supplier) is now fully superseded by
   `suppliers_swarm.py` — safe to delete once Step 4 is confirmed solid.
6. **Hardware**: real sensor ingest (`ingest.py`'s `serial`/`replay` branches) still needs the
   actual rig's data format from the hardware teammate — fully out of scope until then.
7. **Go-live checklist** (for the real iMessage demo, not yet started): Photon line + restrict its
   Users tab to one number, Agentverse mailbox registration already done for the Analyst, Pablo
   publishes the final `spacetimedb/src/index.ts` to Maincloud (never done by anyone else), USB
   cable + a real 60s healthy-baseline recording.

## Hard rules still in force (do not relax without the user's explicit say)
- Never publish to or reset the Maincloud `pdm` database — local `spacetime start` only.
- No auto-approval, ever, at any price.
- Approval/rejection only via Photon, enforced at the reducer level (`channel=imessage`).
- Everything simulated, clearly labeled; no fake links; no invented numbers.
