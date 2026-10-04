// Photon bridge: a dumb pipe between Spacetime and the operator's iMessage. No logic lives here.
//   outbound_message (written only by the Analyst)  -> iMessage to OPERATOR_PHONE
//   iMessage from OPERATOR_PHONE                    -> receive_text -> inbound_message
// The Analyst reads inbound_message and decides everything (observations, YES/NO, RESOLVED).
//
// Runs on a Mac signed into iMessage, using Photon's @photon-ai/imessage-kit (v3).
// The operator's phone must be a DIFFERENT Apple ID than the Mac's, or replies show up as
// "from me" and get ignored. The terminal running this needs Full Disk Access
// (System Settings -> Privacy & Security) so the kit can read the Messages database.
//
//   cd photon_bridge && npm install     (Node 20+)
//   export OPERATOR_PHONE="+15551234567"
//   export SPACETIME_HOST="https://maincloud.spacetimedb.com" SPACETIME_DB="pdm"
//   npx tsx bridge.ts

import { IMessageSDK, type Message } from '@photon-ai/imessage-kit';

const HOST = (process.env.SPACETIME_HOST ?? 'http://localhost:3000').replace(/\/$/, '');
const DB = process.env.SPACETIME_DB ?? 'pdm';
const OPERATOR = process.env.OPERATOR_PHONE ?? '';
if (!OPERATOR) throw new Error('Set OPERATOR_PHONE, e.g. +15551234567');

const digits = (s: string) => s.replace(/\D/g, '').slice(-10);   // match +1 (555) 123-4567 vs 5551234567

async function post(path: string, body: string, contentType = 'application/json'): Promise<any> {
  const r = await fetch(`${HOST}/v1/database/${DB}/${path}`, {
    method: 'POST', headers: { 'Content-Type': contentType }, body,
  });
  const txt = await r.text();
  if (!r.ok) throw new Error(`HTTP ${r.status} from ${path}: ${txt}`);
  return txt.trim() ? JSON.parse(txt) : null;
}

const call = (reducer: string, ...args: unknown[]) => post(`call/${reducer}`, JSON.stringify(args));

async function rows(query: string): Promise<Record<string, any>[]> {
  const out: Record<string, any>[] = [];
  for (const res of (await post('sql', query, 'text/plain')) ?? []) {
    const names = res.schema.elements.map((e: any) => e.name.some);
    for (const row of res.rows) out.push(Object.fromEntries(names.map((n: string, i: number) => [n, row[i]])));
  }
  return out;
}

const sdk = new IMessageSDK();
let sending = false;

async function flushOutbox() {
  if (sending) return;           // don't overlap if a send is slow
  sending = true;
  try {
    const pending = (await rows('SELECT * FROM outbound_message WHERE sent = false'))
      .sort((a, b) => Number(a.id) - Number(b.id));
    for (const m of pending) {
      await sdk.send({ to: OPERATOR, text: m.text });
      await call('mark_text_sent', m.id, Date.now() / 1000);
      console.log(`-> sent #${m.id}: ${String(m.text).split('\n')[0]}`);
    }
  } catch (e) {
    console.error('outbox error:', (e as Error).message);
  } finally {
    sending = false;
  }
}

async function onIncoming(msg: Message) {
  if (msg.isFromMe || !msg.text || !msg.participant) return;
  if (digits(msg.participant) !== digits(OPERATOR)) return;   // only the operator talks to the Analyst
  try {
    await call('receive_text', OPERATOR, msg.text, Date.now() / 1000);
    console.log(`<- received: ${msg.text}`);
  } catch (e) {
    console.error('receive_text failed:', (e as Error).message);
  }
}

await sdk.startWatching({
  onDirectMessage: onIncoming,
  onError: (e) => console.error('watcher error:', e.message),
});
setInterval(flushOutbox, 1000);
console.log(`Photon bridge up. Operator: ${OPERATOR}. Spacetime: ${HOST}/${DB}`);

process.on('SIGINT', async () => { await sdk.close(); process.exit(0); });
