// Phase 5H-B5 — apiFetch typed failures + bounded deadline (Node's runner).
import { test, afterEach } from "node:test";
import assert from "node:assert/strict";

import {
  apiFetch,
  ApiError,
  mutationOutcome,
  describeFailure,
} from "../api.ts";

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

function stubFetch(impl: (input: unknown, init: RequestInit) => Promise<Response>) {
  globalThis.fetch = impl as unknown as typeof fetch;
}

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

// --- mutationOutcome ----------------------------------------------------------
test("mutationOutcome: 4xx (incl. 409 conflict, 429 throttle) => failed", () => {
  for (const s of [400, 401, 403, 404, 409, 422, 429]) {
    assert.equal(mutationOutcome(new ApiError("http", s, "x")), "failed", `status ${s}`);
  }
});

test("mutationOutcome: 408 and 5xx => unknown (may have committed)", () => {
  for (const s of [408, 500, 502, 503]) {
    assert.equal(mutationOutcome(new ApiError("http", s, "x")), "unknown", `status ${s}`);
  }
});

test("mutationOutcome: transport failures => unknown", () => {
  for (const k of ["timeout", "offline", "network", "canceled"] as const) {
    assert.equal(mutationOutcome(new ApiError(k, 0, "x")), "unknown", k);
  }
  assert.equal(mutationOutcome(new Error("boom")), "unknown");
  assert.equal(mutationOutcome("nope"), "unknown");
});

// --- describeFailure --------------------------------------------------------
test("describeFailure: distinct copy per kind", () => {
  assert.equal(describeFailure(new ApiError("http", 422, "Amount must be > 0")), "Amount must be > 0");
  assert.match(describeFailure(new ApiError("timeout", 0, "")), /too long/i);
  assert.match(describeFailure(new ApiError("offline", 0, "")), /offline/i);
  assert.match(describeFailure(new ApiError("network", 0, "")), /reach the server/i);
  assert.match(describeFailure(new ApiError("canceled", 0, "")), /canceled/i);
  assert.match(describeFailure(new Error("x"), { verb: "save this" }), /save this/i);
});

// --- apiFetch: HTTP + success --------------------------------------------------
test("apiFetch: non-2xx => ApiError kind 'http' with status + server detail", async () => {
  stubFetch(async () => jsonResponse(422, { detail: "nope" }));
  await assert.rejects(apiFetch("/api/x", { method: "POST", body: "{}" }), (err: unknown) => {
    assert.ok(err instanceof ApiError);
    assert.equal(err.kind, "http");
    assert.equal(err.status, 422);
    assert.equal(err.message, "nope");
    return true;
  });
});

test("apiFetch: 2xx => parsed body", async () => {
  stubFetch(async () => jsonResponse(200, { a: 1 }));
  assert.deepEqual(await apiFetch("/api/x"), { a: 1 });
});

// --- apiFetch: deadline vs caller-cancel ------------------------------------
test("apiFetch: hung request rejects with kind 'timeout' at the deadline", async () => {
  stubFetch(
    (_input, init) =>
      new Promise((_res, rej) => {
        // Honour abort so the promise settles (as a real fetch would).
        (init.signal as AbortSignal).addEventListener("abort", () =>
          rej(new DOMException("aborted", "AbortError")),
        );
      }),
  );
  const t0 = Date.now();
  await assert.rejects(apiFetch("/api/slow", { timeoutMs: 40 }), (err: unknown) => {
    assert.ok(err instanceof ApiError);
    assert.equal(err.kind, "timeout");
    return true;
  });
  assert.ok(Date.now() - t0 < 1000, "should settle right after the deadline");
});

test("apiFetch: caller AbortSignal => kind 'canceled', DISTINCT from timeout", async () => {
  stubFetch(
    (_input, init) =>
      new Promise((_res, rej) => {
        (init.signal as AbortSignal).addEventListener("abort", () =>
          rej(new DOMException("aborted", "AbortError")),
        );
      }),
  );
  const ctrl = new AbortController();
  setTimeout(() => ctrl.abort(), 20);
  await assert.rejects(
    apiFetch("/api/slow", { timeoutMs: 5000, signal: ctrl.signal }),
    (err: unknown) => {
      assert.ok(err instanceof ApiError);
      assert.equal(err.kind, "canceled");
      return true;
    },
  );
});

test("apiFetch: fetch rejects (no navigator) => kind 'network', not 'offline'", async () => {
  stubFetch(async () => {
    throw new TypeError("Failed to fetch");
  });
  await assert.rejects(apiFetch("/api/x"), (err: unknown) => {
    assert.ok(err instanceof ApiError);
    assert.equal(err.kind, "network");
    return true;
  });
});

test("apiFetch: caller signal ALREADY aborted before the call => 'canceled' (even with timeoutMs 0)", async () => {
  stubFetch(
    (_input, init) =>
      new Promise((_res, rej) => {
        const s = init.signal as AbortSignal;
        if (s.aborted) rej(new DOMException("aborted", "AbortError"));
        else s.addEventListener("abort", () => rej(new DOMException("aborted", "AbortError")));
      }),
  );
  const ctrl = new AbortController();
  ctrl.abort();
  await assert.rejects(
    apiFetch("/api/x", { timeoutMs: 0, signal: ctrl.signal }),
    (err: unknown) => {
      assert.ok(err instanceof ApiError);
      assert.equal(err.kind, "canceled"); // caller-cancel wins over a 0ms deadline
      return true;
    },
  );
});

test("apiFetch: deadline fires while a present-but-not-aborting caller signal exists => 'timeout'", async () => {
  stubFetch(
    (_input, init) =>
      new Promise((_res, rej) => {
        (init.signal as AbortSignal).addEventListener("abort", () =>
          rej(new DOMException("aborted", "AbortError")),
        );
      }),
  );
  const ctrl = new AbortController(); // created, never aborted
  await assert.rejects(
    apiFetch("/api/slow", { timeoutMs: 30, signal: ctrl.signal }),
    (err: unknown) => {
      assert.ok(err instanceof ApiError);
      assert.equal(err.kind, "timeout");
      return true;
    },
  );
});

test("apiFetch: timeout vs caller-abort is deterministic across orderings", async () => {
  const hang = (_i: unknown, init: RequestInit) =>
    new Promise<Response>((_res, rej) => {
      (init.signal as AbortSignal).addEventListener("abort", () =>
        rej(new DOMException("aborted", "AbortError")),
      );
    });

  // caller aborts well before the deadline -> canceled, every run
  for (let i = 0; i < 5; i++) {
    stubFetch(hang as unknown as typeof fetch);
    const c = new AbortController();
    setTimeout(() => c.abort(), 5);
    await assert.rejects(
      apiFetch("/x", { timeoutMs: 500, signal: c.signal }),
      (e: unknown) => (e as ApiError).kind === "canceled",
    );
  }
  // deadline well before any caller action -> timeout, every run
  for (let i = 0; i < 5; i++) {
    stubFetch(hang as unknown as typeof fetch);
    const c = new AbortController();
    await assert.rejects(
      apiFetch("/x", { timeoutMs: 5, signal: c.signal }),
      (e: unknown) => (e as ApiError).kind === "timeout",
    );
  }
});
