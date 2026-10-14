// Phase 5H-B5 — the CLIENT auth-transition -> pending-recovery contract.
//
// Exercises the exact policy function AuthProvider calls
// (pendingActionForAuthTransition) composed with the real localStorage-backed
// pendingCreate store and the real reconcilePendingCreates(), driving the full
// session-expiry recovery sequence the reviewer requires. This is the smallest
// layer available (node --test) that runs the real client logic — no React
// renderer is bundled in this project.
import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";

class MemStorage {
  private m = new Map<string, string>();
  get length() {
    return this.m.size;
  }
  key(i: number) {
    return Array.from(this.m.keys())[i] ?? null;
  }
  getItem(k: string) {
    return this.m.has(k) ? this.m.get(k)! : null;
  }
  setItem(k: string, v: string) {
    this.m.set(k, String(v));
  }
  removeItem(k: string) {
    this.m.delete(k);
  }
  clear() {
    this.m.clear();
  }
}
(globalThis as { window?: unknown }).window = { localStorage: new MemStorage() };

const {
  rememberPendingCreate,
  listPendingCreates,
  clearAllPendingCreates,
  pendingActionForAuthTransition,
} = await import("../pendingCreate.ts");
const { reconcilePendingCreates } = await import("../reconcile.ts");

// Faithful replica of AuthProvider.applyAuthTransition — one line, verbatim.
function applyAuthTransition(
  prevUserId: string | null | undefined,
  event: Parameters<typeof pendingActionForAuthTransition>[1],
) {
  if (pendingActionForAuthTransition(prevUserId, event) === "clear") {
    clearAllPendingCreates();
  }
}

const realFetch = globalThis.fetch;
let statusReply: { status: number; body: unknown } = { status: 200, body: { found: false } };
function stubStatus() {
  globalThis.fetch = (async (input: unknown) => {
    if (String(input).includes("/api/idempotency/status")) {
      return {
        ok: statusReply.status >= 200 && statusReply.status < 300,
        status: statusReply.status,
        json: async () => statusReply.body,
      } as unknown as Response;
    }
    throw new Error("unexpected fetch");
  }) as unknown as typeof fetch;
}

beforeEach(() => {
  ((globalThis as { window: { localStorage: MemStorage } }).window.localStorage).clear();
  statusReply = { status: 200, body: { found: false } };
  stubStatus();
});
afterEach(() => {
  globalThis.fetch = realFetch;
});

function age(token: string, ms: number) {
  const rec = listPendingCreates()[0];
  rec.ts = Date.now() - ms;
  ((globalThis as { window: { localStorage: MemStorage } }).window.localStorage).setItem(
    "coreos.pending." + token,
    JSON.stringify(rec),
  );
}

// --------------------------------------------------------------------------- //
// SESSION-EXPIRY: the record survives 401, survives same-user re-auth, and is
// retired ONLY when found:true is confirmed.
// --------------------------------------------------------------------------- //
test("session expiry -> same-user re-auth -> found:true is the whole recovery path", async () => {
  // 1-2. authenticated user A armed a create; its response was lost.
  rememberPendingCreate("transaction", "tok-A-1");
  age("tok-A-1", 10_000); // eligible for a reconcile check

  // 3-5. the session check returns 401 -> unauthenticated. Record MUST survive.
  applyAuthTransition("usr_A", { kind: "expired" });
  assert.equal(listPendingCreates().length, 1, "401/session-expiry must NOT clear the record");

  // (while unauthenticated, AuthProvider does not run reconcile at all)

  // 6-7. the SAME user A signs back in. Still a "keep".
  applyAuthTransition("usr_A", { kind: "authenticated", userId: "usr_A" });
  assert.equal(listPendingCreates().length, 1, "same-user re-auth must NOT clear the record");

  // 8-9. reconciliation now runs; the create had in fact committed.
  statusReply = { status: 200, body: { found: true, kind: "transaction" } };
  const r = await reconcilePendingCreates();
  assert.deepEqual(r.committed, ["transaction"]); // recovery notice/state is produced
  assert.equal(r.changed, true);

  // 10. the record is retired — and ONLY because found:true was confirmed.
  assert.equal(listPendingCreates().length, 0);
});

test("session expiry with a found:false in between still does not retire the record", async () => {
  rememberPendingCreate("event", "tok-A-2");
  age("tok-A-2", 10_000);
  applyAuthTransition("usr_A", { kind: "expired" });

  // a same-user re-auth, then a reconcile that comes back found:false
  // (the backend handler may still commit later) -> record kept.
  applyAuthTransition("usr_A", { kind: "authenticated", userId: "usr_A" });
  statusReply = { status: 200, body: { found: false } };
  const r1 = await reconcilePendingCreates();
  assert.equal(r1.changed, false);
  assert.equal(listPendingCreates().length, 1);

  // ...later it commits -> next reconcile recovers + retires it.
  statusReply = { status: 200, body: { found: true, kind: "event" } };
  const r2 = await reconcilePendingCreates();
  assert.deepEqual(r2.committed, ["event"]);
  assert.equal(listPendingCreates().length, 0);
});

// --------------------------------------------------------------------------- //
// EXPLICIT LOGOUT remains a hard clear (regression guard).
// --------------------------------------------------------------------------- //
test("explicit logout deletes every pending record", () => {
  rememberPendingCreate("transaction", "t1");
  rememberPendingCreate("task", "t2");
  applyAuthTransition("usr_A", { kind: "logout" });
  assert.equal(listPendingCreates().length, 0);
});

test("session expiry and explicit logout are NOT the same event", () => {
  rememberPendingCreate("task", "t-keep");
  applyAuthTransition("usr_A", { kind: "expired" });
  assert.equal(listPendingCreates().length, 1, "expiry keeps");
  applyAuthTransition("usr_A", { kind: "logout" });
  assert.equal(listPendingCreates().length, 0, "logout clears");
});

// --------------------------------------------------------------------------- //
// DIFFERENT-ACCOUNT: a verified A -> B switch clears before B sees any UX.
// --------------------------------------------------------------------------- //
test("verified switch to a different user clears the previous account's records", () => {
  rememberPendingCreate("transaction", "tok-A-3");
  // AuthProvider still holds A in memory; B now authenticates.
  applyAuthTransition("usr_A", { kind: "authenticated", userId: "usr_B" });
  assert.equal(listPendingCreates().length, 0);
});

test("cold reload (no in-memory previous id) does NOT clear — records stay inert until TTL", async () => {
  rememberPendingCreate("event", "tok-A-4");
  age("tok-A-4", 10_000);
  // A closed the tab; a fresh page load authenticates as B; prev id is unknown.
  applyAuthTransition(undefined, { kind: "authenticated", userId: "usr_B" });
  assert.equal(listPendingCreates().length, 1, "unverifiable -> keep");
  // ...and B's user-scoped reconcile finds nothing for A's token -> no UX.
  statusReply = { status: 200, body: { found: false } };
  const r = await reconcilePendingCreates();
  assert.equal(r.changed, false);
  assert.deepEqual(r.committed, []);
  assert.equal(listPendingCreates().length, 1, "still inert; will TTL out");
});
