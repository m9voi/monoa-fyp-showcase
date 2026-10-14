// Phase 5H-B5 — the ONLY thing CoreOS persists to recover a create whose
// response was lost across a page reload / app restart.
//
// WHAT IS STORED (per pending create, keyed by the opaque token):
//   { token, kind, ts }
//     token : the same opaque client_token the create POST carried (a UUID)
//     kind  : "transaction" | "task" | "event" | "omni"  — a category word only
//     ts    : epoch ms the record was written (for expiry)
//
// WHAT IS NEVER STORED: amounts, descriptions, titles, notes, locations,
// coordinates, dates, AI prompt text — nothing from the request body. The token
// is opaque and carries no data (see idempotency.py / clientToken.ts). On
// reload the token lets CoreOS ASK the server "did this commit?" (see
// reconcile.ts) — it is not a client-side shadow copy of the mutation.
//
// Threat model: an attacker who can run script in this origin can already issue
// authenticated same-origin requests with the live session (the HttpOnly cookie
// need never be read). This record therefore adds only NEGLIGIBLE incremental
// information (a category word + "a create is pending") and NO new capability
// beyond that already-compromised context — which is why storage is kept this
// minimal and holds nothing from any request body.
//
// A record leaves storage on ONE of these state transitions (never on a
// wall-clock timer other than the TTL):
//   * the server confirms the create committed (reconcile -> found:true);
//   * the create form / Omni-bar confirms a real success or a truly definite
//     rejection (a 4xx that is NOT the 409 "already committed with different
//     details");
//   * EXPLICIT logout — the user intentionally ends the session on this
//     browser (auth.tsx logout());
//   * a VERIFIED switch to a different account — the in-memory previous user id
//     differs from the newly authenticated one (isDifferentAccount());
//   * the 15-minute TTL in listPendingCreates().
//
// A bare loss of authentication (an expired / invalid session -> anonymous)
// does NOT clear anything: the user did not abandon the mutation, and the
// opaque token is the only thing that can still reconcile a late commit once
// the SAME user re-authenticates. While anonymous, reconciliation is simply
// suspended. A foreign or absent session querying one of these tokens gets
// found:false (the status endpoint is user-scoped), so a stale record left by
// another account is inert — it can never surface a recovery notice for the
// wrong user — and expires at the TTL.

export type PendingKind = "transaction" | "task" | "event" | "omni";

export interface PendingCreate {
  token: string;
  kind: PendingKind;
  ts: number;
}

const PREFIX = "coreos.pending.";
const MAX_AGE_MS = 15 * 60 * 1000; // 15 min — after this a lost create is stale
const TOKEN_RE = /^[A-Za-z0-9._~:-]{1,64}$/;

function store(): Storage | null {
  try {
    return typeof window !== "undefined" ? window.localStorage : null;
  } catch {
    return null; // private mode / storage disabled
  }
}

export function rememberPendingCreate(kind: PendingKind, token: string): void {
  if (!TOKEN_RE.test(token)) return;
  const s = store();
  if (!s) return;
  try {
    s.setItem(
      PREFIX + token,
      JSON.stringify({ token, kind, ts: Date.now() } satisfies PendingCreate),
    );
  } catch {
    /* quota / disabled — recovery is best-effort */
  }
}

export function forgetPendingCreate(token: string): void {
  const s = store();
  if (!s) return;
  try {
    s.removeItem(PREFIX + token);
  } catch {
    /* ignore */
  }
}

/** All non-expired pending records; expired ones are pruned as a side effect. */
export function listPendingCreates(now: number = Date.now()): PendingCreate[] {
  const s = store();
  if (!s) return [];
  const out: PendingCreate[] = [];
  const expired: string[] = [];
  for (let i = 0; i < s.length; i++) {
    const key = s.key(i);
    if (!key || !key.startsWith(PREFIX)) continue;
    try {
      const rec = JSON.parse(s.getItem(key) || "") as PendingCreate;
      if (
        !rec ||
        typeof rec.token !== "string" ||
        !TOKEN_RE.test(rec.token) ||
        typeof rec.ts !== "number"
      ) {
        expired.push(key);
        continue;
      }
      if (now - rec.ts > MAX_AGE_MS) expired.push(key);
      else out.push(rec);
    } catch {
      expired.push(key);
    }
  }
  for (const k of expired) {
    try {
      s.removeItem(k);
    } catch {
      /* ignore */
    }
  }
  return out;
}

/**
 * True ONLY for a verified switch to a different account: both ids are known
 * and differ. An unknown previous identity (a cold reload) or a transition to
 * anonymous (session expiry) returns false — those must NOT wipe the opaque
 * recovery records (they are inert under a foreign/absent session and expire
 * at the TTL; a same-user re-auth still needs them).
 */
export function isDifferentAccount(
  prevUserId: string | null | undefined,
  nextUserId: string | null | undefined,
): boolean {
  return !!prevUserId && !!nextUserId && prevUserId !== nextUserId;
}

export type AuthTransition =
  | { kind: "authenticated"; userId: string } //  a session is (re)established
  | { kind: "expired" } //                        session check returned 401
  | { kind: "logout" }; //                        the user pressed sign-out

/**
 * The SINGLE source of truth for what an auth transition does to the pending
 * create-recovery records. AuthProvider calls exactly this at every transition.
 *
 *   "clear" -> wipe every pending record (a real account boundary)
 *   "keep"  -> leave them (they expire at the TTL, or are retired by a
 *              confirmed found:true / a definite create outcome)
 *
 * Explicit logout is an intentional account boundary -> clear.
 * A bare session expiry is NOT abandonment -> keep (reconciliation is simply
 * suspended while unauthenticated; a same-user re-auth still needs the token).
 * A new authentication clears only when it is a VERIFIED switch to a different
 * user id (an unverifiable cold-reload identity stays "keep").
 */
export function pendingActionForAuthTransition(
  prevUserId: string | null | undefined,
  event: AuthTransition,
): "keep" | "clear" {
  if (event.kind === "logout") return "clear";
  if (event.kind === "expired") return "keep";
  return isDifferentAccount(prevUserId, event.userId) ? "clear" : "keep";
}

/** Wipe every pending record — call on EXPLICIT logout or a verified account switch. */
export function clearAllPendingCreates(): void {
  const s = store();
  if (!s) return;
  const keys: string[] = [];
  for (let i = 0; i < s.length; i++) {
    const key = s.key(i);
    if (key && key.startsWith(PREFIX)) keys.push(key);
  }
  for (const k of keys) {
    try {
      s.removeItem(k);
    } catch {
      /* ignore */
    }
  }
}
