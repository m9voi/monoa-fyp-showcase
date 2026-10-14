// Phase 5H-B5 / B6 — reconcilePendingCreates: a lost create is retired ONLY on a
// server "found: true" (or the TTL / the auth.tsx account-boundary wipe). A
// "found: false" NEVER prunes it (the backend handler can still commit after the
// client's deadline), and neither does an HTTP 401 or a transport failure — those
// mean "outcome not determinable right now", so reconciliation is suspended and
// the opaque record is kept for a same-user re-auth to resolve.
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

const { rememberPendingCreate, listPendingCreates } = await import("../pendingCreate.ts");
const { reconcilePendingCreates, reconcileMessage } = await import("../reconcile.ts");

const realFetch = globalThis.fetch;
let statusReply: { status: number; body: unknown } = { status: 200, body: { found: false } };

function stubStatus() {
  globalThis.fetch = (async (input: unknown) => {
    const url = String(input);
    if (url.includes("/api/idempotency/status")) {
      return {
        ok: statusReply.status >= 200 && statusReply.status < 300,
        status: statusReply.status,
        json: async () => statusReply.body,
      } as unknown as Response;
    }
    throw new Error("unexpected fetch " + url);
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

const OLD = () => Date.now() - 10_000; // past the 2s minAge

test("found:false does NOT prune the record — a late backend commit is still possible", async () => {
  rememberPendingCreate("transaction", "tok-late");
  // pretend the record was armed 10s ago so it is eligible for a check
  const rec = listPendingCreates()[0];
  rec.ts = OLD();
  ((globalThis as { window: { localStorage: MemStorage } }).window.localStorage).setItem(
    "coreos.pending.tok-late",
    JSON.stringify(rec),
  );

  statusReply = { status: 200, body: { found: false } };
  const r1 = await reconcilePendingCreates();
  assert.equal(r1.changed, false);
  assert.equal(listPendingCreates().length, 1, "record must survive a found:false");

  // ...the backend finally commits; a later reconcile recovers it and clears it
  statusReply = { status: 200, body: { found: true, kind: "transaction" } };
  const r2 = await reconcilePendingCreates();
  assert.deepEqual(r2.committed, ["transaction"]);
  assert.equal(r2.changed, true);
  assert.equal(listPendingCreates().length, 0);
});

test("a record younger than minAge is left untouched (a live submit may be racing)", async () => {
  rememberPendingCreate("task", "tok-fresh"); // ts = now
  statusReply = { status: 200, body: { found: true, kind: "task" } }; // would match IF queried
  const r = await reconcilePendingCreates();
  assert.equal(r.changed, false, "fresh record must not be queried/cleared");
  assert.equal(listPendingCreates().length, 1);
});

test("a foreign / different-user session (found:false) leaves the record and shows NO notice", async () => {
  // Scenario 2: account A armed a token; account B is now the session.
  // The status endpoint is user-scoped -> found:false for A's token.
  rememberPendingCreate("transaction", "tok-belongs-to-A");
  const rec = listPendingCreates()[0];
  rec.ts = OLD();
  ((globalThis as { window: { localStorage: MemStorage } }).window.localStorage).setItem(
    "coreos.pending.tok-belongs-to-A",
    JSON.stringify(rec),
  );
  statusReply = { status: 200, body: { found: false } };
  const r = await reconcilePendingCreates();
  assert.equal(r.changed, false); // B gets no recovery toast/message for A's token
  assert.deepEqual(r.committed, []);
  assert.equal(listPendingCreates().length, 1); // inert; not deleted; will TTL out
});

test("a 401 SUSPENDS reconciliation and KEEPS the record; a later same-user found:true retires it", async () => {
  // A create committed but its response was lost; then the session expired.
  rememberPendingCreate("event", "tok-401");
  const rec = listPendingCreates()[0];
  rec.ts = OLD();
  ((globalThis as { window: { localStorage: MemStorage } }).window.localStorage).setItem(
    "coreos.pending.tok-401",
    JSON.stringify(rec),
  );

  // 1. reconciliation runs while the session is already invalid -> 401.
  //    A 401 is NOT authoritative evidence about the mutation outcome, so the
  //    opaque recovery record MUST survive (auth.tsx keeps pending records
  //    across a bare session expiry; only explicit logout clears them).
  statusReply = { status: 401, body: { detail: "Not authenticated." } };
  const r1 = await reconcilePendingCreates();
  assert.equal(r1.changed, false);
  assert.equal(listPendingCreates().length, 1, "a reconciliation 401 must NOT retire the record");

  // 2. the SAME user re-authenticates and reconciliation resumes; the create
  //    had in fact committed -> now it is recovered and retired.
  statusReply = { status: 200, body: { found: true, kind: "event" } };
  const r2 = await reconcilePendingCreates();
  assert.deepEqual(r2.committed, ["event"]);
  assert.equal(r2.changed, true);
  assert.equal(listPendingCreates().length, 0, "retired only after an authoritative found:true");
});

test("a transport failure (network/timeout) also keeps the record for the next pass", async () => {
  rememberPendingCreate("task", "tok-neterr");
  const rec = listPendingCreates()[0];
  rec.ts = OLD();
  ((globalThis as { window: { localStorage: MemStorage } }).window.localStorage).setItem(
    "coreos.pending.tok-neterr",
    JSON.stringify(rec),
  );
  globalThis.fetch = (async () => {
    throw new Error("network down");
  }) as unknown as typeof fetch;
  const r = await reconcilePendingCreates();
  assert.equal(r.changed, false);
  assert.equal(listPendingCreates().length, 1, "a transport failure must not retire the record");
});

test("reconcileMessage: nothing changed -> empty; committed -> non-alarming line", () => {
  assert.equal(reconcileMessage({ committed: [], changed: false }), "");
  const m = reconcileMessage({ committed: ["transaction"], changed: true });
  assert.match(m, /already been saved/i);
  assert.doesNotMatch(m, /fail|error|lost/i);
});
