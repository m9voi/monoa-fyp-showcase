// Phase 5H-B4 (M1) — post-login redirect safety.
//
// `?next=` is attacker-controllable. It must only ever cause navigation to a
// path ON THIS ORIGIN. Anything that could resolve to an external / network
// destination (absolute URL, protocol-relative `//host`, backslash `/\host`,
// `javascript:` / `data:` scheme, encoded equivalents, control characters)
// falls back to "/".

const MAX_LEN = 2048;
const SENTINEL_BASE = "http://coreos.invalid";

function hasControlChar(s: string): boolean {
  for (let i = 0; i < s.length; i++) {
    const code = s.charCodeAt(i);
    if (code < 0x20 || code === 0x7f) return true;
  }
  return false;
}

/**
 * Return a guaranteed same-origin path (always starts with a single "/"), or
 * "/" when `raw` is missing or not a safe local path.
 */
export function safeInternalPath(raw: string | null | undefined): string {
  if (typeof raw !== "string") return "/";
  if (raw.length > MAX_LEN) return "/";
  if (hasControlChar(raw)) return "/"; // CR/LF/TAB/NUL anywhere, incl. edges
  const s = raw.trim();
  if (!s) return "/";

  // Must be a rooted path, not a scheme and not a host reference.
  if (s[0] !== "/") return "/"; // "https://", "javascript:", "  x"
  if (s[1] === "/" || s[1] === "\\") return "/"; // "//evil", "/\evil"

  try {
    const u = new URL(s, SENTINEL_BASE);
    // If the value pulled the URL to any other origin, it was not local.
    if (u.origin !== SENTINEL_BASE) return "/";
    const out = `${u.pathname}${u.search}${u.hash}`;
    if (out[0] !== "/" || out[1] === "/" || out[1] === "\\") return "/";
    return out;
  } catch {
    return "/";
  }
}
