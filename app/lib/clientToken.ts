// Phase 5H-B3 — one opaque idempotency token per logical create submission.
//
// Generate it ONCE when a create form opens (or before its first submit), keep
// it across any retry after a slow / lost response, and let it fall out of scope
// when the form unmounts (a genuinely new operation gets a fresh token). The
// backend, not this value, is the authority: it dedupes on (user_id, token) and
// rejects a token reused with a materially different payload (HTTP 409).
//
// SCOPE: this identity lives IN MEMORY only. It covers in-session retries
// (double-submit, slow/lost response, StrictMode replay). It does NOT survive a
// full page reload / app restart between a committed-but-lost response and the
// user's next attempt — a privacy-safe cross-restart recovery (server-state
// reconciliation / truthful recovery UX) is deferred to Phase 5H-B5. Do NOT put
// prompt / financial / location text into localStorage/sessionStorage to close
// that gap.

/** An opaque, URL-safe token accepted by the backend's `client_token` contract. */
export function newClientToken(): string {
  try {
    const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto;
    if (c?.randomUUID) return c.randomUUID();
  } catch {
    /* fall through to the non-crypto fallback */
  }
  return `ct-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
}

// --- Omni-bar token lifecycle (the dashboard command bar is not a modal, so it
// cannot rely on unmount to rotate the token) -------------------------------- //

/**
 * The token to use for a `/api/process-input` submission. REUSES the pending
 * token only when re-submitting the *exact same text* after a failure / unknown
 * outcome (so a committed-but-lost CREATE is deduped by the backend); any
 * new/edited command gets a fresh token.
 */
export function pickOmniToken(
  pending: { text: string; token: string } | null,
  text: string,
): string {
  return pending && pending.text === text ? pending.token : newClientToken();
}

/**
 * After a submission resolves, should the token be RETAINED (kept pending) so an
 * identical retry reuses it? `null` = network/unknown outcome → retain. A
 * terminal status that committed nothing (SUCCESS clears it a different way;
 * clarification / not-supported) → release. Any other error status is ambiguous
 * (the mutation may have committed) → retain.
 */
export function omniTokenRetained(outcomeStatus: string | null): boolean {
  if (outcomeStatus === null) return true;
  return !["SUCCESS", "CLARIFICATION_REQUIRED", "ACTION_NOT_SUPPORTED"].includes(outcomeStatus);
}
