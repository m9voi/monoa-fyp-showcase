"""Phase 2 security configuration — single source of truth, env-driven.

Production (COREOS_ENV=production) FAILS FAST if a required secret is missing
or left at an insecure development default. Development uses clearly-labelled
insecure defaults and prints a warning.
"""

import os
import sys

from dotenv import load_dotenv

# Load .env before reading any variable — this module is imported (via auth.py)
# before main.py runs its own load_dotenv().
load_dotenv()

ENV = os.getenv("COREOS_ENV", "development").strip().lower()
IS_PRODUCTION = ENV in ("production", "prod")


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean env var. Unset / empty -> `default`.
    Truthy: 1 true yes on. Falsy: 0 false no off (anything else -> default)."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    v = raw.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


def _int_env(name: str, default: int, *, min_value: int, max_value: int) -> int:
    """Parse an integer env var, guarded to an inclusive ``[min_value, max_value]``.

    * Unset / empty -> ``default`` (setting nothing is not a misconfiguration).
    * Set but not a base-10 integer, or outside the allowed range -> FAIL FAST
      with a clear RuntimeError that names the variable and the range.

    A malformed or unsafe explicit value is NEVER silently replaced with the
    default and NEVER clamped into range: a security control that has been
    configured wrong must be visible at startup, not effectively disabled
    (e.g. ``RL_AI_MAX=0`` or ``RL_AI_MAX=999999999``) or broken later mid-request.
    Mirrors ``validate_startup()``'s fail-fast posture.
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    s = raw.strip()
    try:
        v = int(s)
    except ValueError:
        msg = (f"FATAL: {name} must be a base-10 integer (got {s!r}). "
               f"Allowed range: {min_value}..{max_value}.")
        print(msg, file=sys.stderr)
        raise RuntimeError(msg)
    if not (min_value <= v <= max_value):
        msg = (f"FATAL: {name}={v} is out of the allowed range "
               f"{min_value}..{max_value}.")
        print(msg, file=sys.stderr)
        raise RuntimeError(msg)
    return v


# Public alias — other modules (main.py) share this fail-fast integer policy.
int_env = _int_env


# ---- session / cookie ----
COOKIE_NAME = "coreos_session"
# Session lifetime. Guarded (Phase 5H-B4): a malformed / zero / negative value is
# a fail-fast startup error, never a silent 0-second (instantly-expiring) or
# multi-decade session. 1 minute .. 2 years.
SESSION_TTL_SECONDS = _int_env(
    "COREOS_SESSION_TTL_SECONDS", 604800, min_value=60, max_value=63_072_000
)  # default 7 days

_DEV_SESSION_SECRET = "dev-insecure-not-for-production-change-me"
SESSION_SECRET = os.getenv("COREOS_SESSION_SECRET", "").strip() or _DEV_SESSION_SECRET

# Secure flag on the auth cookie.
#   * Production: always on (public origin is HTTPS) — not overridable downward.
#   * Development: OFF by default (plain http://localhost dev), but set
#     COREOS_COOKIE_SECURE=1 when the browser-facing dev origin is HTTPS
#     (Phase 5A reverse proxy). Deterministic from config — never inferred from
#     proxy headers.
COOKIE_SECURE = _env_bool("COREOS_COOKIE_SECURE", IS_PRODUCTION)
# Lax is correct for a same-site (localhost:3000 <-> localhost:8080) cookie that
# must survive top-level navigation. 'none' would be needed for a truly cross-site
# setup and then REQUIRES Secure. Guarded (Phase 5H-B6): an unrecognised value is
# a fail-fast startup error, not a value silently handed to the cookie layer
# (which would 500 on the first Set-Cookie) or a typo that quietly weakens
# scoping.
_VALID_SAMESITE = ("lax", "strict", "none")
COOKIE_SAMESITE = os.getenv("COREOS_COOKIE_SAMESITE", "lax").strip().lower()
if COOKIE_SAMESITE not in _VALID_SAMESITE:
    _msg = (f"FATAL: COREOS_COOKIE_SAMESITE={COOKIE_SAMESITE!r} is invalid; "
            f"use one of {_VALID_SAMESITE}.")
    print(_msg, file=sys.stderr)
    raise RuntimeError(_msg)

# ---- CORS ----
def _parse_origins(raw: str) -> list[str]:
    return [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]

# Development-only CORS defaults. Used ONLY when COREOS_ALLOWED_ORIGINS is unset
# and COREOS_ENV != production. Includes the Phase 5A HTTPS reverse-proxy origin
# (https://coreos.local) alongside the plain-localhost dev origins. A custom
# proxy hostname is picked up automatically from COREOS_DEV_HOST. For LAN/
# physical-device testing, set COREOS_ALLOWED_ORIGINS explicitly to your own
# machine's LAN origin (e.g. http://<your-LAN-IP>:3000) rather than relying on
# this list — it intentionally has no hardcoded LAN address.
_DEV_PROXY_HOST = os.getenv("COREOS_DEV_HOST", "coreos.local").strip() or "coreos.local"
_DEV_ORIGINS = [
    f"https://{_DEV_PROXY_HOST}",
    "https://coreos.local",
    "https://localhost:3000",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]
ALLOWED_ORIGINS = _parse_origins(os.getenv("COREOS_ALLOWED_ORIGINS", ""))
if not ALLOWED_ORIGINS and not IS_PRODUCTION:
    ALLOWED_ORIGINS = list(dict.fromkeys(o.rstrip("/") for o in _DEV_ORIGINS))


def normalize_origin(raw: str) -> str:
    """Canonical `scheme://host[:port]` for an Origin comparison.

    Lowercases scheme + host, drops any path / trailing slash / userinfo. Returns
    "" for anything that is not a well-formed absolute http(s) origin (including
    the literal ``null`` a sandboxed/opaque browser context sends).
    """
    if not isinstance(raw, str):
        return ""
    s = raw.strip()
    if not s or s.lower() == "null":
        return ""
    # A well-formed Origin is a single token: no whitespace (a comma-joined /
    # duplicated header), and no userinfo `@` (a real browser never sends it;
    # allowing it invites `http://evil@allowed-host` confusion).
    if any(c.isspace() for c in s) or "@" in s:
        return ""
    try:
        from urllib.parse import urlsplit
        u = urlsplit(s)
        scheme = u.scheme.lower()
        host = u.hostname
        port = u.port  # may raise ValueError on a malformed netloc
    except ValueError:
        return ""
    if scheme not in ("http", "https") or not host:
        return ""
    # An Origin is scheme://host[:port] ONLY — no path/query/fragment. A real
    # browser never sends one; anything extra is malformed -> reject.
    if u.path not in ("", "/") or u.query or u.fragment:
        return ""
    return f"{scheme}://{host.lower()}{f':{port}' if port else ''}"


# Pre-normalized allowlist for the Phase 5H-B4 Origin defense-in-depth check on
# authenticated state-changing requests (this is IN ADDITION to SameSite cookies
# and CORS, not a replacement).
ALLOWED_ORIGIN_SET = frozenset(
    o for o in (normalize_origin(x) for x in ALLOWED_ORIGINS) if o
)

# ---- auth rate limiting (local, per-process baseline) ----
# Guarded (Phase 5H-B4): a non-integer / zero / negative / absurd value FAILS
# FAST rather than silently disabling the auth throttle or turning it into a
# multi-day lockout.
AUTH_RATE_MAX_ATTEMPTS = _int_env(
    "COREOS_AUTH_RATE_MAX", 10, min_value=1, max_value=1000
)
AUTH_RATE_WINDOW_SECONDS = _int_env(
    "COREOS_AUTH_RATE_WINDOW", 300, min_value=1, max_value=86_400
)  # default 5 min

# ---- API docs exposure (Phase 5H-B4, L3) ----
# Interactive docs + the OpenAPI schema are developer conveniences. On in
# development (reachable through the dev proxy at /api/docs); OFF by default in
# production so a public deployment does not advertise its full surface.
EXPOSE_DOCS = _env_bool("COREOS_EXPOSE_DOCS", not IS_PRODUCTION)

# ---- Phase 5H-B2: abuse / denial-of-wallet protection for the paid /
# rate-policed provider routes. Sliding-window, keyed by AUTHENTICATED user +
# logical route bucket (never by IP, query text or coordinates). Process-local
# and in-memory — a shared store (Redis/etc.) for multi-worker production remains
# a release-readiness item. Values are deliberately generous: comfortably above
# real interactive use, well below a request storm.
#
# Every value is env-overridable but bounded (see _int_env): a non-integer, a
# zero/negative (which would silently disable the control), or an absurd value
# (RL_AI_MAX=999999999 — "configured" yet ineffective) FAILS FAST at startup.
# Upper bounds: a per-user limiter that allows > ~1000 requests / window is not
# meaningfully protecting spend; a window must be a real positive duration and
# not a multi-year lockout; the bucket cap must keep per-user isolation working
# without an insane memory ceiling; the AI-input cap must stay a hard ceiling
# even if fat-fingered.
RL_WINDOW_SECONDS = _int_env("COREOS_RL_WINDOW", 60, min_value=1, max_value=3600)
# AI command input -> a direct, per-token-billed model call: the most expensive
# per request, so the tightest ceiling.
RL_AI_MAX = _int_env("COREOS_RL_AI_MAX", 10, min_value=1, max_value=1000)
# Places nearby-search: one upstream call; a user may click through several
# activity chips while exploring.
RL_PLACES_SEARCH_MAX = _int_env("COREOS_RL_PLACES_SEARCH_MAX", 30, min_value=1, max_value=1000)
# Places resolve (Schedule -> Map "View place"): one-off per opened event.
RL_PLACES_RESOLVE_MAX = _int_env("COREOS_RL_PLACES_RESOLVE_MAX", 20, min_value=1, max_value=1000)
# Geocode area search: explicit "Search" button (not keystroke autocomplete);
# a user may refine the text a handful of times.
RL_GEOCODE_MAX = _int_env("COREOS_RL_GEOCODE_MAX", 20, min_value=1, max_value=1000)
# Activity search: ONE user request, but an internal provider fan-out of up to
# 1 + places_provider.SEARCH_TEXTS_MAX_QUERIES upstream calls, so its
# per-request ceiling is set lower than plain places search.
RL_ACTIVITIES_MAX = _int_env("COREOS_RL_ACTIVITIES_MAX", 15, min_value=1, max_value=1000)
# Weather context: already protected by a bounded short-TTL cache; this limiter
# is a safety net so a coordinate-varying cache-bypass storm still can't make
# unbounded upstream calls. Free provider -> a slightly looser ceiling.
RL_WEATHER_MAX = _int_env("COREOS_RL_WEATHER_MAX", 40, min_value=1, max_value=2000)
# Hard cap on distinct in-memory limiter buckets (one per user+route). Prevents
# unbounded growth; the least-recently-active bucket is evicted past this. Below
# ~100 the eviction churn would break per-user isolation.
RL_MAX_BUCKETS = _int_env("COREOS_RL_MAX_BUCKETS", 50000, min_value=100, max_value=1_000_000)
# Phase 5H-B5: the create-recovery status lookup (POST /api/idempotency/status).
# A read against the caller's OWN rows — no provider cost — but bounded so it
# cannot be spun as a probe. A recovering client makes at most a handful of
# these per reload.
RL_IDEM_STATUS_MAX = _int_env("COREOS_RL_IDEM_STATUS_MAX", 60, min_value=1, max_value=1000)
# B2 (Mo human-in-the-loop): confirming / cancelling a Mo proposal and listing
# the caller's own pending proposal. No provider cost; a bound so repeated
# confirms / a retry storm cannot be spun into abuse. The confirm handler is
# idempotent regardless, so this is a courtesy ceiling, not a safety boundary.
RL_MO_PROPOSAL_MAX = _int_env("COREOS_RL_MO_PROPOSAL_MAX", 60, min_value=1, max_value=2000)
# B3 (Plan conflict decisions): Accept / Keep on a detected schedule overlap.
# A write against the caller's OWN events + one decision row — no provider cost.
# Accept/Keep are idempotent (UNIQUE(user_id, fingerprint)), so this is a
# courtesy ceiling against a retry storm, not a safety boundary.
RL_SCHEDULE_DECISION_MAX = _int_env("COREOS_RL_SCHEDULE_DECISION_MAX", 60, min_value=1, max_value=2000)
# Product Completion Batch 03 (Mo voice input): one short recording -> one
# paid transcription call, the most expensive-per-request bucket after AI
# itself, so a tight ceiling matching RL_AI_MAX's spirit.
RL_MO_TRANSCRIBE_MAX = _int_env("COREOS_RL_MO_TRANSCRIBE_MAX", 10, min_value=1, max_value=1000)

# ---- Phase 5H-B2: AI Omni-bar input bound (fixes H2). A natural-language
# command is short; anything larger is rejected BEFORE the model is called so a
# multi-megabyte / token-abuse payload cannot run up model cost. Must stay >= 1
# (0/negative would reject every command) and <= 100_000 (~25k tokens — a hard
# ceiling that keeps H2 shut even if misconfigured).
AI_INPUT_MAX_CHARS = _int_env("COREOS_AI_INPUT_MAX_CHARS", 2000, min_value=1, max_value=100_000)

# ---- Product Completion Batch 03: Mo voice input (bounded base64 upload,
# see main.py POST /api/mo/transcribe). A short voice COMMAND, not a long
# recording — 30s at even a generous bitrate comfortably fits under 8MB
# decoded (a 30s uncompressed 44.1kHz mono WAV, Safari's worst case, is
# ~2.6MB). Bounding the base64 STRING length directly (rather than decoding
# first) means an oversized payload is rejected before any decode work.
MO_TRANSCRIBE_MAX_SECONDS = _int_env("COREOS_MO_TRANSCRIBE_MAX_SECONDS", 30, min_value=1, max_value=300)
MO_TRANSCRIBE_MAX_AUDIO_BYTES = _int_env(
    "COREOS_MO_TRANSCRIBE_MAX_AUDIO_BYTES", 8_000_000, min_value=10_000, max_value=50_000_000
)

# ---- password policy ----
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128


def validate_startup() -> None:
    """Raise on an unsafe production configuration. Called once at import."""
    problems = []
    # Applies in every environment: SameSite=None strips the cross-site
    # protection off the session cookie and browsers only honour it WITH Secure.
    # Setting it without Secure is always a misconfiguration.
    if COOKIE_SAMESITE == "none" and not COOKIE_SECURE:
        problems.append(
            "COREOS_COOKIE_SAMESITE=none requires COREOS_COOKIE_SECURE=1 "
            "(browsers reject a SameSite=None cookie that is not Secure, and it "
            "removes the cookie's cross-site protection)."
        )
    if IS_PRODUCTION:
        if SESSION_SECRET == _DEV_SESSION_SECRET or len(SESSION_SECRET) < 32:
            problems.append(
                "COREOS_SESSION_SECRET is missing or too weak "
                "(need >= 32 chars; generate with "
                "`python -c \"import secrets;print(secrets.token_urlsafe(48))\"`)."
            )
        if not ALLOWED_ORIGINS:
            problems.append(
                "COREOS_ALLOWED_ORIGINS must list the exact production frontend "
                "origin(s); no default is assumed in production."
            )
        if any(o.startswith("http://") and "localhost" not in o and "127.0.0.1" not in o
               for o in ALLOWED_ORIGINS):
            problems.append("COREOS_ALLOWED_ORIGINS contains a non-HTTPS public origin.")
        if not COOKIE_SECURE:
            problems.append(
                "COREOS_COOKIE_SECURE must not be disabled in production; the "
                "session cookie has to carry the Secure attribute over HTTPS."
            )
    if problems:
        msg = "FATAL: insecure production configuration:\n  - " + "\n  - ".join(problems)
        print(msg, file=sys.stderr)
        raise RuntimeError(msg)

    if not IS_PRODUCTION and SESSION_SECRET == _DEV_SESSION_SECRET:
        print("SECURITY WARNING: using the built-in DEV session secret. "
              "Set COREOS_SESSION_SECRET before any real deployment.", file=sys.stderr)


validate_startup()
