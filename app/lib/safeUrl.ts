// Phase 5H-B4 (L1) — external link safety.
//
// A place "Website" comes from third-party provider data. The backend already
// drops non-web schemes, but the UI applies the same rule at the point of
// render so a bad value can never become a clickable `javascript:` / `data:`
// href. No replacement URL is invented — an invalid value yields null and the
// caller omits the action.

/**
 * Return the URL unchanged only if it is an absolute http/https URL with a
 * host; otherwise null.
 */
export function safeExternalHref(raw: string | null | undefined): string | null {
  if (typeof raw !== "string") return null;
  const s = raw.trim();
  if (!s) return null;
  let u: URL;
  try {
    u = new URL(s);
  } catch {
    return null;
  }
  if ((u.protocol !== "http:" && u.protocol !== "https:") || !u.host) return null;
  return u.href;
}
