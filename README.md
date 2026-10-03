# MHacks 2026 Team Brief: Predictive Maintenance Agents

**Event:** MHacks, Oct 3-4, 2026, Ann Arbor, 24 hours.
**One-liner:** A sensor rig listens to a machine's vibration frequencies, scores the health of each part against a healthy baseline, and a chain of AI agents decides what to replace and orders it from the best supplier, with one click or one text to approve.

---

## 1. Decisions already made

- **Product:** Manufacturer-side predictive maintenance (not the senior walker idea).
- **Main theme (must pick exactly one):** **Beyond the Code (Hardware)**. Your handbook says hackers must build under one of: Sustainability, Actually Intelligent (AI), FinTech, Beyond the Code (Hardware). We chose Hardware because the physical rig plus live fault injection is our best demo.
- **Opt-in themes (stackable):** Judged by an LLM (yes). Useless AI and Dumbest Idea (no).
- **Sponsor prizes to chase:**

| Priority | Prize | Payout (from the screenshots) | Notes |
|---|---|---|---|
| Core | Beyond the Code (Hardware) | $2,500 | Main theme |
| Core | Fetch.ai ASI:One Agent Challenge | $1,250 / $750 / $500 | Agents must be registered on Agentverse and discoverable via ASI:One. **Also requires a separate submission through the ASI Submission Agent, in addition to Devpost.** Avoid "thin chatbot" builds. Hackpack: https://www.fetch.ai/events/hackathons/mhacks-2026/hackpack |
| Add-on | Photon (agents in iMessage) | $700 first ($400 cash + $300 credits + fast-track interview), $300 second | Thin layer: agent texts an alert, human replies "approve" |
| Free | Judged by an LLM | n/a | Needs a clear README and repo |
| Only if cheap | Spacetime | $1,000 / $500 / $200 | Must be meaningfully used as the real-time backend (shared ops dashboard, agent coordination). Skip if it slows the core |
| Skipped | Relay, Capital One Nessie | n/a | Relay needs a separate app; our supplier data is mock |

Photon's site says it doubles prizes won with its SDK. We have not verified the terms; ask a Photon rep at the event.

---

## 2. Architecture

```
[Conveyor rig + sensor] --samples--> [Sensor agent: FFT + baseline]
                                          | PartHealth
                                          v
                                  [Analysis agent: which part, ETA to failure]
                                          | PartHealth
                                          v
                                  [Procurement agent: picks supplier]
                                          | OrderProposal
                                          v
              [Dashboard]   [Photon iMessage: alert + approve]   [ASI:One chat]
```

- Fetch.ai uAgents on Agentverse for the agent chain.
- Procurement auto-approves orders under a spend limit; above it, a human approves (shows autonomy plus trust).
- **All supplier data is MOCK.** Say so in the pitch.
- Time-to-failure is a rough linear **estimate**. Label it that way.

---

## 3. Data contract (agree on this first)

Defined in `contracts.py`:

- `PartHealth(part, health 0-100, drift_lo_hz, drift_hi_hz, eta_s | None, updated_at)`
- `OrderProposal(part, supplier, qty, unit_price, lead_days, reason, status, updated_at)` where status is `auto_approved | needs_approval | approved | rejected`

Plain dataclasses on purpose; port to `uagents.Model` with the same fields.

---

## 4. Split of work

| Owner | Scope |
|---|---|
| **Person A** | `health.py` (signal to health score), live ingest from the sensor, dashboard (live spectrum, per-part health bars, alert feed) |
| **Person B** | Fetch uAgents + Agentverse registration, procurement logic (`suppliers.py`), Photon alert/approve, ASI submission agent, Devpost |
| **Both** | README, demo script, rehearsal |

Swap owners if you prefer. Rules: one repo, a branch per owner, small commits every hour, `main` always demoable, 15-minute sync every ~3 hours.

---

## 5. Starter code (tested on simulated data)

Files in `pdm/`:

- `contracts.py`: message schemas.
- `health.py`: Baseline (fit on 60 s healthy signal, scores new chunks via band energies and z-scores), Trend (ETA estimate).
- `sim.py`: synthetic healthy/faulty signal so we can build before the rig is ready.
- `suppliers.py`: MOCK catalog, `rank()` and `propose()` with auto-approve limit.
- `demo_pipeline.py`: end to end: baseline, fault ramp, health drop, order proposal.
- `spacetimedb/index.ts`: the Spacetime tables (`part_health`, `health_log`, `order_proposal`) and reducers (`report_health`, `propose_order`, `set_order_status`, `reset_demo`). Type-checked, not yet run against a live server.
- `store.py`: Python helper that writes to and reads from Spacetime over HTTP. `smoke_test.py` exercises it.

