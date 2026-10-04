/** Per-operator memory only (name/detail-level/past decisions), local JSON file, no Spacetime
 * involved - this is Photon-only UX state, not shared system-of-record data, so it's fine to keep
 * local. human_observation and escalation now live in real Spacetime tables (see spacetime.ts) -
 * they used to be stubbed here but that's no longer needed now the tables exist. */
import fs from "node:fs";

const DB_PATH = "./local_store.json";

interface PersonMemory {
  name?: string;
  detailLevel?: "short" | "normal" | "detailed";
  pastDecisions: string[];
}

interface SpaceMemory {
  lastPartDiscussed?: string;
}

interface LocalDb {
  personMemory: Record<string, PersonMemory>;
  spaceMemory: Record<string, SpaceMemory>;
}

function empty(): LocalDb {
  return { personMemory: {}, spaceMemory: {} };
}

function load(): LocalDb {
  if (!fs.existsSync(DB_PATH)) return empty();
  try {
    return { ...empty(), ...JSON.parse(fs.readFileSync(DB_PATH, "utf-8")) };
  } catch {
    return empty();
  }
}

function save(db: LocalDb) {
  fs.writeFileSync(DB_PATH, JSON.stringify(db, null, 2));
}

export function getPersonMemory(id: string): PersonMemory {
  const db = load();
  return db.personMemory[id] ?? { pastDecisions: [] };
}

export function updatePersonMemory(id: string, patch: Partial<PersonMemory>) {
  const db = load();
  db.personMemory[id] = { ...getPersonMemory(id), ...db.personMemory[id], ...patch };
  save(db);
}

export function addPastDecision(id: string, decision: string) {
  const db = load();
  const mem = db.personMemory[id] ?? { pastDecisions: [] };
  mem.pastDecisions = [...mem.pastDecisions, decision].slice(-20);
  db.personMemory[id] = mem;
  save(db);
}

export function forgetPerson(id: string) {
  const db = load();
  delete db.personMemory[id];
  save(db);
}

export function getSpaceMemory(spaceId: string): SpaceMemory {
  const db = load();
  return db.spaceMemory[spaceId] ?? {};
}

export function updateSpaceMemory(spaceId: string, patch: Partial<SpaceMemory>) {
  const db = load();
  db.spaceMemory[spaceId] = { ...getSpaceMemory(spaceId), ...db.spaceMemory[spaceId], ...patch };
  save(db);
}
