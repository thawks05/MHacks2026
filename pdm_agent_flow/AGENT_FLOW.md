# Agent flow: what changed and how to run it

## The flow

```
Pod -> Watcher (1s ticks) --PartAlert--> Analyst <--texts--> Photon bridge <--> operator's iPhone
                                            |  ^
                                SourceRequest|  |SourceResult / EscalationNeeded / PaymentCompleted
                                            v  |
                                          Buyer <--RFQ--> 7 supplier agents
Everything is written to Spacetime; the dashboard reads it from there.
```

1. **Watcher** runs `pipeline.py` + `dsp.py` (serial from the host ESP32, a recording, or the sim). It trains a 60-window baseline on startup, scores every 1 s window, and the moment the pod's status is anything other than `healthy` (watch, degraded, critical, from the pipeline's hold rule) it opens an incident and alerts the Analyst.
2. **Analyst** texts the operator: what's wrong, go look, text back what you see.
3. The operator's reply is saved as an `operator_note`. The Analyst diagnoses (sensor + notes) and texts numbered options. Clearing an obstruction or adjusting tension needs no purchase.
4. Repair/replace: the Analyst asks max price, max wait, price or speed. The **Buyer** gets quotes from all 7 suppliers. If the best one needs a decision (counteroffer, substitute, over limit), it goes back through the Analyst as a text.
5. The Analyst texts the comparison. **YES** approves (simulated payment), **NO** goes back to the options.
6. **RESOLVED** (or "fixed", "replaced", "cleared"...) at any point closes the incident. That is the only way an incident closes. The Watcher sees it, retrains the baseline for 60 s, and resumes.

Rules the code enforces:
- The Analyst is the only agent that writes texts. The bridge just sends and receives.
- `resolve_incident` and `decide_order_from_text` both refuse unless they point at a real inbound text, and the approval text must actually say yes/no.
- ASI:One chat is read-only (status, why, risk, order history). It can't approve or resolve.
- Health going back to green does not close an incident.

## Files

| File | What |
|---|---|
| `watcher_agent.py` | Rewritten around `pipeline.py`: runs it in a background thread, writes every window to Spacetime, opens incidents, sends "replace" to retrain after a resolve |
| `pipeline.py` | Your pipeline, two small changes: the sample loop is now `run()` so the Watcher can reuse it, and the sim can be marked fixed. The CLI works the same |
| `dsp.py` | Your file, unchanged |
| `analyst_agent.py` | Rewritten: phone conversation, diagnosis, ASI:One read-only Q&A |
| `buyer_agent.py` | Escalations go through the Analyst; links RFQ + order to the incident |
| `messages.py` | `incident_id` on PartAlert/SourceRequest; EscalationNeeded/EscalationAnswer |
| `store.py` | Your project's store.py plus new methods at the bottom (incidents, texts, pod mode, `report_pod_health`) |
| `spacetimedb/src/index.ts` | Your index.ts with 5 new tables + 10 reducers merged in (and added to `reset_demo`). Drop-in replacement |
| `photon_bridge/` | Mac-side iMessage bridge (Photon `imessage-kit`) |
| `run_all.py` | Starts suppliers, Buyer, Analyst, Watcher in one terminal |
| `test_flow.py` | End-to-end test with no Spacetime and no phone |
| `test_contract.py` | Checks every `store.py` call against the reducers and argument counts in `index.ts` |

## Run it (Mac)

```bash
# 0. Test first (pip3 install uagents numpy scipy pyserial)
python3 test_contract.py                  # Python <-> database calls line up
python3 test_flow.py                      # full purchase path, sim sensor
SCENARIO=clear python3 test_flow.py       # no-purchase path

# 1. Spacetime: replace spacetimedb/src/index.ts with the one in this zip, then republish
spacetime publish --server maincloud --module-path spacetimedb pdm

# 2. Agents (sim sensor by default; for the real rig add the serial lines)
export SPACETIME_HOST="https://maincloud.spacetimedb.com" SPACETIME_DB="pdm"
export SENSOR_SOURCE=serial SERIAL_PORT=/dev/cu.usbserial-0001 FS=1000   # real rig only
python3 run_all.py

# 3. Photon bridge, on the Mac signed into iMessage (Node 20+)
cd photon_bridge && npm install
export OPERATOR_PHONE="+15551234567"      # the phone that gets the texts
export SPACETIME_HOST="https://maincloud.spacetimedb.com" SPACETIME_DB="pdm"
npm start
```

Bridge gotchas: the operator's phone must be a different Apple ID than the Mac's, and the terminal running the bridge needs Full Disk Access (System Settings > Privacy & Security).

Sensor knobs (env vars, see the top of `watcher_agent.py`): `SENSOR_SOURCE` sim | serial | file, `SERIAL_PORT`, `BAUD`, `FS`, `COUNTS_PER_G`, `REPLAY_FILE`, `SIM_FAULT_AT`, `SIM_SPEED`. Baseline length and the watch/degraded/critical hold times live in `pipeline.py`. `SLOT_PARTS` in `watcher_agent.py` maps the pod's slot to the part name suppliers know (`drive_gear`). `health.py` and `ingest.py` are no longer used by the Watcher.

## Dashboard tables

`incident` is the spine: one row per failure with `rfq_id` and `order_id` linked. Pull its `operator_note` rows, `diagnosis` (its `observation_request_id` column now holds the incident id), `supplier_quote` by `rfq_id`, `order_proposal` by `order_id`. `pod_status` shows training vs monitoring. `outbound_message`/`inbound_message` are the full text thread.