Run it: `pip install numpy && python demo_pipeline.py`

**Still to do:**
- Live sensor ingest (needs the real sensor type and sample rate; set `FS` in `demo_pipeline.py`).
- Record the real rig (60 s healthy, then with the obstruction) and edit `PART_MAP` so frequency bands map to the right parts.
- Fetch agent wrappers around these functions.
- Photon integration and the ASI Submission Agent.

---

## 6. Demo plan (about 90 seconds)

1. Conveyor running healthy; dashboard green; baseline recorded for 60 s.
2. Add the obstruction (dried glue or a small piece of plastic on the gear) live.
3. A peak shifts in the spectrum, the part's health drops, and an alert fires.
4. The procurement agent proposes a supplier with its reasoning; approve by click or by iMessage reply.
5. Pitch: avoided downtime, replace parts only when needed.

**Fallbacks:** record a backup video of the full run; keep recorded data that can replay through the same pipeline if the rig fails live; make the obstruction repeatable.

---

## 7. Suggested 24-hour timeline

- **Hours 0-1:** Lock the contract, roles, repo; confirm the sensor and sample rate; ask an organizer or the handbook about AI-coding-tool rules.
- **Hours 1-6:** A: signal pipeline on sim, then live data. B: agent skeletons registered on Agentverse; procurement logic wired to the contract.
- **Hours 6-12:** Integrate the agent chain end to end; dashboard v1; record real baseline and fault data.
- **Hours 12-18:** Photon alert/approve; calibrate bands and thresholds on the real rig; polish.
- **Hours 18-21:** Rehearse the demo three times; record the backup video.
- **Hours 21-23:** README, Devpost, ASI submission via the Submission Agent, opt-in tracks.
- **Last hour:** Buffer only. No new features.

---

## 8. Tooling notes

- Pick one AI coding agent each, in the editor you are fastest in (Cursor or Claude Code both work). Paste `contracts.py` into every prompt and ask for one feature at a time.
- Make sure we can each explain every piece of code to judges.
- Use sponsor products where the prize requires them (Fetch.ai Agentverse/ASI:One, Photon), and follow their hackpack docs for setup.
- Use a chat assistant for README and Devpost writing.

---

## 9. Judging notes (what we found)

- Recent grand prizes (HackMIT 2026, TreeHacks 2026) went to physical devices with AI layered on top that solve real human problems. Cal Hacks 12's top prize went to a software agent project with an unusual interface.
- Hack the North 2026 scored on originality, UX, technical complexity and "wow," and wanted a live demo rather than slides. TreeHacks scored creativity, technical complexity and social impact.
- This is a small sample from press coverage, so treat these as patterns, not rules.

---

## 10. Open questions

1. What sensor and sample rate is the hardware teammate using (accelerometer or mic)?
2. Does MHacks allow AI coding assistants? (Check the handbook: https://safe-banon-80d.notion.site/2026-Hacker-Handbook-3ca24ca0c81b80fb8adee2e26c8508af)
3. Is Spacetime worth it, or do we skip it?

---

## 11. Spacetime setup (verified on Windows, CLI 2.10.2)

1. Install the CLI (Windows PowerShell): `iwr https://windows.spacetimedb.com -useb | iex`. In each new PowerShell window run `$env:Path += ";$HOME\AppData\Local\SpacetimeDB"` unless you have signed out and back in. Check with `spacetime --version`.
2. `spacetime init --lang typescript pdm` (pick npm, database name `pdm`). Then overwrite `pdm\spacetimedb\src\index.ts` with our `spacetimedb/index.ts`.
3. In a SECOND PowerShell window (keep it open): `spacetime start`. If it says it failed to bind to port 3000, run `wsl --shutdown` and retry.
4. From the `pdm` project folder: `spacetime publish --server local --module-path spacetimedb pdm`  (the flag is `--module-path`, not `--project-path`).
5. Extract the code zip to a DIFFERENT folder than the project (for example `Documents\pdm-code`), `cd` into it, and run `python smoke_test.py`.

`eta_s` is stored as -1 when there is no estimate. Argument order for reducer calls over HTTP follows the field order in `index.ts`.
