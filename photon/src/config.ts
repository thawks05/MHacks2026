import "dotenv/config";

export const SPACETIME_HOST = process.env.SPACETIME_HOST ?? "http://127.0.0.1:3000";
export const SPACETIME_DB = process.env.SPACETIME_DB ?? "pdm";
export const SPECTRUM_PROVIDER = process.env.SPECTRUM_PROVIDER ?? "terminal";
export const SPECTRUM_PROJECT_ID = process.env.SPECTRUM_PROJECT_ID ?? "";
export const SPECTRUM_PROJECT_SECRET = process.env.SPECTRUM_PROJECT_SECRET ?? "";
export const ANTHROPIC_API_KEY = process.env.ANTHROPIC_API_KEY ?? "";
export const LLM_MODEL = process.env.LLM_MODEL ?? "claude-sonnet-4-5";

// The ONE person this bridge talks to. In imessage mode, anyone else gets a polite decline (see
// index.ts). In terminal mode there's no phone number to check, so the single terminal user is
// always treated as the owner - no concept of "other people" in dry-run.
export const OWNER_PHONE = process.env.OWNER_PHONE ?? "";

// How long an open interview waits for a reply before nudging once, then marking itself complete
// so the workflow never stalls on a human who's gone quiet.
export const INTERVIEW_TIMEOUT_MS = Number(process.env.INTERVIEW_TIMEOUT_MS ?? String(5 * 60 * 1000));
export const INTERVIEW_NUDGE_BEFORE_MS = Number(process.env.INTERVIEW_NUDGE_BEFORE_MS ?? String(60 * 1000));
