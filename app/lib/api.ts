// Central place for the backend API base URL + a fetch wrapper that always
// sends the session cookie.
//
// Phase 5A — secure development foundation:
//   * The DEFAULT is same-origin. Leave NEXT_PUBLIC_API_BASE_URL blank and every
//     request goes to a RELATIVE "/api/..." path on whatever origin served the
//     page (https://<dev-host>/ behind the reverse proxy, or http://localhost:3000
//     for plain desktop dev). Next.js rewrites "/api/*" to the FastAPI backend
//     server-side (see next.config.ts), so the browser never needs to know that
//     FastAPI listens on :8080.
//   * An EXPLICIT absolute base is still honoured (direct/no-proxy dev, tests,
//     future split-origin deployments) via NEXT_PUBLIC_API_BASE_URL.
//   * MIXED-CONTENT GUARD: an https:// page must never be allowed to silently
//     issue http:// API calls — that is blocked by the browser as active mixed
//     content. If the configuration would do that, fail loudly instead.
//
// Phase 5H-B5 — network / failure UX:
//   * Every request has a bounded deadline (no request hangs forever).
//   * Failures are TYPED: http | timeout | offline | network | canceled, so a
//     caller can tell "the server rejected this" from "we never heard back".
//   * mutationOutcome() maps a failure to failed | unknown so a UI never claims
//     a persistent write failed when it might have committed.
export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL?.trim().replace(/\/+$/, "") || "";

/**
 * Build a request URL from a path like "/api/finance".
 *
 * Empty API_BASE_URL  -> same-origin relative URL ("/api/finance").
 * Explicit API_BASE_URL -> absolute ("https://api.example.com/api/finance").
 *
 * Throws if the page is served over HTTPS but the configured base is http://
 * (would be a mixed-content request the browser silently drops).
 */
export function apiUrl(path: string): string {
  const p = path.startsWith("/") ? path : `/${path}`;
  if (!API_BASE_URL) return p; // same-origin

  if (
    typeof window !== "undefined" &&
    window.location.protocol === "https:" &&
    API_BASE_URL.startsWith("http://")
  ) {
    const msg =
      `CoreOS config error: the page is HTTPS but NEXT_PUBLIC_API_BASE_URL is ` +
      `"${API_BASE_URL}". Leave it blank for same-origin, or use an https:// base. ` +
      `An http:// API base from an HTTPS page is blocked as mixed content.`;
    // Surface it in the console so this misconfiguration is obvious to a
    // developer even though the calling UI shows a generic message.
    console.error(msg);
    throw new Error(msg);
  }
  return `${API_BASE_URL}${p}`;
}

export type ApiErrorKind =
  | "http" //      got an HTTP response with a non-2xx status
  | "timeout" //   our own deadline aborted the request
  | "offline" //   fetch failed AND navigator.onLine corroborates no connection
  | "network" //   fetch failed before a response (DNS, reset, CORS, TLS, ...)
  | "canceled"; //  the CALLER's AbortSignal aborted the request

export class ApiError extends Error {
  /** HTTP status for kind "http"; 0 for every other kind. */
  status: number;
  kind: ApiErrorKind;
  constructor(kind: ApiErrorKind, status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
  }
}

// Deadline budgets by workload. A cheap local CRUD write settles fast; a
// provider-backed search or an AI turn legitimately needs longer. Callers pass
// `kind`; `timeoutMs` overrides outright.
export const REQUEST_BUDGET_MS = {
  crud: 15_000,
  auth: 12_000,
  provider: 20_000,
  ai: 45_000,
} as const;
export type RequestKind = keyof typeof REQUEST_BUDGET_MS;

export interface ApiFetchOptions extends RequestInit {
  /** Deadline bucket (default "crud"). */
  kind?: RequestKind;
  /** Explicit deadline in ms; overrides `kind`. */
  timeoutMs?: number;
}

/**
 * fetch() against the CoreOS backend. Always includes credentials so the
 * HttpOnly session cookie is sent. Rejects with a typed `ApiError` on any
 * failure (HTTP non-2xx, deadline timeout, caller cancel, or network).
 */
export async function apiFetch<T = unknown>(
  path: string,
  options: ApiFetchOptions = {},
): Promise<T> {
  const { kind = "crud", timeoutMs, signal: callerSignal, ...init } = options;
  const budget = timeoutMs ?? REQUEST_BUDGET_MS[kind];

  const ctrl = new AbortController();
  let timedOut = false;
  const onCallerAbort = () => ctrl.abort();
  if (callerSignal) {
    if (callerSignal.aborted) ctrl.abort();
    else callerSignal.addEventListener("abort", onCallerAbort, { once: true });
  }
  const timer = setTimeout(() => {
    timedOut = true;
    ctrl.abort();
  }, budget);

  let res: Response;
  try {
    res = await fetch(apiUrl(path), {
      credentials: "include",
      headers: {
        ...(init.body ? { "Content-Type": "application/json" } : {}),
        ...(init.headers || {}),
      },
      ...init,
      signal: ctrl.signal,
    });
  } catch {
    if (timedOut) {
      throw new ApiError("timeout", 0, "This is taking too long. Please try again.");
    }
    if (callerSignal?.aborted) {
      throw new ApiError("canceled", 0, "Request canceled.");
    }
    // fetch rejected before any response. navigator.onLine is ADVISORY — only
    // treat it as "offline" when it positively says so; otherwise it is a
    // generic network failure, not proof the device is disconnected.
    const offline =
      typeof navigator !== "undefined" && navigator.onLine === false;
    throw new ApiError(
      offline ? "offline" : "network",
      0,
      offline
        ? "You appear to be offline. Check your connection and try again."
        : "Couldn't reach the server. Please try again.",
    );
  } finally {
    clearTimeout(timer);
    if (callerSignal) callerSignal.removeEventListener("abort", onCallerAbort);
  }

  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const data = await res.json();
      if (typeof data?.detail === "string") detail = data.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError("http", res.status, detail);
  }

  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/**
 * After a PERSISTENT mutation failed, was it DEFINITELY not applied, or is the
 * outcome UNKNOWN (it may have committed before the response was lost)?
 *
 *   "failed"  -> the server rejected the request before writing anything
 *               (validation 4xx, 409 idempotency conflict, 429 throttle).
 *   "unknown" -> 5xx, request-timeout, or any transport failure: the write may
 *               have landed. NEVER tell the user it failed.
 */
export function mutationOutcome(err: unknown): "failed" | "unknown" {
  if (err instanceof ApiError && err.kind === "http") {
    if (err.status >= 400 && err.status < 500 && err.status !== 408) return "failed";
    return "unknown"; // 408, 5xx
  }
  return "unknown"; // timeout / offline / network / canceled / anything unexpected
}

/** User-facing copy for a failure, tuned by whether a read or a write failed. */
export function describeFailure(
  err: unknown,
  opts: { verb?: string } = {},
): string {
  const verb = opts.verb ?? "load that";
  if (err instanceof ApiError) {
    switch (err.kind) {
      case "http":
        return err.message;
      case "timeout":
        return `Couldn't ${verb} — the server took too long. Please try again.`;
      case "offline":
        return "You appear to be offline. Check your connection and try again.";
      case "network":
        return "Couldn't reach the server. Please try again.";
      case "canceled":
        return "Request canceled.";
    }
  }
  return `Couldn't ${verb}. Please try again.`;
}
