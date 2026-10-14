// Phase 5H-B5 — reconcile pending creates after a reload / restart.
//
// For every opaque pending record left behind by a create whose response was
// lost (see pendingCreate.ts), ask the server whether that token already
// committed. If it did, the UI can tell the user truthfully and refresh —
// instead of the user re-submitting and creating a duplicate.
//
// This module NEVER retires a record on its own except on a confirmed
// `found: true`. A `found: false` means "not committed yet" (the backend may
// still commit after the client's deadline). A transport failure or an HTTP 401
// means "cannot determine the outcome right now" — reconciliation is suspended,
// the opaque record is kept, and the next authenticated pass retries it. The
// account-boundary retirement (explicit logout / a verified different account)
// is owned solely by auth.tsx; the 15-minute TTL in pendingCreate.ts is the
// backstop. We never auto-retry the mutation itself (the body was never stored).

import { apiFetch } from "./api.ts";
import {
  listPendingCreates,
  forgetPendingCreate,
  type PendingKind,
} from "./pendingCreate.ts";

export interface ReconcileResult {
  /** Categories that were found already committed on the server. */
  committed: PendingKind[];
  /** true if any record resolved to "committed". */
  changed: boolean;
}

const KIND_LABEL: Record<PendingKind, string> = {
  transaction: "transaction",
  task: "task",
  event: "event",
  omni: "request",
};

/** A one-line, non-alarming summary for a toast, or "" when nothing changed. */
export function reconcileMessage(r: ReconcileResult): string {
  if (!r.changed) return "";
  const uniq = Array.from(new Set(r.committed.map((k) => KIND_LABEL[k])));
  const list =
    uniq.length === 1
      ? `a ${uniq[0]}`
      : `${uniq.slice(0, -1).join(", ")} and ${uniq[uniq.length - 1]} items`;
  return `Something you started earlier (${list}) had already been saved. It's up to date now.`;
}

// Concurrent callers (the global watcher + a page mount) share one run so the
// status endpoint is not hit twice for the same token.
let inFlight: Promise<ReconcileResult> | null = null;

/**
 * Check every pending record against the server.
 *   * `found: true`  -> the create committed: report it and drop the record.
 *   * `found: false` -> LEAVE the record. A client-side apiFetch deadline does
 *     NOT stop the backend handler / provider call, so a create whose response
 *     was lost may still commit seconds later. `false` means "not committed
 *     YET", never "never committed". A record is retired ONLY by: a later
 *     `found: true`, the form / Omni-bar confirming a real success or a truly
 *     definite rejection, the account-boundary wipe in auth.tsx, or the
 *     15-minute TTL in listPendingCreates().
 * Never throws — a failed check just leaves the record for the next attempt.
 */
export function reconcilePendingCreates(
  opts: { minAgeMs?: number; now?: number } = {},
): Promise<ReconcileResult> {
  if (inFlight) return inFlight;
  inFlight = _reconcile(opts).finally(() => {
    inFlight = null;
  });
  return inFlight;
}

async function _reconcile(
  opts: { minAgeMs?: number; now?: number } = {},
): Promise<ReconcileResult> {
  const minAgeMs = opts.minAgeMs ?? 2_000;
  const now = opts.now ?? Date.now();
  const committed: PendingKind[] = [];

  for (const rec of listPendingCreates(now)) {
    if (now - rec.ts < minAgeMs) continue; // too fresh — a live submit may be racing this
    try {
      const res = await apiFetch<{ found: boolean; kind?: string }>(
        "/api/idempotency/status",
        { method: "POST", body: JSON.stringify({ client_token: rec.token }), kind: "crud" },
      );
      if (res.found) {
        committed.push(rec.kind);
        forgetPendingCreate(rec.token);
      }
      // res.found === false -> keep the record; a late commit is still possible.
    } catch {
      // A 401 (session expired / invalid) or any transport failure means the
      // outcome cannot be determined right now — NOT that the create failed.
      // Suspend: leave the opaque record untouched so a same-user re-auth can
      // still reconcile it (and it TTLs out otherwise). Retiring here would
      // destroy the only identity able to recover a committed-but-unconfirmed
      // create, contradicting the auth-boundary policy (auth.tsx keeps pending
      // records across a bare session expiry; only an explicit logout or a
      // verified different account clears them). Never surfaced to the user.
    }
  }

  return { committed, changed: committed.length > 0 };
}
