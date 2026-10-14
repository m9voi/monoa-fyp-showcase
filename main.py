import os
import uuid
import datetime
import json
import math
import re
import difflib
import shutil
import sqlite3
import ast
import operator
import base64
import binascii
from enum import Enum
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Depends, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from openai import OpenAI

# Phase 1: central database access layer + background reminder scheduler.
import db as db_layer
import scheduler as reminder_scheduler
# Phase 2: authentication core + security configuration.
import auth as auth_core
import security_config as sec
# Phase 3: finance core (money-as-sen, canonical categories, authoritative snapshot).
import finance_core as fin
# Phase 4: planning & schedule core (deterministic conflict + recurrence engine).
import schedule_core as sched
# Phase 5C: verified-places core + provider abstraction (real external place data).
import places_core as places
import places_provider as places_provider
# Phase 5D: geocoding provider abstraction (typed area -> real search origin).
import geocode_provider as geocode
# Phase 5E: deterministic Activity Engine (intent -> requirements -> evidence -> rank).
import activity_core as activity_core
# Phase 5F: weather intelligence foundation (contextual infrastructure, not a feature).
import weather_core as weather_core
import weather_provider as weather_provider
# Phase 5G: provider-neutral place reference for the Map <-> Schedule bridge.
import place_ref as place_ref
# Phase 5H-B3: shared create-once idempotency (client_token + DB uniqueness).
import idempotency as idem

import mo_proposals as mo
import schedule_conflict as sconf
import weather_conflict as wxc
import task_conflict as tec
# Securely load the API key from the hidden .env file
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Runtime configuration (overridable via environment / .env). Defaults preserve
# the previous hard-coded behaviour so nothing changes for the current setup.
DB_PATH = os.getenv("COREOS_DB_PATH", "coreos.db")
# Phase 5H-B4 (backend exposure / M3 CASE D): bind loopback by default. The
# browser and the phone reach CoreOS through the trusted HTTPS front door
# (dev/Caddyfile: Caddy listens on :443 on every interface and proxies to
# 127.0.0.1:8080), so FastAPI never needs a LAN-facing socket. Binding
# 127.0.0.1 removes the direct-to-:8080 path a LAN client could otherwise use
# to bypass Caddy's TLS + its X-Forwarded-For sanitisation. Set COREOS_HOST
# explicitly only if a topology genuinely needs a wider bind.
HOST = os.getenv("COREOS_HOST", "127.0.0.1")
# Guarded (Phase 5H-B4): a malformed / out-of-range COREOS_PORT is a fail-fast
# startup error, not a confusing bind failure deep in uvicorn.
PORT = sec.int_env("COREOS_PORT", 8080, min_value=1, max_value=65_535)

# Initialize OpenAI client for the active Ask Bar flow
if OPENAI_API_KEY:
    ai_client = OpenAI(api_key=OPENAI_API_KEY)
else:
    ai_client = None

# Mo's interpretation model. Selected via a bounded, real controlled
# benchmark (22 representative prompts, 3 candidates, same code path — see
# the Mo smartness + experience closure report) run against gpt-4o-mini,
# gpt-5.6-luna and gpt-5.6-terra: gpt-5.6-terra scored 22/22 structural
# invariants at the LOWEST average latency of the three (gpt-4o-mini 18/22,
# gpt-5.6-luna 19/22 — not a clear enough gain over the baseline to prefer it
# per the model-selection rule, so terra was evaluated and won outright).
# Overridable via COREOS_MO_MODEL for that same benchmark script, or to roll
# back to gpt-4o-mini if a real-device regression is ever found. gpt-4o-mini
# is the only family here that supports temperature=0.0 — the newer
# gpt-5.6-* models reject a custom temperature entirely (400 "Only the
# default (1) value is supported"), so temperature is only sent for the
# gpt-4o family.
MO_MODEL = os.getenv("COREOS_MO_MODEL", "gpt-5.6-terra")

# Product Completion Batch 03 (Mo voice input): a currently-supported model
# from the installed OpenAI SDK's transcription model set (openai==3.3.1).
# Separate knob from MO_MODEL — transcription and interpretation are two
# different model families and may need to move independently.
MO_TRANSCRIBE_MODEL = os.getenv("COREOS_MO_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")

# Product Completion Batch 04 (Today): thresholds for the deterministic
# priority stack. "Deterministic prioritization of real cards" per the spec —
# not a learned/personalized threshold, just a documented constant.
DASHBOARD_EVENT_SOON_MINUTES = 120  # an event this close counts as "starting soon"
DASHBOARD_BUDGET_NEAR_PCT = 80.0    # today's spend at/above this % of the daily budget is "nearing" it


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: the reminder scheduler polls the DB for due reminders.
    # It is best-effort - a failure here must never stop the API.
    reminder_scheduler.start()
    try:
        yield
    finally:
        reminder_scheduler.shutdown()


# Phase 5H-B4 (L3): interactive docs + the OpenAPI schema are gated. On in
# development (served under /api/* so they reach the browser through the dev
# proxy); off by default in production so a public deployment does not publish
# its full surface. COREOS_EXPOSE_DOCS overrides either way.
_docs_kwargs = (
    dict(docs_url="/api/docs", redoc_url="/api/redoc", openapi_url="/api/openapi.json")
    if sec.EXPOSE_DOCS
    else dict(docs_url=None, redoc_url=None, openapi_url=None)
)
app = FastAPI(lifespan=lifespan, **_docs_kwargs)

# Phase 2: no wildcard. Credentialed cookie auth requires an explicit origin
# allowlist (a wildcard is invalid with allow_credentials=True). Origins come
# from COREOS_ALLOWED_ORIGINS; production has no default (see security_config).
app.add_middleware(
    CORSMiddleware,
    allow_origins=sec.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

# --------------------------------------------------------------------------- #
# Phase 5H-B4 — browser Origin defense-in-depth + baseline response headers.
#
# This is IN ADDITION to the SameSite=lax session cookie and the CORS allowlist,
# not a replacement for either. For an authenticated state-changing request
# (POST/PUT/PATCH/DELETE) that carries a browser `Origin` header, the origin
# must be one of the configured allowed origins or the request is refused with a
# generic 403 BEFORE any handler / mutation runs. A request with NO `Origin`
# header is allowed through (native apps, server-to-server callers, the test
# client and future mobile clients legitimately omit it); it is still subject to
# cookie auth, CORS and the rate limiter. `Referer` is never used for this.
# --------------------------------------------------------------------------- #
_ORIGIN_GUARD_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def _origin_and_headers_mw(request: Request, call_next):
    if (
        request.method in _ORIGIN_GUARD_METHODS
        and request.url.path.startswith("/api/")
    ):
        raw_origin = request.headers.get("origin")
        if raw_origin is not None:
            if sec.normalize_origin(raw_origin) not in sec.ALLOWED_ORIGIN_SET:
                # Generic — never echo the rejected origin back to the caller.
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={"detail": "Request origin is not allowed."},
                )
    response = await call_next(request)
    # Cheap, universally-safe hardening header on every API response. Document
    # security headers (CSP/frame-ancestors/Permissions-Policy/Referrer-Policy)
    # belong on the HTML responses and are set by Next.js — see next.config.ts.
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


@app.exception_handler(RequestValidationError)
async def _sanitized_validation_handler(_request: Request, exc: RequestValidationError):
    # Phase 5H-B4 (privacy): FastAPI's default 422 body echoes `input` (and
    # `ctx`), reflecting the submitted value — a password, financial free text,
    # a location string, Omni-bar prompt text. Keep the truthful field / type /
    # message, drop the echoed value.
    safe = [
        {"type": e.get("type"), "loc": list(e.get("loc", [])), "msg": e.get("msg")}
        for e in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": safe})

def get_db():
    # Central connection: sqlite3.Row rows + PRAGMA foreign_keys=ON + busy_timeout.
    # (Phase 1 - see db.py. Kept as a function so every existing call site works
    # unchanged.)
    return db_layer.get_connection()


@app.exception_handler(idem.IdempotencyConflict)
async def _idempotency_conflict_handler(_request: Request, _exc: idem.IdempotencyConflict):
    # Phase 5H-B3: a client_token was reused for a materially different create
    # payload. Truthful, fixed message — never echoes the already-stored row.
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": "This form was already submitted with different details. "
                           "Refresh the page and try again."},
    )


# Phase 5H-B3: tables that carry the shared (user_id, client_token) create-once
# uniqueness. Interpolated into SQL only from this allow-list, never user input.
_IDEMPOTENT_TABLES = ("transactions", "tasks", "schedule_events")


def _prior_create(table: str, user_id: str, client_token: str):
    """Return ``(row_id, client_request_hash)`` of an existing create by this
    user + token, or ``None``. The hash lets the caller tell a truthful replay
    from a token-reuse-with-different-payload conflict."""
    if table not in _IDEMPOTENT_TABLES:
        raise ValueError(f"not an idempotent table: {table!r}")
    conn = get_db()
    try:
        r = conn.execute(
            f"SELECT id, client_request_hash FROM {table} "
            f"WHERE user_id = ? AND client_token = ?",
            (user_id, client_token),
        ).fetchone()
        return (r["id"], r["client_request_hash"]) if r else None
    finally:
        conn.close()


def _idem_prior_outcome(table: str, user_id: str, client_token, incoming_hash, build_outcome):
    """AI-path (Omni-bar) create-once. Returns a replay ``ActionOutcome`` (built
    by ``build_outcome(prior_id)``) when a prior create by (user_id, client_token)
    exists and its payload matches; raises ``idem.IdempotencyConflict`` when the
    token was reused with a materially different payload; returns ``None`` when
    there is no prior (the caller proceeds to insert)."""
    if not client_token:
        return None
    prior = _prior_create(table, user_id, client_token)
    if prior is None:
        return None
    prior_id, prior_hash = prior
    if prior_hash is not None and prior_hash != incoming_hash:
        raise idem.IdempotencyConflict()
    return build_outcome(prior_id)

# Helper function to get current time with timezone offset (e.g., +08:00)
def get_iso_now():
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()


# --------------------------------------------------------------------------- #
# Phase 2: authenticated identity. The backend NEVER trusts a client-supplied
# user_id. Every protected endpoint resolves the caller from the session cookie.
# --------------------------------------------------------------------------- #
def get_current_user(request: Request) -> dict:
    token = request.cookies.get(sec.COOKIE_NAME)
    user = auth_core.resolve_session(token) if token else None
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated.",
            headers={"WWW-Authenticate": "cookie"},
        )
    return user


# --------------------------------------------------------------------------- #
# Phase 5H-B2 — denial-of-wallet / abuse protection.
# A per-(authenticated user + logical route) sliding-window limiter that runs as
# a dependency, so it fires BEFORE the handler body ever reaches a paid or
# rate-policed provider call. Never keyed by IP, query text or coordinates.
# The ceiling is read from security_config at request time (env-overridable,
# test-tunable). ONE POST /api/activities/search counts as ONE request here even
# though it fans out to several bounded upstream searches.
# --------------------------------------------------------------------------- #
_RL_BUCKET_MAX = {
    "ai": lambda: sec.RL_AI_MAX,
    "places_search": lambda: sec.RL_PLACES_SEARCH_MAX,
    "places_resolve": lambda: sec.RL_PLACES_RESOLVE_MAX,
    "geocode": lambda: sec.RL_GEOCODE_MAX,
    "activities": lambda: sec.RL_ACTIVITIES_MAX,
    "weather": lambda: sec.RL_WEATHER_MAX,
    "idem_status": lambda: sec.RL_IDEM_STATUS_MAX,
    "mo_proposal": lambda: sec.RL_MO_PROPOSAL_MAX,
    "schedule_decision": lambda: sec.RL_SCHEDULE_DECISION_MAX,
    "mo_transcribe": lambda: sec.RL_MO_TRANSCRIBE_MAX,
}


def _rl(bucket: str):
    """Build an auth + rate-limit dependency for `bucket`. Returns the resolved
    user dict so the handler signature is unchanged."""
    def _dep(current: dict = Depends(get_current_user)) -> dict:
        allowed, retry_after = auth_core.rate_limit_hit(
            f"rl:{bucket}:{current['id']}",
            _RL_BUCKET_MAX[bucket](),
            sec.RL_WINDOW_SECONDS,
        )
        if not allowed:
            # CoreOS-side throttle. Distinct from a provider-side 429, which the
            # _run_* helpers normalise to a 200 `PROVIDER_ERROR` outcome instead.
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests right now. Try again shortly.",
                headers={"Retry-After": str(retry_after)},
            )
        return current
    return _dep


def _set_session_cookie(response: Response, raw_token: str) -> None:
    response.set_cookie(
        key=sec.COOKIE_NAME,
        value=raw_token,
        max_age=sec.SESSION_TTL_SECONDS,
        httponly=True,
        secure=sec.COOKIE_SECURE,
        samesite=sec.COOKIE_SAMESITE,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=sec.COOKIE_NAME,
        path="/",
        httponly=True,
        secure=sec.COOKIE_SECURE,
        samesite=sec.COOKIE_SAMESITE,
    )

# --- 1. STARTUP: ALEMBIC IS THE SOLE SCHEMA AUTHORITY (PHASE 2, STEP 0) ---
_BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")


def _timestamped_backup():
    """Copy the DB into backups/coreos_backup_YYYYMMDD.db once per day, before
    migrations run. Never overwrites an existing backup; never deletes old ones."""
    if not os.path.exists(DB_PATH):
        return
    try:
        os.makedirs(_BACKUP_DIR, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d")
        dest = os.path.join(_BACKUP_DIR, f"coreos_backup_{stamp}.db")
        if not os.path.exists(dest):
            shutil.copy2(DB_PATH, dest)
            print(f"SYSTEM: pre-migration DB backup -> {dest}")
    except Exception as e:
        print(f"SYSTEM WARNING: backup step failed ({e}); continuing.")


def _alembic_config():
    from alembic.config import Config
    here = os.path.dirname(os.path.abspath(__file__))
    cfg = Config(os.path.join(here, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(here, "alembic"))
    return cfg


def init_db():
    """Deterministic startup. NO in-code schema creation/mutation any more.

      1. backup the DB file
      2. `alembic upgrade head`      -> FAIL FAST on any error
      3. verify the DB is exactly at the Alembic head revision

    A partial / fallback schema is never constructed. Development data seeding
    lives in setup_db.py, not here.
    """
    _timestamped_backup()

    cfg = _alembic_config()
    try:
        from alembic import command
        command.upgrade(cfg, "head")
    except Exception as e:
        raise RuntimeError(
            f"FATAL: database migration failed; refusing to start. ({e})"
        ) from e

    # Verify head — mismatch is fatal (never run the app on an unexpected schema).
    from alembic.script import ScriptDirectory
    expected_head = ScriptDirectory.from_config(cfg).get_current_head()
    conn = get_db()
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        current = row[0] if row else None
    finally:
        conn.close()
    if current != expected_head:
        raise RuntimeError(
            f"FATAL: database is at Alembic revision {current!r}, "
            f"expected head {expected_head!r}. Refusing to start."
        )
    print(f"SYSTEM: schema verified at Alembic head ({expected_head}).")


init_db()

# --- 2. DATA MODELS ---
# Phase 2: NO model carries a user_id any more. The owner of every mutation is
# the authenticated session user, resolved server-side (get_current_user).
class RecommendationUpdate(BaseModel):
    recommendation_id: str
    action: str                      # ACCEPTED | DISMISSED | SNOOZE
    snooze_minutes: Optional[int] = None

# --- Phase 4: planning request models ---
class EventCreate(BaseModel):
    title: str
    start: str
    end: Optional[str] = None
    all_day: Optional[bool] = False
    location: Optional[str] = None
    notes: Optional[str] = None
    recurrence: Optional[str] = "none"        # none | daily | weekly | weekdays
    recurrence_until: Optional[str] = None
    related_task_id: Optional[str] = None
    # Phase 5G: an optional REAL place linked to this event (Map/Activity -> Schedule).
    place: Optional[dict] = None              # {provider, provider_place_id, name, address?, latitude, longitude}
    environment: Optional[str] = None         # B5: indoor | outdoor | uncertain (default uncertain)
    client_token: Optional[str] = None        # idempotency key for a retried create

class EventUpdate(BaseModel):
    title: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    all_day: Optional[bool] = None
    location: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = None              # confirmed | cancelled
    recurrence: Optional[str] = None
    recurrence_until: Optional[str] = None
    related_task_id: Optional[str] = None
    environment: Optional[str] = None         # B5: indoor | outdoor | uncertain
    # Phase 5G: re-link / re-point the real place, or `place: null` to unlink it.
    place: Optional[dict] = None

class TaskCreate(BaseModel):
    title: str
    notes: Optional[str] = None
    due_date: Optional[str] = None
    planned_at: Optional[str] = None
    scheduled_end: Optional[str] = None       # B7: end of an optional task work window
    reminder_at: Optional[str] = None
    client_token: Optional[str] = None        # Phase 5H-B3: idempotency key for a retried create

class TaskUpdate(BaseModel):
    title: Optional[str] = None
    notes: Optional[str] = None
    due_date: Optional[str] = None
    planned_at: Optional[str] = None
    scheduled_end: Optional[str] = None       # B7
    reminder_at: Optional[str] = None

class UserInput(BaseModel):
    user_input: str
    client_token: Optional[str] = None       # Phase 5H-B3: idempotency key for a retried Omni-bar submission

_APPEARANCE_MODES = ("system", "light", "dark")


class SettingsUpdate(BaseModel):
    notifications: bool
    location_services: bool
    # Legacy inert flag — still accepted from an old client, no longer drives
    # appearance. Optional so the B4 client can omit it.
    dark_mode: Optional[bool] = None
    # B4 canonical appearance choice. Optional for backward compatibility; when
    # present it MUST be one of system | light | dark (validated in the handler).
    appearance_mode: Optional[str] = None

class TransactionCreate(BaseModel):
    amount: Any = Field(description="Positive monetary amount in MYR major units.")
    type: Optional[str] = Field(default="expense", description="'expense' or 'income'.")
    category: Optional[str] = Field(default="Other")
    description: Optional[str] = Field(default="")
    date: Optional[str] = Field(default=None, description="ISO-8601 datetime; defaults to now (MYT).")
    client_token: Optional[str] = None       # Phase 5H-B3: idempotency key for a retried create

class TransactionUpdate(BaseModel):
    amount: Optional[Any] = None
    type: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    date: Optional[str] = None

class FinanceSettingsUpdate(BaseModel):
    # All financial fields are optional. Only the fields actually supplied by the
    # caller are changed; anything left out keeps its current stored value.
    # This prevents "save starting balance" from wiping existing budgets.
    starting_balance: Optional[float] = None
    weekly_budget: Optional[float] = None
    monthly_budget: Optional[float] = None
    daily_budget: Optional[float] = None

# --- Auth request models (Phase 2) ---
class RegisterRequest(BaseModel):
    email: str
    password: str
    name: Optional[str] = None

class LoginRequest(BaseModel):
    email: str
    password: str

class ProfileUpdate(BaseModel):
    name: str


# --- Account & security (Change Password / Change Email / Delete Account) ---
# All three re-verify the caller's CURRENT password before taking effect, even
# though they're already behind the session cookie — the account's own
# credential is the one thing only the real owner can supply, and these are
# exactly the actions a hijacked-but-not-yet-detected session shouldn't be
# able to take unchallenged. None of this sends a verification email: no
# email provider is wired into this project, so Change Email takes effect
# immediately on a correct password, with no second-channel confirmation —
# a real limitation, not something to misrepresent as verified.
class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class ChangeEmailRequest(BaseModel):
    new_email: str
    current_password: str


class DeleteAccountRequest(BaseModel):
    current_password: str


# --- Who Am I — persistent interests (Product Completion Batch 02) ---
# A bounded, curated vocabulary per category — never free text. This keeps
# validation trivial (membership check), makes deduplication exact, and
# rules out an unbounded taxonomy or accidental sensitive-attribute capture.
INTEREST_CATEGORIES: tuple[str, ...] = ("game", "sport", "movie_tv", "general")

INTEREST_OPTIONS: Dict[str, tuple] = {
    "game": (
        "Action", "RPG", "Strategy", "Shooter", "Sports Games",
        "Puzzle", "Simulation", "Adventure", "Racing", "Multiplayer/Online",
    ),
    "sport": (
        "Football", "Basketball", "Badminton", "Tennis", "Running",
        "Swimming", "Cycling", "Gym/Fitness", "Esports", "Martial Arts",
    ),
    "movie_tv": (
        "Action", "Comedy", "Drama", "Sci-Fi", "Anime",
        "Documentary", "Horror", "Thriller", "Romance", "K-Drama",
    ),
    "general": (
        "Fitness / Gym", "Running", "Studying", "Technology", "AI",
        "Travel", "Food", "Music", "Gaming culture", "Movies / entertainment",
        "Outdoor activities",
    ),
}
INTEREST_VALUE_MAX_LENGTH = 40


class InterestsUpdate(BaseModel):
    # Optional + None = "don't touch this category" (lets a single-category
    # screen save without needing to know the other three categories'
    # current state). An explicit [] clears that category. Never a raw
    # free-text field — every value is checked against INTEREST_OPTIONS.
    game: Optional[List[str]] = None
    sport: Optional[List[str]] = None
    movie_tv: Optional[List[str]] = None
    general: Optional[List[str]] = None


# --- Location contract (Phase 5B) ---
_LAT_MIN, _LAT_MAX = -90.0, 90.0
_LNG_MIN, _LNG_MAX = -180.0, 180.0


def _finite_number_or_none(v: Any) -> Optional[float]:
    """Return a finite float, or None for anything else (bool, str, NaN, inf...)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def validate_location_payload(raw: Any) -> dict:
    """Authoritative, reusable coordinate validation (Phase 5B; also used by the
    Phase 5C place/weather calls). Rejects NaN / Infinity / out-of-range /
    missing / non-numeric values. Error messages NEVER contain the coordinate
    values (privacy). Nothing is stored or logged here.
    """
    if not isinstance(raw, dict):
        raise HTTPException(status_code=422, detail="Location body must be a JSON object.")
    lat = _finite_number_or_none(raw.get("latitude"))
    lng = _finite_number_or_none(raw.get("longitude"))
    if lat is None or not (_LAT_MIN <= lat <= _LAT_MAX):
        raise HTTPException(status_code=422, detail="latitude must be a number between -90 and 90.")
    if lng is None or not (_LNG_MIN <= lng <= _LNG_MAX):
        raise HTTPException(status_code=422, detail="longitude must be a number between -180 and 180.")
    # An unusable accuracy figure does not invalidate the fix — normalise to None.
    acc = _finite_number_or_none(raw.get("accuracy_m"))
    if acc is not None and acc < 0:
        acc = None
    ts = _finite_number_or_none(raw.get("timestamp_ms"))
    timestamp_ms = int(ts) if ts is not None and ts > 0 else None
    return {"latitude": lat, "longitude": lng, "accuracy_m": acc, "timestamp_ms": timestamp_ms}


class LocationValidateResult(BaseModel):
    status: str = "VALID"
    latitude: float
    longitude: float
    accuracy_m: Optional[float] = None
    timestamp_ms: Optional[int] = None


# --- Geocoding contract (Phase 5D — typed area -> real search origin) ---
class GeoSearchResult(BaseModel):
    status: str                          # "OK" | "NO_RESULTS" | "PROVIDER_ERROR"
    provider: str                        # "osm" | "google"
    query: str
    results: list[dict] = []
    attribution: str = ""
    error: Optional[str] = None


# --- Activity Engine contract (Phase 5E — activity intent -> ranked real places) ---
class ActivitySearchResult(BaseModel):
    status: str                          # "OK" | "NO_RESULTS" | "PROVIDER_ERROR"
    activity_id: str
    provider: str                        # "osm" | "google"
    origin_source: str                   # "device" | "manual"
    distance_confident: bool             # False when a device fix is too coarse (Phase 5D tier)
    candidates: list[dict] = []          # CorePlace + activity_relevance block
    radius_m: int = places.SEARCH_RADIUS_DEFAULT_M   # B9.1: the radius the staged 8k->12k->20k search settled on
    attribution: str = ""
    error: Optional[str] = None          # a places_core.PROVIDER_* kind when status == PROVIDER_ERROR


# Mirror of the Phase 5D "very approximate" tier (app/lib/location.ts
# LOCATION_UNRELIABLE_ACCURACY_M): above this a device fix is too coarse for a
# confident metre-level distance, so the engine flags distance as not trustworthy.
_ACTIVITY_DISTANCE_UNRELIABLE_ACCURACY_M = 20_000.0


def _merge_places_by_id(*groups: list) -> list:
    out: list = []
    seen: set = set()
    for g in groups:
        for p in g or []:
            pid = p.get("place_id") if isinstance(p, dict) else None
            if pid and pid not in seen:
                seen.add(pid)
                out.append(p)
    return out


async def _run_activity_search(raw: Any) -> ActivitySearchResult:
    """Phase 5E — translate an activity intent into ranked, truthfully-classified
    real candidates. Reuses the Phase 5C/5D place-provider abstraction; the
    deterministic activity_core engine owns requirements / evidence / relevance.
    Stateless; no coordinate logging; the provider key stays server-side."""
    if not isinstance(raw, dict):
        raise HTTPException(status_code=422, detail="Search body must be a JSON object.")
    try:
        activity_id = activity_core.validate_activity_id(raw.get("activity_id"))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    origin = raw.get("origin")
    coords = validate_location_payload(origin)            # Phase 5B authoritative check
    source = str((origin or {}).get("source") or "").strip().lower()
    if source not in ("device", "manual"):
        source = "device"
    acc = coords["accuracy_m"]
    distance_confident = not (
        source == "device"
        and isinstance(acc, (int, float))
        and float(acc) > _ACTIVITY_DISTANCE_UNRELIABLE_ACCURACY_M
    )

    lat, lng = coords["latitude"], coords["longitude"]
    profile = activity_core.ACTIVITY_PROFILES[activity_id]
    provider = places_provider.get_provider()
    ladder = places.ACTIVITY_RADIUS_LADDER_M
    want = places.ACTIVITY_MIN_RESULTS_BEFORE_EXPAND

    # B9.1 — TRUE staged provider search. One real upstream request per rung, at
    # 8 km, then 12 km, then 20 km. Widen ONLY when the CURRENT rung yields fewer
    # than `want` valid, deduplicated, in-radius, category-relevant results; stop
    # at the first rung that clears the bar; stop unconditionally at 20 km. At
    # most len(ladder) == 3 sequential search_nearby calls — never a 4th, and
    # never 12/20 km when an earlier rung already succeeded. Each rung's response
    # is authoritative for THAT rung: responses are not accumulated across rungs,
    # because a wider provider response is independently capped / ranked / biased
    # and supersedes the narrower one rather than merely adding to it.
    ranked: list[dict] = []
    used_radius = ladder[0]
    try:
        for radius_m in ladder:
            used_radius = radius_m
            raw_places = await provider.search_nearby(
                latitude=lat, longitude=lng, activity=activity_id, radius_m=radius_m,
            )
            if profile.allows_public_geography and hasattr(provider, "search_texts"):
                capped_queries = list(profile.google_text_queries)[
                    : places_provider.SEARCH_TEXTS_MAX_QUERIES
                ]
                extra = await provider.search_texts(
                    latitude=lat, longitude=lng, activity=activity_id, radius_m=radius_m,
                    queries=capped_queries, max_queries=places_provider.SEARCH_TEXTS_MAX_QUERIES,
                )
                raw_places = _merge_places_by_id(raw_places, extra)
            else:
                # Dedupe this rung by stable provider place_id (an entry with no
                # id is dropped — the normaliser always sets one).
                raw_places = _merge_places_by_id(raw_places)

            # within_radius is the strict straight-line distance gate (it also
            # enforces the radius for providers that only *bias*, e.g. Google
            # Text Search); activity_core.rank owns the documented relevance
            # ordering + deduplication.
            in_radius = places.within_radius(raw_places, radius_m)
            ranked = activity_core.rank(
                activity_id, in_radius, distance_confident=distance_confident,
            )
            if len(ranked) >= want:
                break
    except places.ProviderError as e:
        print(f"ACTIVITY: provider={provider.name} activity={activity_id} error={e.kind}")  # kind only
        return ActivitySearchResult(
            status="PROVIDER_ERROR", activity_id=activity_id, provider=provider.name,
            origin_source=source, distance_confident=distance_confident,
            candidates=[], radius_m=used_radius,
            attribution=provider.attribution, error=e.kind,
        )

    return ActivitySearchResult(
        status="OK" if ranked else "NO_RESULTS",
        activity_id=activity_id, provider=provider.name, origin_source=source,
        distance_confident=distance_confident, candidates=ranked,
        radius_m=used_radius, attribution=provider.attribution, error=None,
    )


# --- Weather context contract (Phase 5F — contextual infrastructure) ---
class WeatherContextResult(BaseModel):
    status: str                          # "OK" | "PROVIDER_ERROR"
    provider: str                        # "open-meteo"
    attribution: str
    timezone: str                        # target-LOCATION local timezone (IANA)
    timezone_abbreviation: str = ""
    utc_offset_seconds: int = 0
    fetched_at: str                      # ISO-8601 UTC — forecast freshness
    stale: bool = False
    current: Optional[dict] = None       # provider's conditions "around now"
    window: Optional[dict] = None        # aggregated FORECAST over the requested window
    window_error: Optional[str] = None   # set when a window was requested but is unavailable
    error: Optional[str] = None          # a weather_core.WEATHER_* kind when status == PROVIDER_ERROR


_WINDOW_ERR_MSG = {
    weather_core.WeatherWindowError.WINDOW_MALFORMED: "start_time / end_time must be ISO-8601 timestamps.",
    weather_core.WeatherWindowError.WINDOW_NOT_TZ_AWARE: "start_time / end_time must include a timezone offset.",
    weather_core.WeatherWindowError.WINDOW_INVERTED: "end_time must be after start_time.",
    weather_core.WeatherWindowError.WINDOW_TOO_LONG: "The requested window is too long.",
}


async def _run_weather_context(raw: Any) -> WeatherContextResult:
    """Phase 5F — turn a real location (+ optional event window) into a
    normalised, deterministic weather context. Stateless; provider-neutral
    downstream; coordinates never logged (only provider name + error KIND);
    nothing persisted; no AI involvement."""
    if not isinstance(raw, dict):
        raise HTTPException(status_code=422, detail="Weather body must be a JSON object.")
    coords = validate_location_payload(raw.get("origin") if isinstance(raw.get("origin"), dict) else raw)
    lat, lng = coords["latitude"], coords["longitude"]

    start_raw, end_raw = raw.get("start_time"), raw.get("end_time")
    want_window = start_raw is not None or end_raw is not None
    start_utc = end_utc = None
    if want_window:
        try:
            start_utc, end_utc = weather_core.validate_window(start_raw, end_raw)
        except weather_core.WeatherWindowError as e:
            raise HTTPException(status_code=422, detail=_WINDOW_ERR_MSG.get(e.kind, "Invalid time window."))

    provider = weather_provider.get_weather_provider()
    try:
        forecast = await weather_provider.get_forecast(provider, lat, lng)
    except weather_core.WeatherProviderError as e:
        print(f"WEATHER: provider={provider.name} error={e.kind}")     # KIND only, no coords
        return WeatherContextResult(
            status="PROVIDER_ERROR", provider=provider.name, attribution=provider.attribution,
            timezone="", fetched_at=weather_core.now_utc().isoformat(), error=e.kind,
        )

    cur = forecast.get("current")
    current = None
    if isinstance(cur, dict):
        current = {
            "temperature_c": cur.get("temperature_c"),
            "apparent_temperature_c": cur.get("apparent_temperature_c"),
            "condition": cur.get("condition"),
            "wmo_code": cur.get("wmo_code"),
            "precipitation_mm": cur.get("precipitation_mm"),
            "wind_speed_kmh": cur.get("wind_speed_kmh"),
            "wind_gusts_kmh": cur.get("wind_gusts_kmh"),
            "is_day": cur.get("is_day"),
            "observed_at": cur.get("observed_at"),
        }

    window = None
    window_error = None
    if want_window:
        hours = forecast.get("hourly") or []
        _now = weather_core.now_utc()
        horizon_end = _now + datetime.timedelta(hours=weather_core.FORECAST_HORIZON_HOURS)
        if end_utc <= _now or start_utc >= horizon_end:
            window_error = weather_core.WEATHER_NO_FORECAST     # outside the supported forecast horizon
        else:
            sel = weather_core.select_window_hours(hours, start_utc, end_utc)
            agg = weather_core.aggregate_window(sel, start_utc, end_utc)
            if agg is None:
                window_error = weather_core.WEATHER_NO_FORECAST
            else:
                prim = weather_core.derive_primitives(agg)
                window = {
                    "requested_start": start_utc.isoformat(),
                    "requested_end": end_utc.isoformat(),
                    **agg,
                    "primitives": prim,
                    "phrases": weather_core.factual_phrases(agg, prim),
                }

    return WeatherContextResult(
        status="OK",
        provider=forecast["provider"], attribution=forecast["attribution"],
        timezone=forecast["timezone"], timezone_abbreviation=forecast.get("timezone_abbreviation", ""),
        utc_offset_seconds=forecast.get("utc_offset_seconds", 0),
        fetched_at=forecast["fetched_at"],
        stale=weather_core.is_stale(forecast["fetched_at"]),
        current=current, window=window, window_error=window_error, error=None,
    )


async def _run_geocode(raw: Any) -> GeoSearchResult:
    """Validate the query, call the geocoding provider abstraction, normalise the
    results. Stateless; the provider key stays server-side; coordinates are never
    logged; the raw provider status becomes a normalised PROVIDER_* kind."""
    if not isinstance(raw, dict):
        raise HTTPException(status_code=422, detail="Search body must be a JSON object.")
    q = raw.get("query")
    if not isinstance(q, str):
        raise HTTPException(status_code=422, detail="A search text is required.")
    q = q.strip()
    if len(q) < geocode.GEOCODE_QUERY_MIN:
        raise HTTPException(status_code=422, detail="Enter at least 2 characters.")
    if len(q) > geocode.GEOCODE_QUERY_MAX:
        raise HTTPException(status_code=422, detail="Search text is too long.")

    provider = geocode.get_geo_provider()
    try:
        rows = await provider.geocode(q)
    except places.ProviderError as e:
        print(f"GEOCODE: provider={provider.name} error={e.kind}")   # kind only, no query text/coords
        return GeoSearchResult(status="PROVIDER_ERROR", provider=provider.name, query=q,
                               results=[], attribution=provider.attribution, error=e.kind)
    return GeoSearchResult(status="OK" if rows else "NO_RESULTS", provider=provider.name, query=q,
                           results=list(rows), attribution=provider.attribution, error=None)


# --- Verified places contract (Phase 5C) ---
class PlacesSearchResult(BaseModel):
    status: str                          # "OK" | "NO_RESULTS" | "PROVIDER_ERROR"
    provider: str                        # "osm" | "google"
    activity: str
    radius_m: int
    places: list[dict] = []
    attribution: str = ""
    error: Optional[str] = None          # a places_core.PROVIDER_* kind when status == PROVIDER_ERROR


# --- Phase 5G: resolve ONE linked place on Schedule -> Map reopen ---
class PlaceResolveResult(BaseModel):
    status: str                          # "LIVE" | "SNAPSHOT"
    reason: Optional[str] = None         # why SNAPSHOT: a PROVIDER_* kind, PROVIDER_NO_MATCH,
                                         # PROVIDER_NOT_CONFIGURED, or NO_LIVE_PROVIDER
    provider: str                        # "google" | "osm"
    attribution: str = ""
    place: dict = {}                     # normalised place. LIVE carries volatile facts
                                         # (rating / open_now / hours); SNAPSHOT carries only
                                         # the stored stable identity + name/address/coords.


def _snapshot_place(ref: dict) -> dict:
    """Build a place object from ONLY the stored stable snapshot — provider
    identity plus the name / address / coordinates that were frozen when the
    event was linked. NO volatile facts: a caller must never render a rating,
    open-now or today's-hours value from a snapshot."""
    prov = ref["place_provider"]
    pid = ref["place_provider_id"]
    lat, lng = ref["place_lat"], ref["place_lng"]
    name = ref["location"]
    if prov == "google":
        maps = (f"https://www.google.com/maps/search/?api=1&query={quote(name)}"
                f"&query_place_id={quote(pid)}")
    else:
        maps = f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"
    return {
        "place_id": f"{prov}:{pid}",
        "provider": prov,
        "provider_place_id": pid,
        "name": name,
        "latitude": lat,
        "longitude": lng,
        "address": ref.get("place_address"),
        "maps_url": maps,
        "rating": None, "rating_count": None,
        "opening_hours": None, "open_now": None, "business_status": None,
        "website": None, "phone": None,
    }


async def _run_place_resolve(raw: Any) -> PlaceResolveResult:
    """Phase 5G — Schedule -> "View place": take the event's STORED place
    reference (identity + snapshot), try a live provider details lookup, and fall
    back TRUTHFULLY to the snapshot if the provider is unavailable / not
    configured / no longer knows the id. Never invents live metadata. Stateless;
    coordinates are never logged (only the provider name + error KIND)."""
    if not isinstance(raw, dict):
        raise HTTPException(status_code=422, detail="Resolve body must be a JSON object.")
    try:
        ref = place_ref.normalize_place_ref(raw)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    prov = ref["place_provider"]
    snap = _snapshot_place(ref)

    if prov != "google":
        # OSM (and any future provider that publishes no volatile facts): the
        # stored snapshot already IS the whole truth — there is nothing live to
        # refresh. Documented Phase 5G behaviour, not a failure.
        return PlaceResolveResult(
            status="SNAPSHOT", reason="NO_LIVE_PROVIDER", provider=prov,
            attribution=places_provider.OSM_ATTRIBUTION, place=snap,
        )

    gp = places_provider.GooglePlacesProvider()
    if not gp.is_configured():
        return PlaceResolveResult(
            status="SNAPSHOT", reason="PROVIDER_NOT_CONFIGURED", provider=prov,
            attribution=places_provider.GOOGLE_ATTRIBUTION, place=snap,
        )
    try:
        live = await gp.place_details(
            provider_place_id=ref["place_provider_id"],
            origin_lat=ref["place_lat"], origin_lng=ref["place_lng"],
        )
    except places.ProviderError as e:
        print(f"PLACE_RESOLVE: provider=google error={e.kind}")   # KIND only
        return PlaceResolveResult(
            status="SNAPSHOT", reason=e.kind, provider=prov,
            attribution=places_provider.GOOGLE_ATTRIBUTION, place=snap,
        )
    if live is None:
        return PlaceResolveResult(
            status="SNAPSHOT", reason="PROVIDER_NO_MATCH", provider=prov,
            attribution=places_provider.GOOGLE_ATTRIBUTION, place=snap,
        )
    return PlaceResolveResult(
        status="LIVE", reason=None, provider=prov,
        attribution=places_provider.GOOGLE_ATTRIBUTION, place=live,
    )


async def _run_place_search(user_id: str, raw: Any) -> PlacesSearchResult:
    """Shared by the endpoint. Validates the request, invokes the provider
    abstraction, normalises + bounds the results. No persistence, no coordinate
    logging.

    Two modes, chosen by the optional `adaptive_radius` request flag:
      * absent / false -> Phase 5C single-shot: ONE search_nearby at
        validate_radius_m(radius_m), finalize_results, no radius gate. Every
        pre-B10 caller keeps this exact behaviour.
      * true -> B10 staged adaptive parity with Activity: a genuine provider
        request at 8 km, then (only if < 3 valid) 12 km, then (only if still
        < 3) 20 km. Each rung's response is authoritative for that rung
        (NOT accumulated), strictly filtered by places.within_radius, then
        finalized. Stop at the first rung with >= 3; never a 4th rung; never
        start at 20 km. `radius_m` in the response is the rung it settled on.
    `adaptive_radius` and a caller-supplied `radius_m` are mutually exclusive
    (422) — an adaptive request must never silently discard a fixed radius.
    """
    coords = validate_location_payload(raw)          # Phase 5B authoritative check
    body = raw if isinstance(raw, dict) else {}
    try:
        activity = places.validate_activity(body.get("activity"))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    adaptive = body.get("adaptive_radius") is True
    if adaptive and body.get("radius_m") is not None:
        raise HTTPException(
            status_code=422,
            detail="radius_m cannot be combined with adaptive_radius.",
        )
    if not adaptive:
        try:
            radius_m = places.validate_radius_m(body.get("radius_m"))
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

    provider = places_provider.get_provider()
    lat, lng = coords["latitude"], coords["longitude"]

    if not adaptive:
        try:
            raw_places = await provider.search_nearby(
                latitude=lat, longitude=lng, activity=activity, radius_m=radius_m,
            )
        except places.ProviderError as e:
            # Log the KIND only (never coordinates); return a normalised outcome.
            print(f"PLACES: provider={provider.name} activity={activity} error={e.kind}")
            return PlacesSearchResult(
                status="PROVIDER_ERROR", provider=provider.name, activity=activity,
                radius_m=radius_m, places=[], attribution=provider.attribution, error=e.kind,
            )
        results = places.finalize_results(list(raw_places))
        return PlacesSearchResult(
            status="OK" if results else "NO_RESULTS",
            provider=provider.name, activity=activity, radius_m=radius_m,
            places=results, attribution=provider.attribution, error=None,
        )

    # ---- B10 staged adaptive search (Map/Explore parity with Activity) ----
    ladder = places.ACTIVITY_RADIUS_LADDER_M          # (8_000, 12_000, 20_000)
    want = places.ACTIVITY_MIN_RESULTS_BEFORE_EXPAND  # 3
    results: list[dict] = []
    used_radius = ladder[0]
    try:
        for radius_m in ladder:
            used_radius = radius_m
            rung_places = await provider.search_nearby(
                latitude=lat, longitude=lng, activity=activity, radius_m=radius_m,
            )
            # This rung's response is the whole truth for this rung: strictly
            # gate it by the CoreOS-computed straight-line distance (this is the
            # real enforcement for a provider that only *biases*, e.g. Google
            # Text Search), then dedupe + nearest-first + cap. No accumulation.
            in_radius = places.within_radius(list(rung_places), radius_m)
            results = places.finalize_results(in_radius)
            if len(results) >= want:
                break
    except places.ProviderError as e:
        print(f"PLACES: provider={provider.name} activity={activity} error={e.kind}")
        return PlacesSearchResult(
            status="PROVIDER_ERROR", provider=provider.name, activity=activity,
            radius_m=used_radius, places=[], attribution=provider.attribution, error=e.kind,
        )
    return PlacesSearchResult(
        status="OK" if results else "NO_RESULTS",
        provider=provider.name, activity=activity, radius_m=used_radius,
        places=results, attribution=provider.attribution, error=None,
    )


# --- NEW AI CONTRACT BLUEPRINTS ---
class IntentEnum(str, Enum):
    CREATE_TRANSACTION = "CREATE_TRANSACTION"
    GET_SPENDING_SUMMARY = "GET_SPENDING_SUMMARY"
    CREATE_TASK = "CREATE_TASK"
    CREATE_REMINDER = "CREATE_REMINDER"
    CREATE_EVENT = "CREATE_EVENT"
    GET_SCHEDULE_SUMMARY = "GET_SCHEDULE_SUMMARY"
    UPDATE_TASK = "UPDATE_TASK"
    DELETE_TASK = "DELETE_TASK"
    UPDATE_EVENT = "UPDATE_EVENT"
    DELETE_EVENT = "DELETE_EVENT"
    GET_TODAY_SUMMARY = "GET_TODAY_SUMMARY"
    # Mo smartness + experience closure: bounded, read-only additions.
    GENERAL_UTILITY = "GENERAL_UTILITY"        # arithmetic / conversions / definitions
    SUGGEST_FREE_TIME = "SUGGEST_FREE_TIME"    # "when should I study" — real free-window judgment
    CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"
    ACTION_NOT_SUPPORTED = "ACTION_NOT_SUPPORTED"

# NOTE (Phase 3): the canonical transaction taxonomy + validation now live in
# finance_core (CANONICAL_CATEGORIES / normalize_category / to_sen) and
# _validate_transaction_fields(). The old TransactionCategory enum and
# TransactionParams model were only ever used to validate AI CREATE_TRANSACTION
# output; that path was migrated to _validate_transaction_fields, so they were
# removed here as dead code.

class TaskParams(BaseModel):
    title: str = Field(description="The title of the task to complete.")
    due_date: Optional[str] = Field(None, description="ISO-8601 formatted due date, if mentioned.")
    reminder_at: Optional[str] = Field(None, description="ISO-8601 formatted reminder date and time, if mentioned.")
    planned_at: Optional[str] = Field(None, description="ISO-8601 time the user plans to do it, if mentioned.")
    notes: Optional[str] = Field(None, description="Any extra detail the user mentioned.")

class EventParams(BaseModel):
    title: str = Field(description="The event title.")
    start: str = Field(description="ISO-8601 start datetime with timezone.")
    end: Optional[str] = Field(None, description="ISO-8601 end datetime, if given.")
    location: Optional[str] = Field(None, description="Human-readable location, if given.")
    all_day: Optional[bool] = Field(False, description="True for an all-day event.")

class AIResponse(BaseModel):
    intent: IntentEnum
    parameters: Optional[Dict[str, Any]] = None
    clarification_question: Optional[str] = None

# Mo final intelligence + multi-operation closure. One OpenAI call returns a
# SINGLE MoInterpretation carrying ONE OR MORE MoOperation entries — the
# generalized replacement for the old "always exactly one intent" AIResponse.
# A single-item request is just a one-element operations[] list; nothing about
# the per-operation validation, proposal shape, or execution path changes for
# that case (see AIService.interpret / _mo_understand_operations).
MO_MODES = ("read", "single_operation", "multi_operation", "plan_day",
            "clarification", "unsupported")

class MoOperation(BaseModel):
    intent: IntentEnum
    parameters: Optional[Dict[str, Any]] = None
    # Only meaningful for CREATE_EVENT/CREATE_TASK inside a "plan_day" request:
    # "hard" (fixed, do not move) or "soft" (may be nudged to a nearby free
    # slot if it conflicts). Missing/unrecognized -> treated as "hard" (an
    # explicit time is fixed unless the user's phrasing says otherwise).
    constraint: Optional[str] = None
    # The ONLY supported conditional: "if_free" — propose this item only if it
    # does not conflict with the user's real schedule; otherwise it is excluded
    # (never silently created, never offered an alternate slot). No other
    # conditional value is recognized.
    condition: Optional[str] = None

class MoInterpretation(BaseModel):
    mode: str
    operations: List[MoOperation] = []
    # Short human descriptions of parts of the message Mo recognized but
    # cannot act on (purchases, payments, banking, profile/settings changes,
    # anything outside ALLOWED INTENTS) — e.g. ["buying football shoes"].
    # Final correctness patch: a mixed request used to just OMIT the
    # unsupported half from operations[] with nothing else marking it, so it
    # was silently dropped from the user's point of view. Every such part must
    # now be named here instead, and _mo_understand always surfaces it in the
    # returned message — never authoritative, never actioned, just honest.
    unsupported_items: List[str] = []
    # A short explanatory line for a plan_day response. NEVER treated as
    # authoritative data — every number/date/conflict the user sees still
    # comes from deterministic backend computation, this is framing only.
    narrative: Optional[str] = None
    clarification_question: Optional[str] = None

class ActionOutcome(BaseModel):
    status: str
    message: str
    data: Optional[Dict[str, Any]] = None

def get_finance_snapshot(user_id: str) -> dict:
    """Phase 3: the single authoritative Finance computation lives in
    finance_core.get_snapshot (money-as-sen, MYT period boundaries). This
    wrapper keeps the historical name + adds the legacy `balance` alias."""
    snap = fin.get_snapshot(user_id)
    snap["balance"] = snap["available_balance"]  # backwards-compatible alias
    return snap


def _validate_transaction_fields(
    *, amount=None, type_=None, category=None, description=None, date=None,
    require_amount: bool = True,
) -> dict:
    """Normalize + validate one transaction's fields. Raises HTTPException(422)
    on bad input. Returns only the fields that were provided (plus amount_sen)."""
    out: dict = {}
    if amount is not None or require_amount:
        try:
            sen = fin.to_sen(amount)
        except fin.MoneyError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if sen <= 0:
            raise HTTPException(status_code=422, detail="Amount must be greater than zero.")
        out["amount_sen"] = sen
        out["amount"] = fin.from_sen(sen)
    if type_ is not None:
        out["type"] = fin.normalize_type(type_)
    if category is not None:
        out["category"] = fin.normalize_category(category)
    if description is not None:
        desc = str(description).strip()
        if len(desc) > 140:
            raise HTTPException(status_code=422, detail="Description is too long (max 140 characters).")
        out["description"] = desc
    if date is not None:
        try:
            out["date"] = fin.normalize_iso_datetime(date, default_now=True)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    elif require_amount:
        # create context, no date supplied -> now (MYT)
        out["date"] = fin.myt_now().isoformat()
    return out

# --- 2b. AUTHENTICATION ENDPOINTS (PHASE 2) ---
def _public_user(row) -> dict:
    """Only non-sensitive fields ever leave the server."""
    return {"id": row["id"], "email": row["email"], "name": row["name"]}


def _client_ip(request: Request) -> str:
    # The dev topology is browser -> Caddy (dev/Caddyfile) -> 127.0.0.1:8080.
    # Verified against Caddy v2.11.4: Caddy DISCARDS any client-supplied
    # X-Forwarded-For and emits only the address it observed, and uvicorn's
    # default proxy-header handling (trusted: 127.0.0.1) adopts that. So this is
    # the true client through the front door; a caller cannot rotate it. A
    # direct hit on :8080 is prevented by the loopback bind (see HOST above).
    return (request.client.host if request.client else "unknown")


@app.post("/api/auth/register", status_code=status.HTTP_201_CREATED)
def register(req: RegisterRequest, request: Request, response: Response):
    if not auth_core.rate_limit_check(f"register:{_client_ip(request)}"):
        raise HTTPException(status_code=429, detail="Too many attempts. Please wait and try again.")
    try:
        email_norm = auth_core.normalize_and_validate_email(req.email)
        auth_core.validate_password(req.password)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    pw_hash = auth_core.hash_password(req.password)
    uid = "usr_" + uuid.uuid4().hex
    now = get_iso_now()
    display_name = (req.name or req.email.split("@")[0]).strip()[:120]

    conn = get_db()
    try:
        existing = conn.execute(
            "SELECT 1 FROM users WHERE email_normalized = ?", (email_norm,)
        ).fetchone()
        if existing:
            # Registration inherently reveals existence; keep the message plain.
            raise HTTPException(status_code=409, detail="An account with this email already exists.")
        conn.execute(
            """INSERT INTO users (id, name, email, email_normalized, password_hash,
                                  is_active, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
            (uid, display_name, req.email.strip(), email_norm, pw_hash, now, now),
        )
        conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        print(f"AUTH_ERROR (register): {e}")
        raise HTTPException(status_code=500, detail="Couldn't create your account. Please try again.")
    finally:
        conn.close()

    raw_token, _ = auth_core.create_session(uid, request.headers.get("user-agent"))
    _set_session_cookie(response, raw_token)
    auth_core.rate_limit_reset(f"register:{_client_ip(request)}")
    return {"user": {"id": uid, "email": req.email.strip(), "name": display_name}}


@app.post("/api/auth/login")
def login(req: LoginRequest, request: Request, response: Response):
    ip_key = f"login:{_client_ip(request)}"
    if not auth_core.rate_limit_check(ip_key):
        raise HTTPException(status_code=429, detail="Too many attempts. Please wait and try again.")

    # Normalize but do NOT reveal validation specifics on the login path.
    try:
        email_norm = auth_core.normalize_and_validate_email(req.email)
    except ValueError:
        email_norm = req.email.strip().lower()

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, name, email, password_hash, is_active FROM users WHERE email_normalized = ?",
            (email_norm,),
        ).fetchone()
    finally:
        conn.close()

    GENERIC = "Invalid email or password."
    if row is None or not row["is_active"] or not row["password_hash"]:
        # Uniform response for: no account / disabled / not-yet-bootstrapped.
        raise HTTPException(status_code=401, detail=GENERIC)
    if not auth_core.verify_password(row["password_hash"], req.password):
        raise HTTPException(status_code=401, detail=GENERIC)

    raw_token, _ = auth_core.create_session(row["id"], request.headers.get("user-agent"))
    _set_session_cookie(response, raw_token)
    auth_core.rate_limit_reset(ip_key)
    return {"user": _public_user(row)}


@app.get("/api/auth/me")
def auth_me(current: dict = Depends(get_current_user)):
    return {"user": current}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(sec.COOKIE_NAME)
    if token:
        auth_core.revoke_session(token)
    _clear_session_cookie(response)
    return {"message": "Signed out."}


# Profile — Batch 01: the authenticated user may edit their own display name
# only. Email is intentionally not accepted here (it is the account's login
# identity and changing it belongs to a future, separately-verified flow).
# `current["id"]` is the session-resolved user id (never taken from the
# request body), so this can never update another account's row.
PROFILE_NAME_MAX_LENGTH = 120


@app.patch("/api/profile")
def update_profile(req: ProfileUpdate, current: dict = Depends(get_current_user)):
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Name cannot be blank.")
    if len(name) > PROFILE_NAME_MAX_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=f"Name must be at most {PROFILE_NAME_MAX_LENGTH} characters.",
        )

    try:
        with db_layer.transaction() as conn:
            cur = conn.execute(
                "UPDATE users SET name = ?, updated_at = ? WHERE id = ?",
                (name, get_iso_now(), current["id"]),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Account not found.")
            row = conn.execute(
                "SELECT id, name, email FROM users WHERE id = ?", (current["id"],)
            ).fetchone()
    except HTTPException:
        raise
    except Exception as e:
        print(f"DB_ERROR (update_profile): {str(e)}")
        raise HTTPException(status_code=500, detail="Couldn't save your name. Please try again.")

    return {"user": _public_user(row)}


def _verify_current_password(user_id: str, plain: str) -> None:
    """Shared re-authentication check for the three account-security actions
    below. Raises 401 on a missing/inactive account or a wrong password —
    never reveals which, matching the login endpoint's generic-failure style."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT password_hash, is_active FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    finally:
        conn.close()
    if (
        row is None
        or not row["is_active"]
        or not row["password_hash"]
        or not auth_core.verify_password(row["password_hash"], plain)
    ):
        raise HTTPException(status_code=401, detail="Current password is incorrect.")


@app.post("/api/auth/change-password")
def change_password(
    req: ChangePasswordRequest, request: Request, response: Response,
    current: dict = Depends(get_current_user),
):
    user_id = current["id"]
    if not auth_core.rate_limit_check(f"change_password:{user_id}"):
        raise HTTPException(status_code=429, detail="Too many attempts. Please wait and try again.")

    _verify_current_password(user_id, req.current_password)

    try:
        auth_core.validate_password(req.new_password)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    new_hash = auth_core.hash_password(req.new_password)
    try:
        with db_layer.transaction() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (new_hash, get_iso_now(), user_id),
            )
    except Exception as e:
        print(f"AUTH_ERROR (change_password): {e}")
        raise HTTPException(status_code=500, detail="Couldn't change your password. Please try again.")

    # A changed password invalidates every existing session (this device's
    # old cookie included) — the caller just re-proved their identity, so
    # issue a fresh one immediately rather than forcing an extra login.
    auth_core.revoke_all_sessions(user_id)
    raw_token, _ = auth_core.create_session(user_id, request.headers.get("user-agent"))
    _set_session_cookie(response, raw_token)
    auth_core.rate_limit_reset(f"change_password:{user_id}")
    return {"message": "Password changed. You've been signed out of other devices."}


@app.post("/api/auth/change-email")
def change_email(req: ChangeEmailRequest, current: dict = Depends(get_current_user)):
    user_id = current["id"]
    if not auth_core.rate_limit_check(f"change_email:{user_id}"):
        raise HTTPException(status_code=429, detail="Too many attempts. Please wait and try again.")

    _verify_current_password(user_id, req.current_password)

    try:
        email_norm = auth_core.normalize_and_validate_email(req.new_email)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    new_email = req.new_email.strip()
    try:
        with db_layer.transaction() as conn:
            existing = conn.execute(
                "SELECT 1 FROM users WHERE email_normalized = ? AND id != ?",
                (email_norm, user_id),
            ).fetchone()
            if existing:
                raise HTTPException(status_code=409, detail="An account with this email already exists.")
            conn.execute(
                "UPDATE users SET email = ?, email_normalized = ?, updated_at = ? WHERE id = ?",
                (new_email, email_norm, get_iso_now(), user_id),
            )
    except HTTPException:
        raise
    except Exception as e:
        print(f"AUTH_ERROR (change_email): {e}")
        raise HTTPException(status_code=500, detail="Couldn't change your email. Please try again.")

    auth_core.rate_limit_reset(f"change_email:{user_id}")
    return {"user": {"id": user_id, "email": new_email, "name": current["name"]}}


# Tables that carry a user_id column but — per DATABASE_SCHEMA_REPORT.md —
# have no DB-declared FOREIGN KEY / ON DELETE CASCADE back to users(id) (an
# application-level reference only). Every other user_id-bearing table
# (schedule_events, tasks, transactions, sessions, user_financial_settings,
# user_interests, user_preferences, map_locations, ai_recommendations) DOES
# cascade automatically when the users row is deleted, since db.py turns on
# PRAGMA foreign_keys for every connection. Deleting these first in the same
# transaction is what makes account deletion actually complete rather than
# leaving orphaned proposal/conflict-decision rows behind.
_ACCOUNT_DELETE_ORPHAN_TABLES = (
    "mo_context",
    "mo_proposals",
    "schedule_conflict_decisions",
    "schedule_occurrence_overrides",
    "task_event_conflict_decisions",
    "task_move_proposals",
    "weather_decisions",
    "weather_move_proposals",
)


@app.post("/api/auth/delete-account")
def delete_account(
    req: DeleteAccountRequest, response: Response, current: dict = Depends(get_current_user),
):
    user_id = current["id"]
    if not auth_core.rate_limit_check(f"delete_account:{user_id}"):
        raise HTTPException(status_code=429, detail="Too many attempts. Please wait and try again.")

    _verify_current_password(user_id, req.current_password)

    try:
        with db_layer.transaction() as conn:
            for table in _ACCOUNT_DELETE_ORPHAN_TABLES:
                conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
            cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            deleted = cur.rowcount
    except Exception as e:
        print(f"AUTH_ERROR (delete_account): {e}")
        raise HTTPException(status_code=500, detail="Couldn't delete your account. Please try again.")

    if not deleted:
        raise HTTPException(status_code=404, detail="Account not found.")

    # The users-row delete already cascaded every session row away (incl. this
    # one), but the browser still holds the cookie until told otherwise.
    _clear_session_cookie(response)
    return {"message": "Account deleted."}


def _get_user_interests(user_id: str) -> Dict[str, List[str]]:
    """Every interest the user explicitly saved, grouped by category.
    Always returns all four category keys (possibly empty lists), never a
    partial shape — the canonical response both the API and Mo's context
    assembly read from."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT category, value FROM user_interests WHERE user_id = ? "
            "ORDER BY category, value",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    result: Dict[str, List[str]] = {cat: [] for cat in INTEREST_CATEGORIES}
    for row in rows:
        if row["category"] in result:
            result[row["category"]].append(row["value"])
    return result


@app.get("/api/profile/interests")
def get_interests(current: dict = Depends(get_current_user)):
    return {"interests": _get_user_interests(current["id"])}


@app.put("/api/profile/interests")
def update_interests(req: InterestsUpdate, current: dict = Depends(get_current_user)):
    """Idempotent per-category replace. A category omitted (None) from the
    request body is left untouched; a category present (even []) is fully
    replaced with exactly the given, validated, deduplicated values. Every
    value must belong to that category's curated INTEREST_OPTIONS — this can
    never persist free text. `current["id"]` (session-resolved) is the only
    source of user_id, so this can never write another account's row."""
    user_id = current["id"]
    provided = {
        "game": req.game,
        "sport": req.sport,
        "movie_tv": req.movie_tv,
        "general": req.general,
    }

    to_write: Dict[str, List[str]] = {}
    for category, values in provided.items():
        if values is None:
            continue  # category not touched by this request
        allowed = INTEREST_OPTIONS[category]
        deduped: List[str] = []
        for raw in values:
            v = (raw or "").strip()
            if not v:
                continue
            if len(v) > INTEREST_VALUE_MAX_LENGTH:
                raise HTTPException(
                    status_code=422,
                    detail=f"Interest value too long for {category}.",
                )
            if v not in allowed:
                raise HTTPException(
                    status_code=422,
                    detail=f"'{v}' is not a recognised {category} interest.",
                )
            if v not in deduped:
                deduped.append(v)
        to_write[category] = deduped

    if not to_write:
        return {"interests": _get_user_interests(user_id)}

    try:
        with db_layer.transaction() as conn:
            now = get_iso_now()
            for category, values in to_write.items():
                conn.execute(
                    "DELETE FROM user_interests WHERE user_id = ? AND category = ?",
                    (user_id, category),
                )
                for v in values:
                    conn.execute(
                        "INSERT INTO user_interests "
                        "(id, user_id, category, value, created_at) VALUES (?, ?, ?, ?, ?)",
                        (f"int_{uuid.uuid4().hex}", user_id, category, v, now),
                    )
    except HTTPException:
        raise
    except Exception as e:
        print(f"DB_ERROR (update_interests): {str(e)}")
        raise HTTPException(status_code=500, detail="Couldn't save your interests. Please try again.")

    return {"interests": _get_user_interests(user_id)}


_INTEREST_CATEGORY_LABELS = {
    "game": "Games",
    "sport": "Sports",
    "movie_tv": "Movies & TV",
    "general": "General",
}


def _mo_interests_context_line(interests: dict) -> str:
    """Pure, deterministic: turns the user's explicitly SAVED Who-Am-I
    interests into a short system-prompt line Mo may use as a soft
    personalization signal. Never claims Mo "learned" or "remembered" these
    from conversation, never lets them override an explicit request, and is
    silent (empty string) when nothing is saved — Mo behaves exactly as
    before in that case. No network call, no side effect — safe to unit
    test directly."""
    parts = []
    for category in INTEREST_CATEGORIES:
        values = interests.get(category) or []
        if values:
            parts.append(f"{_INTEREST_CATEGORY_LABELS[category]}: {', '.join(values)}")
    if not parts:
        return ""
    return (
        "\nUSER-DECLARED INTERESTS (from their MONOA profile — a soft "
        "personalization signal only. The user explicitly chose these; you "
        "did NOT learn, infer or remember them from this conversation, and "
        "must never claim you did. Use them ONLY to add light, relevant "
        "color when the user asks for a suggestion or recommendation where "
        "they are genuinely relevant — never mention them for an unrelated "
        "request, and never let them override an explicit different request "
        "the user just made.)\n"
        + "\n".join(f"  {p}" for p in parts) + "\n"
    )


# --- NEW DATABASE SERVICES (SAFE EXECUTION) ---
def _insert_transaction(user_id: str, fields: dict, *,
                        client_token: Optional[str] = None,
                        client_request_hash: Optional[str] = None) -> dict:
    """Insert one fully-validated transaction (fields from _validate_transaction_fields).
    Writes both `amount` (major, 2dp) and `amount_sen` (authoritative). Returns the row.
    `client_token` (Phase 5H-B3) is the create-once idempotency key; the AI path
    leaves it None (an untokened create is never deduplicated)."""
    tx_id = str(uuid.uuid4())
    now = get_iso_now()
    amount_sen = fields["amount_sen"]
    with db_layer.transaction() as conn:
        conn.execute(
            """INSERT INTO transactions
               (id, user_id, amount, amount_sen, category, date, type, description, merchant,
                created_at, updated_at, client_token, client_request_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (tx_id, user_id, fin.from_sen(amount_sen), amount_sen,
             fields.get("category", "Other"), fields["date"],
             fields.get("type", "expense"), fields.get("description", ""), "", now, now,
             client_token, client_request_hash),
        )
    return _get_owned_transaction(user_id, tx_id)


def _get_owned_transaction(user_id: str, tx_id: str) -> Optional[dict]:
    conn = get_db()
    try:
        r = conn.execute(
            "SELECT id, amount, amount_sen, type, category, description, date, created_at, updated_at "
            "FROM transactions WHERE id = ? AND user_id = ?", (tx_id, user_id)
        ).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


class AppServices:
    @staticmethod
    def create_transaction(user_id: str, fields: dict, *,
                           client_token: Optional[str] = None,
                           client_request_hash: Optional[str] = None) -> ActionOutcome:
        """`fields` is the output of _validate_transaction_fields (already
        normalized/validated). Used by BOTH the manual endpoint and the AI path,
        so AI-created and hand-entered transactions are identical. Phase 5H-B3:
        `client_token` (from the Omni-bar submission) makes the create idempotent
        — a retried command after a committed-but-lost response returns the first
        transaction instead of a duplicate."""
        def _built(pid):
            row = _get_owned_transaction(user_id, pid)
            amt = fin.fmt(fields["amount_sen"])
            desc = fields.get("description") or fields.get("category", "transaction")
            return ActionOutcome(status="SUCCESS", message=f"Logged RM {amt} for {desc}.",
                                 data={"transaction": row, "idempotent": True})

        replay = _idem_prior_outcome("transactions", user_id, client_token,
                                     client_request_hash, _built)
        if replay is not None:
            return replay
        try:
            row = _insert_transaction(user_id, fields, client_token=client_token,
                                      client_request_hash=client_request_hash)
            amt = fin.fmt(fields["amount_sen"])
            desc = fields.get("description") or fields.get("category", "transaction")
            return ActionOutcome(status="SUCCESS",
                                 message=f"Logged RM {amt} for {desc}.",
                                 data={"transaction": row})
        except sqlite3.IntegrityError:
            replay = _idem_prior_outcome("transactions", user_id, client_token,
                                         client_request_hash, _built)
            if replay is not None:
                return replay
            print("DB_ERROR (create_transaction): integrity")
            return ActionOutcome(status="DATABASE_ERROR",
                                 message="I couldn't save that transaction right now. Please try again.")
        except Exception as e:
            print(f"DB_ERROR (create_transaction): {str(e)}")
            return ActionOutcome(status="DATABASE_ERROR",
                                 message="I couldn't save that transaction right now. Please try again.")

    @staticmethod
    def create_task(user_id: str, params: TaskParams, is_reminder: bool = False, *,
                    client_token: Optional[str] = None,
                    client_request_hash: Optional[str] = None) -> ActionOutcome:
        msg = "Reminder set." if is_reminder or params.reminder_at else "Task created."

        def _built(pid):
            return ActionOutcome(status="SUCCESS", message=msg, data={"id": pid, "idempotent": True})

        replay = _idem_prior_outcome("tasks", user_id, client_token, client_request_hash, _built)
        if replay is not None:
            return replay

        task_id = str(uuid.uuid4())
        now = get_iso_now()
        reminder_status = "PENDING" if params.reminder_at else "NONE"
        try:
            with db_layer.transaction() as conn:
                conn.execute(
                    """INSERT INTO tasks
                    (id, user_id, title, is_completed, due_date, reminder_at,
                     reminder_status, created_at, updated_at, client_token, client_request_hash)
                    VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)""",
                    (task_id, user_id, params.title, params.due_date, params.reminder_at,
                     reminder_status, now, now, client_token, client_request_hash),
                )
            return ActionOutcome(status="SUCCESS", message=msg, data={"id": task_id})
        except sqlite3.IntegrityError:
            replay = _idem_prior_outcome("tasks", user_id, client_token, client_request_hash, _built)
            if replay is not None:
                return replay
            print("DB_ERROR (create_task): integrity")
            return ActionOutcome(status="DATABASE_ERROR", message="I couldn't save that task. Please try again.")
        except Exception as e:
            print(f"DB_ERROR: {str(e)}")
            return ActionOutcome(status="DATABASE_ERROR", message="I couldn't save that task. Please try again.")

    @staticmethod
    def get_spending_summary(user_id: str, current_time_iso: str = "") -> ActionOutcome:
        """AI 'how much did I spend' — reads the ONE authoritative snapshot."""
        try:
            snap = fin.get_snapshot(user_id)
            today = fin.fmt(fin.to_sen(snap["today_spending"]))
            month = fin.fmt(fin.to_sen(snap["month_spending"]))
            return ActionOutcome(
                status="SUCCESS",
                message=f"You've spent RM {today} today and RM {month} so far this month.",
                data={"today_spending": snap["today_spending"], "month_spending": snap["month_spending"]},
            )
        except Exception as e:
            print(f"DB_ERROR (get_spending_summary): {str(e)}")
            return ActionOutcome(status="DATABASE_ERROR", message="I couldn't retrieve your spending summary.")

    @staticmethod
    def create_event(user_id: str, fields: dict, *,
                     client_token: Optional[str] = None,
                     client_request_hash: Optional[str] = None) -> ActionOutcome:
        """`fields` is the output of _validate_event_fields (already validated).
        Same insert path as the manual POST /api/events endpoint. Phase 5H-B3:
        an Omni-bar retry with the same client_token returns the first event."""
        start = fields["start_time"]

        def _built(pid):
            when = datetime.datetime.fromisoformat(start).strftime("%a %d %b, %I:%M %p")
            return ActionOutcome(status="SUCCESS",
                                 message=f"Added “{fields['title']}” on {when}.",
                                 data={"event_id": pid, "idempotent": True})

        replay = _idem_prior_outcome("schedule_events", user_id, client_token,
                                     client_request_hash, _built)
        if replay is not None:
            return replay

        ev_id = str(uuid.uuid4())
        now = get_iso_now()
        end = fields.get("end_time")
        if not end and not fields.get("all_day"):
            st = datetime.datetime.fromisoformat(start)
            end = (st + datetime.timedelta(hours=1)).isoformat()
        try:
            with db_layer.transaction() as conn:
                conn.execute(
                    """INSERT INTO schedule_events
                       (id, user_id, title, location, start_time, end_time, notes, all_day,
                        status, source_type, external_id, recurrence, recurrence_until,
                        related_task_id, created_at, updated_at, client_token, client_request_hash)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', 'coreos', NULL, ?, ?, NULL, ?, ?, ?, ?)""",
                    (ev_id, user_id, fields["title"], fields.get("location"), start, end,
                     fields.get("notes"), fields.get("all_day", 0),
                     fields.get("recurrence", "none"), fields.get("recurrence_until"), now, now,
                     client_token, client_request_hash),
                )
            when = datetime.datetime.fromisoformat(start).strftime("%a %d %b, %I:%M %p")
            return ActionOutcome(status="SUCCESS",
                                 message=f"Added “{fields['title']}” on {when}.",
                                 data={"event_id": ev_id})
        except sqlite3.IntegrityError:
            replay = _idem_prior_outcome("schedule_events", user_id, client_token,
                                         client_request_hash, _built)
            if replay is not None:
                return replay
            print("DB_ERROR (create_event): integrity")
            return ActionOutcome(status="DATABASE_ERROR",
                                 message="I couldn't save that event right now. Please try again.")
        except Exception as e:
            print(f"DB_ERROR (create_event): {str(e)}")
            return ActionOutcome(status="DATABASE_ERROR",
                                 message="I couldn't save that event right now. Please try again.")

    @staticmethod
    def get_schedule_summary(user_id: str, date: Optional[str] = None) -> ActionOutcome:
        """AI 'what's my day looking like' — reads the deterministic agenda for
        `date` (YYYY-MM-DD), defaulting to today when omitted (unchanged
        behavior for every pre-existing caller)."""
        try:
            today = sched.myt_now().date().isoformat()
            day = date or today
            f_iso, t_iso = sched.myt_day_bounds(day)
            data = _load_schedule(user_id, f_iso, t_iso)
            evs = [e for e in data["events"] if e.get("status") != "cancelled"]
            open_tasks = [t for t in data["tasks"] if not t["is_completed"]]
            if day == today:
                label = "Today"
            else:
                label = datetime.date.fromisoformat(day).strftime("%A, %d %b")
            if not evs and not open_tasks:
                msg = f"{label} is clear — no events and no open tasks." if day != today \
                    else "Your day is clear — no events and no open tasks."
            else:
                parts = []
                if evs:
                    nxt = evs[0]
                    t = datetime.datetime.fromisoformat(nxt["start"]).strftime("%I:%M %p")
                    parts.append(f"{len(evs)} event(s); next is “{nxt['title']}” at {t}")
                if open_tasks:
                    parts.append(f"{len(open_tasks)} open task(s)")
                if data["conflicts"]:
                    parts.append(f"{len(data['conflicts'])} overlap(s) to resolve")
                msg = f"{label}: " + "; ".join(parts) + "."
            return ActionOutcome(status="SUCCESS", message=msg,
                                 data={"events": len(evs), "open_tasks": len(open_tasks),
                                       "conflicts": len(data["conflicts"])})
        except Exception as e:
            print(f"DB_ERROR (get_schedule_summary): {str(e)}")
            return ActionOutcome(status="DATABASE_ERROR", message="I couldn't read your schedule.")

    @staticmethod
    def get_today_summary(user_id: str) -> ActionOutcome:
        """AI 'what do I have today' — a lightweight combined view built from the
        SAME deterministic reads as get_schedule_summary + get_spending_summary.
        Invents nothing beyond those two real data sources."""
        try:
            today = sched.myt_now().date().isoformat()
            f_iso, t_iso = sched.myt_day_bounds(today)
            data = _load_schedule(user_id, f_iso, t_iso)
            evs = [e for e in data["events"] if e.get("status") != "cancelled"]
            open_tasks = [t for t in data["tasks"] if not t["is_completed"]]
            snap = fin.get_snapshot(user_id)
            today_spend = fin.fmt(fin.to_sen(snap["today_spending"]))

            parts = []
            if evs:
                nxt = evs[0]
                t = datetime.datetime.fromisoformat(nxt["start"]).strftime("%I:%M %p")
                parts.append(f"{len(evs)} event(s), next “{nxt['title']}” at {t}")
            if open_tasks:
                parts.append(f"{len(open_tasks)} open task(s)")
            if data["conflicts"]:
                parts.append(f"{len(data['conflicts'])} overlap(s) to resolve")
            schedule_part = "; ".join(parts) if parts else "a clear schedule"
            msg = f"Today: {schedule_part}. You've spent RM {today_spend} so far today."
            return ActionOutcome(
                status="SUCCESS", message=msg,
                data={"events": len(evs), "open_tasks": len(open_tasks),
                      "conflicts": len(data["conflicts"]), "today_spending": snap["today_spending"]},
            )
        except Exception as e:
            print(f"DB_ERROR (get_today_summary): {str(e)}")
            return ActionOutcome(status="DATABASE_ERROR", message="I couldn't put together today's summary.")

# --- 3. PLANNING & SCHEDULE ENGINE (PHASE 4) ---
_EVENT_COLS = ("id, user_id, title, location, start_time, end_time, notes, all_day, "
               "status, source_type, external_id, recurrence, recurrence_until, "
               "related_task_id, created_at, updated_at, "
               "place_provider, place_provider_id, place_lat, place_lng, place_address, "
               "environment")
_TASK_COLS = ("id, user_id, title, notes, is_completed, due_date, planned_at, "
              "scheduled_end, reminder_at, reminder_status, completed_at, created_at, updated_at")


def _event_out(row) -> dict:
    d = dict(row)
    d.pop("user_id", None)
    d["all_day"] = bool(d.get("all_day"))
    # Phase 5G: expose the linked real place as ONE nested object; keep the raw
    # place_* columns out of the consumer shape.
    d["place"] = place_ref.place_out(d)
    for c in ("place_provider", "place_provider_id", "place_lat", "place_lng", "place_address"):
        d.pop(c, None)
    return d


def _task_out(row) -> dict:
    d = dict(row)
    d.pop("user_id", None)
    d["is_completed"] = bool(d.get("is_completed"))
    return d


def _get_owned_event(user_id: str, event_id: str) -> Optional[dict]:
    conn = get_db()
    try:
        r = conn.execute(
            f"SELECT {_EVENT_COLS} FROM schedule_events WHERE id = ? AND user_id = ?",
            (event_id, user_id),
        ).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _get_owned_task(user_id: str, task_id: str) -> Optional[dict]:
    conn = get_db()
    try:
        r = conn.execute(
            f"SELECT {_TASK_COLS} FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        ).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _validate_event_fields(user_id: str, *, require_start: bool, **raw) -> dict:
    """Normalize + validate an event's supplied fields. Raises HTTPException(422)
    on bad input; 404 if related_task_id is not owned. Shared by the manual
    endpoints and the AI CREATE_EVENT path."""
    out: dict = {}

    if "title" in raw and raw["title"] is not None:
        t = str(raw["title"]).strip()
        if not t:
            raise HTTPException(status_code=422, detail="Title cannot be empty.")
        if len(t) > 200:
            raise HTTPException(status_code=422, detail="Title is too long (max 200 characters).")
        out["title"] = t
    elif require_start:
        raise HTTPException(status_code=422, detail="Title is required.")

    all_day = bool(raw.get("all_day")) if raw.get("all_day") is not None else None
    if all_day is not None:
        out["all_day"] = 1 if all_day else 0

    def _norm(v):
        return fin.normalize_iso_datetime(v, default_now=False)

    start = raw.get("start")
    end = raw.get("end")
    try:
        if start is not None:
            out["start_time"] = _norm(start)
        elif require_start:
            raise HTTPException(status_code=422, detail="A start time is required.")
        if end is not None and str(end).strip() != "":
            out["end_time"] = _norm(end)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # default end = start + 1h for a timed event that gave no end
    if "start_time" in out and "end_time" not in out and not (out.get("all_day")):
        st = datetime.datetime.fromisoformat(out["start_time"])
        out["end_time"] = (st + datetime.timedelta(hours=1)).isoformat()

    s_iso = out.get("start_time")
    e_iso = out.get("end_time")
    if s_iso and e_iso:
        if datetime.datetime.fromisoformat(e_iso) < datetime.datetime.fromisoformat(s_iso):
            raise HTTPException(status_code=422, detail="End time cannot be before the start time.")

    if "location" in raw:
        loc = (raw["location"] or "").strip()
        out["location"] = loc[:200] or None

    # Phase 5G: an optional real linked place. When present it is validated
    # provider-neutrally (never trust arbitrary frontend metadata) and its STABLE
    # identity + a small snapshot are persisted. `place: null` clears the link.
    if "place" in raw:
        if raw["place"] in (None, {}):
            for c in ("place_provider", "place_provider_id", "place_lat", "place_lng", "place_address"):
                out[c] = None
        else:
            try:
                pref = place_ref.normalize_place_ref(raw["place"])
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e))
            out.update(pref)                      # includes out["location"] = place name

    if "notes" in raw:
        notes = (raw["notes"] or "").strip()
        if len(notes) > 2000:
            raise HTTPException(status_code=422, detail="Notes are too long (max 2000 characters).")
        out["notes"] = notes or None
    if "status" in raw and raw["status"] is not None:
        st = str(raw["status"]).strip().lower()
        if st not in sched.EVENT_STATUSES:
            raise HTTPException(status_code=422, detail="Invalid event status.")
        out["status"] = st

    if "environment" in raw and raw["environment"] is not None:
        try:
            env = wxc.normalize_environment(raw["environment"])
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if env is not None:
            out["environment"] = env

    if "recurrence" in raw and raw["recurrence"] is not None:
        rec = str(raw["recurrence"]).strip().lower()
        if rec not in sched.RECURRENCE_KINDS:
            raise HTTPException(status_code=422, detail="Invalid recurrence.")
        out["recurrence"] = rec
    if "recurrence_until" in raw and raw["recurrence_until"] not in (None, ""):
        try:
            out["recurrence_until"] = _norm(raw["recurrence_until"])
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

    if "related_task_id" in raw:
        rtid = raw["related_task_id"]
        if rtid:
            if _get_owned_task(user_id, rtid) is None:
                raise HTTPException(status_code=404, detail="Linked task not found.")
            out["related_task_id"] = rtid
        else:
            out["related_task_id"] = None

    return out


def _validate_task_fields(*, require_title: bool, **raw) -> dict:
    out: dict = {}
    if "title" in raw and raw["title"] is not None:
        t = str(raw["title"]).strip()
        if not t:
            raise HTTPException(status_code=422, detail="Title cannot be empty.")
        if len(t) > 200:
            raise HTTPException(status_code=422, detail="Title is too long (max 200 characters).")
        out["title"] = t
    elif require_title:
        raise HTTPException(status_code=422, detail="Title is required.")
    if "notes" in raw:
        notes = (raw["notes"] or "").strip()
        if len(notes) > 2000:
            raise HTTPException(status_code=422, detail="Notes are too long (max 2000 characters).")
        out["notes"] = notes or None
    for key, col in (("due_date", "due_date"), ("planned_at", "planned_at"),
                     ("scheduled_end", "scheduled_end"), ("reminder_at", "reminder_at")):
        if key in raw:
            v = raw[key]
            if v in (None, ""):
                out[col] = None
            else:
                try:
                    out[col] = fin.normalize_iso_datetime(v, default_now=False)
                except ValueError as e:
                    raise HTTPException(status_code=422, detail=str(e))
    return out


def _task_window_check(planned_at, scheduled_end) -> None:
    """B7: a task work window is [planned_at, scheduled_end). When both are set,
    end must be strictly after start. scheduled_end alone (no planned_at) is not
    a window and is rejected so it can't sit as an orphan end."""
    if scheduled_end and not planned_at:
        raise HTTPException(
            status_code=422,
            detail="A scheduled end needs a planned start time.",
        )
    if planned_at and scheduled_end:
        try:
            s = datetime.datetime.fromisoformat(planned_at)
            e = datetime.datetime.fromisoformat(scheduled_end)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid scheduled time.")
        if e <= s:
            raise HTTPException(
                status_code=422,
                detail="The task's end time must be after its start time.",
            )


def _task_update_columns(existing: dict, supplied: dict) -> dict:
    """Validate + normalise a partial task edit into concrete column values,
    cross-checking the resulting [planned_at, scheduled_end) window against the
    stored row. Shared by PATCH /api/tasks and the B7 task-move path so a
    conflict-resolution move goes through the exact same validation as a manual
    edit. Raises HTTPException."""
    fields = _validate_task_fields(require_title=False, **supplied)
    p = fields.get("planned_at", existing.get("planned_at"))
    e = fields.get("scheduled_end", existing.get("scheduled_end"))
    _task_window_check(p, e)
    return fields


def _exec_task_update(conn, user_id: str, task_id: str, fields: dict) -> None:
    """Run the UPDATE for a validated partial task edit on the caller's
    connection. Keeps reminder_status consistent when reminder_at changes.
    Raises HTTPException(404) if nothing matched."""
    sets, args = [], []
    for col, val in fields.items():
        sets.append(f"{col} = ?")
        args.append(val)
    if "reminder_at" in fields:
        sets.append("reminder_status = ?")
        args.append("PENDING" if fields["reminder_at"] else "NONE")
    sets.append("updated_at = ?")
    args.append(get_iso_now())
    args += [task_id, user_id]
    cur = conn.execute(
        f"UPDATE tasks SET {', '.join(sets)} WHERE id = ? AND user_id = ?", tuple(args)
    )
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Task not found.")


# --------------------------------------------------------------------------- #
# B3: Plan conflict decisions — enrichment of the read path
# --------------------------------------------------------------------------- #
def _sc_now() -> str:
    return sched.myt_now().isoformat()


# --------------------------------------------------------------------------- #
# B6: per-occurrence conflict decisions for recurring events
# --------------------------------------------------------------------------- #
# How far around "now" recurring series are expanded when recomputing conflicts
# for a decision/keep/accept action (mirrors the month/week read window bound).
#
# Product Completion Batch 05 — BACK_DAYS was 1, and it is the ONLY thing that
# bounds _sc_match_conflict (used by both /keep and /accept). expand_event()
# excludes ANY occurrence — recurring or a plain one-off — whose time falls
# outside [now - BACK_DAYS, now + FWD_DAYS), so a genuinely still-unresolved,
# still-persisted conflict silently became un-actionable (a 409) the moment
# its calendar date crossed more than ~1 day into the past, even though the
# SAME day is still visibly shown with that conflict unresolved when a user
# navigates Plan to that day (that read path uses its own, unrelated window).
# There is no "conflicts expire" product rule anywhere else in this schema
# (schedule_conflict_decisions has no ttl/expiry column) — this was an
# incidental side effect of a bound whose docstring only ever described
# bounding RECURRING expansion cost, applied uniformly to one-off events too.
# A short grace period (one week) fixes that without making arbitrarily old
# historical conflicts freely resolvable — still clearly bounded, not
# unbounded. (Total span is separately capped at MAX_WINDOW_DAYS by
# sched.clamp_window, which trims the FORWARD side when exceeded — so this
# still leaves ~55 days of forward reach, not 62.)
_SC_RECUR_WINDOW_BACK_DAYS = 7
_SC_RECUR_WINDOW_FWD_DAYS = sched.MAX_WINDOW_DAYS


def _occ_overrides_by_series(user_id: str, conn=None) -> dict:
    """`{series_event_id -> {occurrence_date -> {"start","end","status"}}}` for the
    owner's schedule_occurrence_overrides rows (B6). The series row itself is
    never modified; these are RFC 5545 RECURRENCE-ID style exceptions."""
    own = conn is None
    c = conn or get_db()
    try:
        rows = c.execute(
            "SELECT series_event_id, occurrence_date, start_time, end_time, status "
            "FROM schedule_occurrence_overrides WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    finally:
        if own:
            c.close()
    out: dict = {}
    for r in rows:
        out.setdefault(r["series_event_id"], {})[r["occurrence_date"]] = {
            "start": r["start_time"], "end": r["end_time"],
            "status": r["status"] or "moved",
        }
    return out


def _sc_recur_window():
    """The [start_iso, end_iso) MYT window used to expand recurring series when
    recomputing conflicts server-side."""
    now = sched.myt_now()
    a = (now - datetime.timedelta(days=_SC_RECUR_WINDOW_BACK_DAYS))
    b = (now + datetime.timedelta(days=_SC_RECUR_WINDOW_FWD_DAYS))
    return a.isoformat(), b.isoformat()


def _sc_expand_all(conn, user_id: str) -> list[dict]:
    """Every concrete occurrence (one-off + recurring, overrides applied) of the
    owner's non-cancelled events across the recompute window."""
    rows = _sc_live_event_rows(conn, user_id)
    obs = _occ_overrides_by_series(user_id, conn)
    a_iso, b_iso = _sc_recur_window()
    win_start, win_end = sched.clamp_window(a_iso, b_iso)
    occ: list[dict] = []
    for r in rows:
        occ.extend(sched.expand_event(dict(r), win_start, win_end, obs.get(r["id"])))
    return occ


def _occ_key(o: dict) -> tuple:
    return (o.get("id"), o.get("occurrence_date"))


def _sc_other_intervals(
    event_rows: list[dict],
    exclude_ids: set[str],
    occurrences: Optional[list[dict]] = None,
    exclude_occ_keys: Optional[set] = None,
) -> list[tuple[str, str]]:
    """(start,end) of the owner's OTHER timed, non-all-day, non-cancelled event
    occurrences — the set a proposed reschedule must not newly collide with.

    When ``occurrences`` is supplied (B6) every OTHER concrete occurrence is
    considered — including sibling occurrences of a recurring series — so moving
    one occurrence can never be proposed on top of another. ``exclude_occ_keys``
    holds ``(series_id, occurrence_date)`` pairs to skip (the occurrence being
    moved). Without ``occurrences`` the pre-B6 behaviour (one-off events only) is
    kept for callers that have not been migrated."""
    out: list[tuple[str, str]] = []
    if occurrences is not None:
        xk = exclude_occ_keys or set()
        for o in occurrences:
            if o.get("all_day") or (o.get("status") or "confirmed") == "cancelled":
                continue
            if o.get("id") in exclude_ids or _occ_key(o) in xk:
                continue
            s, e = o.get("start"), o.get("end")
            if s and e:
                out.append((s, e))
        return out
    for r in event_rows:
        if r.get("id") in exclude_ids:
            continue
        if r.get("all_day") or (r.get("status") or "confirmed") == "cancelled":
            continue
        if (r.get("recurrence") or "none") != "none":
            continue  # a recurring series is out of scope for the collision pre-check
        s = r.get("start_time")
        if not s:
            continue
        e = r.get("end_time") or (
            datetime.datetime.fromisoformat(s) + datetime.timedelta(hours=1)
        ).isoformat()
        out.append((s, e))
    return out


def _sc_occ_for_date(conn, user_id: str, row: dict, occ_date: str) -> Optional[dict]:
    """Expand series ``row`` (overrides applied) and return the concrete
    occurrence whose canonical date == ``occ_date``, or None if that date no
    longer recurs (rule change / recurrence_until shortened / series edited)."""
    if (row.get("recurrence") or "none") == "none":
        return None
    try:
        d = datetime.date.fromisoformat(occ_date)
    except (TypeError, ValueError):
        return None
    a = datetime.datetime(d.year, d.month, d.day, tzinfo=sched.MYT) - datetime.timedelta(days=1)
    b = a + datetime.timedelta(days=3)
    ov = _occ_overrides_by_series(user_id, conn).get(row["id"])
    for o in sched.expand_event(dict(row), a, b, ov):
        if o.get("occurrence_date") == occ_date:
            return o
    return None


def _sc_stamp_stale_decisions(user_id: str) -> None:
    """Lazily invalidate decisions whose underlying events no longer match the
    fingerprint that was decided (a relevant time / all-day / location edit, a
    deletion, or — B6 — a recurrence-rule change or an occurrence that no longer
    exists). A stale decision stops suppressing anything, so the recomputed (now
    different) overlap re-surfaces truthfully."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, fingerprint, event_a_id, event_b_id, occurrence_date "
            "FROM schedule_conflict_decisions WHERE user_id = ? AND stale_at IS NULL",
            (user_id,),
        ).fetchall()
        if not rows:
            return
        stale_ids: list[str] = []
        for d in rows:
            a = conn.execute(
                f"SELECT {_EVENT_COLS} FROM schedule_events WHERE id = ? AND user_id = ?",
                (d["event_a_id"], user_id),
            ).fetchone()
            b = conn.execute(
                f"SELECT {_EVENT_COLS} FROM schedule_events WHERE id = ? AND user_id = ?",
                (d["event_b_id"], user_id),
            ).fetchone()
            if a is None or b is None:
                stale_ids.append(d["id"])
                continue
            occ_date = d["occurrence_date"] if "occurrence_date" in d.keys() else None
            if occ_date:
                a_row, b_row = dict(a), dict(b)
                side_a = (_sc_occ_for_date(conn, user_id, a_row, occ_date)
                          if (a_row.get("recurrence") or "none") != "none" else a_row)
                side_b = (_sc_occ_for_date(conn, user_id, b_row, occ_date)
                          if (b_row.get("recurrence") or "none") != "none" else b_row)
                if side_a is None or side_b is None:
                    stale_ids.append(d["id"])
                    continue
                if sconf.pair_fingerprint(side_a, side_b) != d["fingerprint"]:
                    stale_ids.append(d["id"])
                continue
            if sconf.pair_fingerprint(dict(a), dict(b)) != d["fingerprint"]:
                stale_ids.append(d["id"])
        if stale_ids:
            now = _sc_now()
            conn.executemany(
                "UPDATE schedule_conflict_decisions SET stale_at = ? "
                "WHERE id = ? AND user_id = ? AND stale_at IS NULL",
                [(now, sid, user_id) for sid in stale_ids],
            )
            conn.commit()
    finally:
        conn.close()


def _sc_enrich_conflicts(
    user_id: str, conflicts: list[dict], event_rows: list[dict],
    occurrences: Optional[list[dict]] = None,
) -> list[dict]:
    """Annotate each detected overlap with a deterministic fingerprint, a concise
    explanation, a server-generated reschedule proposal when one is safe, and the
    owner's recorded decision (kept / accepted) if any.

    B6: a recurring-instance overlap is now decidable PER OCCURRENCE. Its
    fingerprint is bound to that one occurrence of that one series version; its
    proposal (if any) moves only that occurrence (written later as an occurrence
    override, never a series edit). ``conflict_type`` is ``recurring_overlap``
    and ``scope`` is ``occurrence``."""
    if not conflicts:
        return conflicts
    by_id = {r["id"]: r for r in event_rows}
    conn = get_db()
    try:
        drows = conn.execute(
            "SELECT fingerprint, decision, applied_json FROM schedule_conflict_decisions "
            "WHERE user_id = ? AND stale_at IS NULL",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    decisions = {d["fingerprint"]: d for d in drows}
    all_occ = occurrences or []

    enriched: list[dict] = []
    for c in conflicts:
        a_occ, b_occ = c.get("a") or {}, c.get("b") or {}
        recurring = bool(a_occ.get("is_recurring_instance") or b_occ.get("is_recurring_instance"))
        item = dict(c)
        item["explanation"] = sconf.explain(a_occ, b_occ)

        fp = sconf.pair_fingerprint(a_occ, b_occ)
        dec = decisions.get(fp)
        # the collision set: every OTHER occurrence in view (siblings included),
        # minus the two occurrences in this conflict.
        other = _sc_other_intervals(
            event_rows, {a_occ.get("id"), b_occ.get("id")},
            occurrences=all_occ if all_occ else None,
            exclude_occ_keys={_occ_key(a_occ), _occ_key(b_occ)},
        )
        proposal = sconf.propose_shift(a_occ, b_occ, other)
        occ_date = (a_occ.get("occurrence_date") if a_occ.get("is_recurring_instance")
                    else (b_occ.get("occurrence_date") if b_occ.get("is_recurring_instance")
                          else None))
        item.update({
            "conflict_type": (sconf.CONFLICT_TYPE_RECURRING_OVERLAP if recurring
                              else sconf.CONFLICT_TYPE_EVENT_OVERLAP),
            "scope": "occurrence" if recurring else "series",
            "occurrence_date": occ_date,
            "occurrence_label": sconf.occurrence_date_label(occ_date),
            "decidable": True,
            "fingerprint": fp,
            "proposal": proposal,
            "decision": (dec["decision"] if dec else None),
        })
        enriched.append(item)
    return enriched


def _load_schedule(user_id: str, win_from_iso: str, win_to_iso: str) -> dict:
    """Authoritative read: events (recurrence-expanded, occurrence overrides
    applied), tasks, deterministic conflicts, and pending schedule
    recommendations for the window."""
    _sc_stamp_stale_decisions(user_id)
    conn = get_db()
    try:
        event_rows = conn.execute(
            f"SELECT {_EVENT_COLS} FROM schedule_events WHERE user_id = ? AND status != 'cancelled'",
            (user_id,),
        ).fetchall()
        all_event_rows = conn.execute(
            f"SELECT {_EVENT_COLS} FROM schedule_events WHERE user_id = ?", (user_id,),
        ).fetchall()
        task_rows = conn.execute(
            f"SELECT {_TASK_COLS} FROM tasks WHERE user_id = ?", (user_id,),
        ).fetchall()
        recs = conn.execute(
            "SELECT id, module, type, message, status, target_id, suggested_action, "
            "snooze_until, created_at, updated_at FROM ai_recommendations "
            "WHERE user_id = ? AND module = 'SCHEDULE' AND status = 'PENDING' "
            "AND (snooze_until IS NULL OR snooze_until <= ?)",
            (user_id, sched.myt_now().isoformat()),
        ).fetchall()
        overrides = _occ_overrides_by_series(user_id, conn)
    finally:
        conn.close()

    event_dicts = [dict(r) for r in event_rows]
    occurrences, conflicts = sched.build_agenda(
        event_dicts, win_from_iso, win_to_iso, overrides_by_series=overrides
    )
    conflicts = _sc_enrich_conflicts(user_id, conflicts, event_dicts, occurrences)
    task_conf = _te_conflicts_payload(user_id)   # B7: task-window <-> event-occurrence overlaps
    return {
        "range": {"from": win_from_iso, "to": win_to_iso},
        "events": occurrences,
        "all_events": [_event_out(r) for r in all_event_rows],
        "tasks": [_task_out(r) for r in task_rows],
        "conflicts": conflicts,
        "unresolved_conflicts": [c for c in conflicts if not c.get("decision")],
        "task_conflicts": task_conf["conflicts"],
        "unresolved_task_conflicts": task_conf["unresolved"],
        "recommendations": [dict(r) for r in recs],
        "timezone": "Asia/Kuala_Lumpur",
    }


@app.get("/api/schedule")
def get_schedule(
    current: dict = Depends(get_current_user),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    """Window semantics (Asia/Kuala_Lumpur, half-open ``[from, to)``):

      * no params -> the current MYT month.
      * ``date_from`` + ``date_to`` (both required together): a **date-only**
        ``YYYY-MM-DD`` value is a calendar date in MYT — ``date_from`` is the
        inclusive start at ``00:00:00+08:00`` and ``date_to`` is an inclusive
        end date whose backend range ends at the NEXT day's ``00:00:00+08:00``.
        So ``date_from=2026-09-07&date_to=2026-09-13`` queries
        ``[2026-09-07T00:00:00+08:00, 2026-09-14T00:00:00+08:00)`` — a full
        seven calendar days. Full datetime values are used as given.
    """
    if date_from and date_to:
        try:
            f_iso, t_iso = sched.resolve_window_params(date_from, date_to)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    elif date_from or date_to:
        raise HTTPException(
            status_code=422, detail="date_from and date_to must be provided together."
        )
    else:
        now = sched.myt_now()
        f_iso, t_iso = sched.myt_month_bounds(now.year, now.month)
    return _load_schedule(current["id"], f_iso, t_iso)


@app.get("/api/schedule/day")
def get_schedule_day(date: str, current: dict = Depends(get_current_user)):
    try:
        f_iso, t_iso = sched.myt_day_bounds(date)
    except ValueError:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD.")
    return _load_schedule(current["id"], f_iso, t_iso)


@app.get("/api/schedule/month")
def get_schedule_month(year: int, month: int, current: dict = Depends(get_current_user)):
    if not (1 <= month <= 12) or not (2000 <= year <= 2100):
        raise HTTPException(status_code=422, detail="Invalid year/month.")
    f_iso, t_iso = sched.myt_month_bounds(year, month)
    data = _load_schedule(current["id"], f_iso, t_iso)
    conn = get_db()
    try:
        task_rows = conn.execute(
            "SELECT planned_at, due_date, reminder_at FROM tasks WHERE user_id = ?",
            (current["id"],),
        ).fetchall()
    finally:
        conn.close()
    return {
        "year": year, "month": month,
        "range": data["range"],
        "day_counts": sched.day_counts(data["events"], [dict(r) for r in task_rows]),
        "conflicts": data["conflicts"],
    }


# --- events CRUD ---
def _event_create_kwargs(req: EventCreate) -> dict:
    kw = dict(
        title=req.title, start=req.start, end=req.end, all_day=req.all_day,
        location=req.location, notes=req.notes, recurrence=req.recurrence,
        recurrence_until=req.recurrence_until, related_task_id=req.related_task_id,
    )
    if req.place is not None:
        kw["place"] = req.place
    if req.environment is not None:
        kw["environment"] = req.environment
    return kw


# The normalized event fields that define a create's identity for the
# create-once fingerprint (Phase 5H-B3). No server-generated timestamp appears
# here, so a retry produces the same hash.
_EVENT_FP_KEYS = (
    "title", "start_time", "end_time", "all_day", "location", "notes",
    "recurrence", "recurrence_until", "related_task_id",
    "place_provider", "place_provider_id", "place_lat", "place_lng", "place_address",
)
# NOTE: `environment` is deliberately NOT in the create-idempotency fingerprint
# (it is not part of an event's create identity and would break replay for a
# pre-B5 tokened row whose column now defaults to 'uncertain'). Its
# decision-critical role is enforced by the B5 weather-hazard fingerprint.


@app.post("/api/events", status_code=status.HTTP_201_CREATED)
def create_event(req: EventCreate, response: Response, current: dict = Depends(get_current_user)):
    user_id = current["id"]

    # Phase 5G/5H-B3 idempotency: a retried create (double-click, lost response,
    # network retry, Strict Mode) with the same (user_id, client_token) returns
    # the FIRST event, never a duplicate. A token reused for a MATERIALLY
    # DIFFERENT payload is a truthful 409. Enforced here and by a DB unique index.
    try:
        token = idem.normalize_token(req.client_token)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    fields = _validate_event_fields(user_id, require_start=True, **_event_create_kwargs(req))
    fp = idem.request_fingerprint({k: fields.get(k) for k in _EVENT_FP_KEYS})

    def _replay(prior):
        prior_id, prior_hash = prior
        prior_row = _get_owned_event(user_id, prior_id)
        effective_hash = prior_hash
        if effective_hash is None:
            # A pre-5H-B3 tokened row (the Phase 5G Map/Activity -> Schedule
            # bridge is the only pre-B3 caller that sent a client_token). Its
            # fingerprint is deterministically reconstructable from the persisted
            # create semantics, so the collision contract still applies to it.
            effective_hash = idem.request_fingerprint(
                {k: prior_row.get(k) for k in _EVENT_FP_KEYS})
        if effective_hash != fp:
            raise idem.IdempotencyConflict()
        response.status_code = status.HTTP_200_OK
        return {"status": "SUCCESS", "idempotent": True, "event": _event_out(prior_row)}

    if token:
        prior = _prior_create("schedule_events", user_id, token)
        if prior is not None:
            return _replay(prior)

    ev_id = str(uuid.uuid4())
    now = get_iso_now()
    try:
        with db_layer.transaction() as conn:
            conn.execute(
                """INSERT INTO schedule_events
                   (id, user_id, title, location, start_time, end_time, notes, all_day,
                    status, source_type, external_id, recurrence, recurrence_until,
                    related_task_id, created_at, updated_at,
                    place_provider, place_provider_id, place_lat, place_lng, place_address,
                    environment, client_token, client_request_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', 'coreos', NULL, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ev_id, user_id, fields["title"], fields.get("location"),
                 fields["start_time"], fields.get("end_time"), fields.get("notes"),
                 fields.get("all_day", 0), fields.get("recurrence", "none"),
                 fields.get("recurrence_until"), fields.get("related_task_id"), now, now,
                 fields.get("place_provider"), fields.get("place_provider_id"),
                 fields.get("place_lat"), fields.get("place_lng"), fields.get("place_address"),
                 fields.get("environment") or "uncertain",
                 token, fp if token else None),
            )
    except sqlite3.IntegrityError:
        # a concurrent request with the same token won the race — replay its event
        prior = _prior_create("schedule_events", user_id, token) if token else None
        if prior is not None:
            return _replay(prior)
        raise HTTPException(status_code=500, detail="Couldn't save that event. Please try again.")
    return {"status": "SUCCESS", "event": _event_out(_get_owned_event(user_id, ev_id))}


def _event_update_columns(user_id: str, existing: dict, supplied: dict) -> dict:
    """Validate + normalize a partial event edit into concrete column values.
    Shared by the manual PATCH endpoint and the B3 Accept path so a
    conflict-resolution reschedule goes through the exact same validation and
    linked-place-consistency rules as a manual time edit. Raises HTTPException."""
    fields = _validate_event_fields(user_id, require_start=False, **supplied)

    # A move that changes only the start time (no explicit new end supplied)
    # must preserve the event's OWN existing duration — not the create-time
    # "no end given -> default to start+1h" rule inside _validate_event_fields
    # (that default only makes sense for a brand-new event with no prior
    # duration to keep). Batch 05: this is what stops "move X to 7pm" from
    # silently shrinking/stretching a non-1h event to exactly 1h. Shared by
    # the manual PATCH endpoint and the Mo UPDATE_EVENT path alike.
    existing_all_day = fields["all_day"] if "all_day" in fields else bool(existing.get("all_day"))
    if (
        "start_time" in fields
        and "end" not in supplied
        and not existing_all_day
        and existing.get("start_time")
        and existing.get("end_time")
    ):
        try:
            orig_start = datetime.datetime.fromisoformat(existing["start_time"])
            orig_end = datetime.datetime.fromisoformat(existing["end_time"])
            new_start = datetime.datetime.fromisoformat(fields["start_time"])
        except ValueError:
            orig_start = orig_end = new_start = None
        if orig_start and orig_end and new_start and orig_end > orig_start:
            fields["end_time"] = (new_start + (orig_end - orig_start)).isoformat()

    # Cross-check a partial time edit against the stored value.
    s_iso = fields.get("start_time", existing["start_time"])
    e_iso = fields.get("end_time", existing["end_time"])
    if s_iso and e_iso and datetime.datetime.fromisoformat(e_iso) < datetime.datetime.fromisoformat(s_iso):
        raise HTTPException(status_code=422, detail="End time cannot be before the start time.")

    # Phase 5G linked-place consistency: the generic event form edits the
    # free-text `location` and never carries a structured `place`. If such an edit
    # changes the location text away from the currently linked place's name, the
    # event would otherwise DISPLAY one destination while its structured identity
    # (provider id / coordinates) still points at another. Clear the structured
    # link in this SAME update so the event becomes an ordinary free-text event.
    # Nothing about the new text is treated as a verified place.
    had_place = bool(existing.get("place_provider")) and bool(existing.get("place_provider_id"))
    if (
        had_place
        and "place" not in supplied
        and "location" in fields
        and (fields.get("location") or "") != (existing.get("location") or "")
    ):
        for _c in ("place_provider", "place_provider_id", "place_lat", "place_lng", "place_address"):
            fields[_c] = None
    return fields


def _exec_event_update(conn, user_id: str, event_id: str, fields: dict) -> None:
    """Run the UPDATE for a validated partial event edit on the given connection
    (the caller owns the transaction). Raises HTTPException(404) if nothing matched."""
    sets, args = [], []
    for col, val in fields.items():
        sets.append(f"{col} = ?")
        args.append(val)
    sets.append("updated_at = ?")
    args.append(get_iso_now())
    args += [event_id, user_id]
    cur = conn.execute(
        f"UPDATE schedule_events SET {', '.join(sets)} WHERE id = ? AND user_id = ?",
        tuple(args),
    )
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Event not found.")


@app.patch("/api/events/{event_id}")
def update_event(event_id: str, req: EventUpdate, current: dict = Depends(get_current_user)):
    user_id = current["id"]
    existing = _get_owned_event(user_id, event_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Event not found.")

    supplied = req.model_dump(exclude_unset=True)
    if not supplied:
        raise HTTPException(status_code=422, detail="No fields to update.")
    fields = _event_update_columns(user_id, existing, supplied)
    if not fields:
        raise HTTPException(status_code=422, detail="No fields to update.")
    # B6: a change to the series' timing / recurrence rule / all-day flag moves
    # the anchor that occurrence exceptions were computed against — drop them so
    # the series reverts to its clean default occurrences and any per-occurrence
    # decision stales lazily. A pure title/notes/location edit keeps exceptions.
    _drop_overrides = (
        (existing.get("recurrence") or "none") != "none"
        and any(k in fields for k in
                ("start_time", "end_time", "recurrence", "recurrence_until", "all_day"))
    )
    try:
        with db_layer.transaction() as conn:
            _exec_event_update(conn, user_id, event_id, fields)
            if _drop_overrides:
                conn.execute(
                    "DELETE FROM schedule_occurrence_overrides "
                    "WHERE user_id = ? AND series_event_id = ?",
                    (user_id, event_id),
                )
    except HTTPException:
        raise
    except Exception as e:
        print(f"DB_ERROR (update_event): {str(e)}")
        raise HTTPException(status_code=500, detail="Couldn't update that event. Please try again.")
    return {"status": "SUCCESS", "event": _event_out(_get_owned_event(user_id, event_id))}


@app.delete("/api/events/{event_id}")
def delete_event(event_id: str, current: dict = Depends(get_current_user)):
    user_id = current["id"]
    with db_layer.transaction() as conn:
        cur = conn.execute(
            "DELETE FROM schedule_events WHERE id = ? AND user_id = ?", (event_id, user_id)
        )
        deleted = cur.rowcount
        # B6: drop any per-occurrence exceptions for a deleted series (their
        # anchor is gone). Occurrence decisions stale lazily on the next read.
        conn.execute(
            "DELETE FROM schedule_occurrence_overrides WHERE user_id = ? AND series_event_id = ?",
            (user_id, event_id),
        )
    if not deleted:
        raise HTTPException(status_code=404, detail="Event not found.")
    return {"status": "SUCCESS"}


# --------------------------------------------------------------------------- #
# B3: Plan conflict Accept / Keep
# --------------------------------------------------------------------------- #
class _ScStale(Exception):
    """The live schedule no longer matches the conflict the client is acting on."""


_SC_STALE_MSG = "This overlap has changed. Reload your plan and try again."


def _sc_live_event_rows(conn, user_id: str) -> list[dict]:
    return [
        dict(r) for r in conn.execute(
            f"SELECT {_EVENT_COLS} FROM schedule_events "
            f"WHERE user_id = ? AND status != 'cancelled'",
            (user_id,),
        ).fetchall()
    ]


def _sc_match_conflict(conn, user_id: str, fingerprint: str):
    """Recompute the owner's event↔event overlaps from expanded occurrences
    (one-off + recurring, occurrence overrides applied) over the bounded recompute
    window, and return ``(a_occ, b_occ, proposal)`` for the pair whose fingerprint
    matches, else ``None``. A fingerprint that belongs to another user's events
    never matches here. For a recurring occurrence the proposal carries
    ``is_recurring_instance`` + ``occurrence_date`` so the caller writes an
    occurrence override rather than a series edit."""
    occ = _sc_expand_all(conn, user_id)
    for c in sched.find_conflicts(occ):
        a, b = c["a"], c["b"]
        if sconf.pair_fingerprint(a, b) != fingerprint:
            continue
        other = _sc_other_intervals(
            [], {a.get("id"), b.get("id")}, occurrences=occ,
            exclude_occ_keys={_occ_key(a), _occ_key(b)},
        )
        proposal = sconf.propose_shift(a, b, other)
        return a, b, proposal
    return None


def _sc_occ_date_for(a_occ: dict, b_occ: dict) -> Optional[str]:
    if a_occ.get("is_recurring_instance"):
        return a_occ.get("occurrence_date")
    if b_occ.get("is_recurring_instance"):
        return b_occ.get("occurrence_date")
    return None


def _sc_decision_row(user_id: str, fingerprint: str) -> Optional[dict]:
    conn = get_db()
    try:
        r = conn.execute(
            "SELECT * FROM schedule_conflict_decisions WHERE user_id = ? AND fingerprint = ?",
            (user_id, fingerprint),
        ).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _sc_replay(user_id: str, dec: dict) -> dict:
    """The already-established outcome for a decision — returned on any safe
    replay (repeated click, network retry, refresh) so retry never applies a
    different schedule mutation."""
    occ_date = dec["occurrence_date"] if "occurrence_date" in dec.keys() else None
    out = {
        "status": dec["decision"],
        "decision_id": dec["id"],
        "decision": dec["decision"],
        "conflict_type": dec["conflict_type"],
        "scope": "occurrence" if occ_date else "series",
        "occurrence_date": occ_date,
        "already": True,
    }
    if dec["decision"] == "accepted":
        try:
            out["resolution"] = json.loads(dec["applied_json"]) if dec.get("applied_json") else None
        except (TypeError, ValueError):
            out["resolution"] = None
        ev = _get_owned_event(user_id, dec.get("applied_event_id") or "")
        out["event"] = _event_out(ev) if ev else None
    return out


def _sc_decision_common(fingerprint: str, current: dict):
    fp = (fingerprint or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", fp):
        # not a possible fingerprint — same shape as "conflict not found"
        raise HTTPException(status_code=409, detail=_SC_STALE_MSG)
    return fp, current["id"]


@app.post("/api/schedule/conflicts/{fingerprint}/keep")
def keep_schedule_conflict(fingerprint: str, current: dict = Depends(_rl("schedule_decision"))):
    """Record that the owner chooses to keep both items exactly as they are. The
    unchanged overlap stops appearing as unresolved; nothing about either event
    is edited or deleted. Idempotent."""
    fp, user_id = _sc_decision_common(fingerprint, current)

    prior = _sc_decision_row(user_id, fp)
    if prior is not None:
        return _sc_replay(user_id, prior)

    try:
        with db_layer.transaction() as conn:
            match = _sc_match_conflict(conn, user_id, fp)
            if match is None:
                raise _ScStale()
            a_row, b_row, proposal = match
            recurring = bool(a_row.get("is_recurring_instance") or b_row.get("is_recurring_instance"))
            ctype = "recurring_overlap" if recurring else "event_overlap"
            occ_date = _sc_occ_date_for(a_row, b_row)
            did = "scd_" + uuid.uuid4().hex
            now = _sc_now()
            original = {
                "a": sconf._se(a_row) | {"title": a_row.get("title")},
                "b": sconf._se(b_row) | {"title": b_row.get("title")},
            }
            conn.execute(
                """INSERT INTO schedule_conflict_decisions
                   (id, user_id, fingerprint, conflict_type, event_a_id, event_b_id,
                    original_json, proposal_json, decision, applied_event_id, applied_json,
                    exec_token, created_at, decided_at, stale_at, occurrence_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'kept', NULL, NULL, ?, ?, ?, NULL, ?)""",
                (did, user_id, fp, ctype, a_row["id"], b_row["id"],
                 json.dumps(original, sort_keys=True, default=str),
                 json.dumps(proposal, sort_keys=True, default=str) if proposal else None,
                 "scd:" + did, now, now, occ_date),
            )
    except _ScStale:
        raise HTTPException(status_code=409, detail=_SC_STALE_MSG)
    except sqlite3.IntegrityError:
        again = _sc_decision_row(user_id, fp)
        if again is not None:
            return _sc_replay(user_id, again)
        raise HTTPException(status_code=500, detail="Couldn't save that choice. Please try again.")

    return {"status": "kept", "decision_id": did, "decision": "kept",
            "conflict_type": ctype, "scope": "occurrence" if recurring else "series",
            "occurrence_date": occ_date, "already": False}


@app.post("/api/schedule/conflicts/{fingerprint}/accept")
def accept_schedule_conflict(fingerprint: str, current: dict = Depends(_rl("schedule_decision"))):
    """Apply the exact server-generated reschedule for a detected overlap through
    the SAME canonical event-update path a manual Plan edit uses, and record the
    decision — atomically, exactly once. A stale proposal (the schedule item
    changed after the conflict was shown) is rejected without mutating anything."""
    fp, user_id = _sc_decision_common(fingerprint, current)

    prior = _sc_decision_row(user_id, fp)
    if prior is not None:
        return _sc_replay(user_id, prior)

    # Pre-check outside the write txn: is there still a matching conflict with a
    # safe resolution? (Re-verified inside the txn below against a fresh read.)
    pre_conn = get_db()
    try:
        pre = _sc_match_conflict(pre_conn, user_id, fp)
    finally:
        pre_conn.close()
    if pre is None:
        raise HTTPException(status_code=409, detail=_SC_STALE_MSG)
    if pre[2] is None:
        raise HTTPException(
            status_code=422,
            detail="No automatic reschedule is safe for this overlap. You can keep the current plan.",
        )

    did = "scd_" + uuid.uuid4().hex
    try:
        with db_layer.transaction() as conn:
            match = _sc_match_conflict(conn, user_id, fp)   # fresh read, race-safe
            if match is None:
                raise _ScStale()
            a_row, b_row, proposal = match
            if proposal is None or not proposal.get("event_id"):
                raise _ScStale()

            move_id = proposal["event_id"]
            recurring = bool(proposal.get("is_recurring_instance"))
            ctype = "recurring_overlap" if recurring else "event_overlap"
            occ_date = proposal.get("occurrence_date") if recurring else None
            applied = {"event_id": move_id, "start": proposal["start"],
                       "end": proposal["end"], "occurrence_date": occ_date,
                       "scope": "occurrence" if recurring else "series"}

            if recurring:
                # Move exactly ONE occurrence — an RFC 5545 style exception row.
                # The series row, its recurrence rule, and every other occurrence
                # are untouched. UNIQUE(user_id, series_event_id, occurrence_date)
                # is the exactly-once gate.
                if not occ_date:
                    raise _ScStale()
                norm = _validate_event_fields(
                    user_id, require_start=False,
                    start=proposal["start"], end=proposal["end"],
                )
                ns, ne = norm.get("start_time"), norm.get("end_time")
                if not ns or not ne or datetime.datetime.fromisoformat(ne) <= datetime.datetime.fromisoformat(ns):
                    raise _ScStale()
                nowi = get_iso_now()
                conn.execute(
                    """INSERT INTO schedule_occurrence_overrides
                       (id, user_id, series_event_id, occurrence_date, start_time, end_time,
                        status, origin, exec_token, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'moved', 'overlap', ?, ?, ?)""",
                    ("soo_" + uuid.uuid4().hex, user_id, move_id, occ_date, ns, ne,
                     "scd:" + did, nowi, nowi),
                )
            else:
                existing = _get_owned_event(user_id, move_id)
                if existing is None:
                    raise _ScStale()
                fields = _event_update_columns(
                    user_id, existing,
                    {"start": proposal["start"], "end": proposal["end"]},
                )
                _exec_event_update(conn, user_id, move_id, fields)

            original = {
                "a": sconf._se(a_row) | {"title": a_row.get("title")},
                "b": sconf._se(b_row) | {"title": b_row.get("title")},
            }
            now = _sc_now()
            conn.execute(
                """INSERT INTO schedule_conflict_decisions
                   (id, user_id, fingerprint, conflict_type, event_a_id, event_b_id,
                    original_json, proposal_json, decision, applied_event_id, applied_json,
                    exec_token, created_at, decided_at, stale_at, occurrence_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?, ?, ?, ?, NULL, ?)""",
                (did, user_id, fp, ctype, a_row["id"], b_row["id"],
                 json.dumps(original, sort_keys=True, default=str),
                 json.dumps(proposal, sort_keys=True, default=str),
                 move_id, json.dumps(applied, sort_keys=True, default=str),
                 "scd:" + did, now, now, occ_date),
            )
    except _ScStale:
        raise HTTPException(status_code=409, detail=_SC_STALE_MSG)
    except HTTPException:
        raise
    except sqlite3.IntegrityError:
        again = _sc_decision_row(user_id, fp)
        if again is not None:
            return _sc_replay(user_id, again)
        raise HTTPException(status_code=500, detail="Couldn't apply that change. Please try again.")
    except Exception as e:
        print(f"SC_ACCEPT_ERROR: {type(e).__name__}")
        raise HTTPException(status_code=500, detail="Couldn't apply that change. Please try again.")

    ev = _get_owned_event(user_id, move_id)
    return {
        "status": "accepted", "decision_id": did, "decision": "accepted",
        "conflict_type": ctype, "scope": applied["scope"],
        "occurrence_date": occ_date, "already": False,
        "resolution": applied,
        "event": _event_out(ev) if ev else None,
    }


# --------------------------------------------------------------------------- #
# B5: Contextual weather-aware schedule conflicts
# --------------------------------------------------------------------------- #
_WX_STALE_MSG = "This weather warning is out of date. Reload your plan and try again."
_WX_MOVE_PROPOSAL_TTL_S = 30 * 60
_WX_MAX_EVENTS_EVALUATED = 6
_WX_NO_ALT_MSG = "No suitable alternative is available from the current forecast."


class WeatherMoveConfirm(BaseModel):
    candidate_index: int


def _wx_now():
    return weather_core.now_utc()


def _wx_event_coords(row: dict):
    """The event's linked-PLACE coordinates (never the device location), or None
    when there is no validated place location to reason about."""
    lat, lng = row.get("place_lat"), row.get("place_lng")
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
        return None
    if lat == 0.0 and lng == 0.0:
        return None
    return lat, lng


def _wx_event_utc_window(row: dict):
    """(start_utc, end_utc) for a timed, non-all-day event, or None. All-day
    events are deliberately NOT weather-evaluated (a whole-day forecast is too
    coarse and would produce repetitive messaging)."""
    if row.get("all_day"):
        return None
    s, e = row.get("start_time"), row.get("end_time")
    if not s or not e:
        return None
    try:
        return weather_core.validate_window(s, e)
    except weather_core.WeatherWindowError:
        return None


def _wx_eligible(row: dict, now_utc):
    """All eligibility rules must hold. Returns (start_utc, end_utc, lat, lng) or None.
    Ineligible -> silently no weather conflict for this event (never a 'safe' claim).

    Non-recurring only — for recurring series use ``_wx_eligible_occurrences``."""
    if (row.get("recurrence") or "none") != "none":
        return None
    if (row.get("environment") or "uncertain") != "outdoor":
        return None
    win = _wx_event_utc_window(row)
    if win is None:
        return None
    start_utc, end_utc = win
    horizon_end = now_utc + datetime.timedelta(hours=weather_core.FORECAST_HORIZON_HOURS)
    if end_utc <= now_utc or start_utc >= horizon_end:
        return None  # entirely in the past, or beyond forecast availability
    coords = _wx_event_coords(row)
    if coords is None:
        return None
    return start_utc, end_utc, coords[0], coords[1]


def _wx_eligible_occurrences(row: dict, now_utc, ov_for_series: Optional[dict] = None):
    """Every concrete OUTDOOR occurrence of ``row`` inside the forecast horizon,
    each ``(start_utc, end_utc, lat, lng, occurrence_date | None)``.

    B6: a recurring outdoor series is evaluated occurrence-by-occurrence (one per
    matching day in ``[now, now + 48h)``, occurrence overrides applied). A
    one-off event still yields 0 or 1. All B5 gates (explicit ``outdoor``,
    validated place coordinates, timed / not all-day, inside the horizon) apply
    per occurrence."""
    if (row.get("environment") or "uncertain") != "outdoor" or row.get("all_day"):
        return []
    coords = _wx_event_coords(row)
    if coords is None:
        return []
    lat, lng = coords
    horizon_end = now_utc + datetime.timedelta(hours=weather_core.FORECAST_HORIZON_HOURS)
    kind = (row.get("recurrence") or "none").lower()

    if kind == "none" or kind not in sched.RECURRENCE_KINDS:
        win = _wx_event_utc_window(row)
        if win is None:
            return []
        s, e = win
        if e <= now_utc or s >= horizon_end:
            return []
        return [(s, e, lat, lng, None)]

    win_start = now_utc.astimezone(sched.MYT) - datetime.timedelta(days=1)
    win_end = horizon_end.astimezone(sched.MYT) + datetime.timedelta(days=1)
    out = []
    for occ in sched.expand_event(dict(row), win_start, win_end, ov_for_series):
        if occ.get("all_day"):
            continue
        try:
            s, e = weather_core.validate_window(occ["start"], occ["end"])
        except weather_core.WeatherWindowError:
            continue
        if e <= now_utc or s >= horizon_end:
            continue
        out.append((s, e, lat, lng, occ.get("occurrence_date")))
    return out


def _wx_busy_intervals(conn, user_id: str, exclude_id: str,
                       exclude_occ_date: Optional[str] = None, now_utc=None):
    """Timed, non-cancelled occurrences the owner already has — the set a weather
    reschedule candidate must not collide with. B6: recurring series are expanded
    across the forecast window (overrides applied); the target occurrence
    ``(exclude_id, exclude_occ_date)`` is skipped, and for a non-recurring target
    the whole series ``exclude_id`` is skipped."""
    now_utc = now_utc or _wx_now()
    win_start = now_utc.astimezone(sched.MYT) - datetime.timedelta(days=1)
    win_end = (now_utc + datetime.timedelta(hours=weather_core.FORECAST_HORIZON_HOURS)
               ).astimezone(sched.MYT) + datetime.timedelta(days=2)
    obs = _occ_overrides_by_series(user_id, conn)
    rows = conn.execute(
        f"SELECT {_EVENT_COLS} FROM schedule_events "
        f"WHERE user_id = ? AND status != 'cancelled'",
        (user_id,),
    ).fetchall()
    out = []
    for r in rows:
        r = dict(r)
        if r.get("all_day"):
            continue
        for occ in sched.expand_event(r, win_start, win_end, obs.get(r["id"])):
            if occ.get("all_day"):
                continue
            if occ["id"] == exclude_id and (
                exclude_occ_date is None or occ.get("occurrence_date") == exclude_occ_date
            ):
                continue
            s, e = occ.get("start"), occ.get("end")
            if s and e:
                out.append((s, e))
    return out


def _wx_window_forecast(forecast: dict, start_utc, end_utc, now_utc):
    """Aggregate the provider forecast over the real event interval, or None when
    the window is outside the horizon / has no covering rows / the forecast is
    stale (fail safe)."""
    if weather_core.is_stale(forecast.get("fetched_at")):
        return None
    horizon_end = now_utc + datetime.timedelta(hours=weather_core.FORECAST_HORIZON_HOURS)
    if end_utc <= now_utc or start_utc >= horizon_end:
        return None
    sel = weather_core.select_window_hours(forecast.get("hourly") or [], start_utc, end_utc)
    agg = weather_core.aggregate_window(sel, start_utc, end_utc)
    if agg is None:
        return None
    prim = weather_core.derive_primitives(agg)
    return {**agg, "primitives": prim}


async def _wx_forecast_for(lat: float, lng: float):
    """(forecast, None) or (None, error_kind). Coordinates never logged."""
    provider = weather_provider.get_weather_provider()
    try:
        fc = await weather_provider.get_forecast(provider, lat, lng)
        return fc, None
    except weather_core.WeatherProviderError as e:
        print(f"WX_CONFLICT: provider={provider.name} error={e.kind}")
        return None, e.kind


def _wx_occ_token(row: dict, occ_date: Optional[str]) -> Optional[str]:
    """The value fed to the weather-hazard fingerprint's ``occ`` slot. For a
    recurring occurrence it binds the identity to BOTH the occurrence date and
    the current series version (recurrence rule + end bound) so a rule change
    stales any prior weather decision / proposal for that occurrence. ``None``
    for a one-off event -> the fingerprint is byte-identical to its pre-B6 value."""
    if not occ_date:
        return None
    return f"{occ_date}|{(row.get('recurrence') or 'none')}|{row.get('recurrence_until') or ''}"


def _wx_hazard_fp(row: dict, start_utc, end_utc, lat: float, lng: float, hazard: dict,
                  occ_date: Optional[str] = None) -> str:
    return wxc.hazard_fingerprint(
        event_id=row["id"],
        start_iso=start_utc.isoformat(), end_iso=end_utc.isoformat(),
        environment=(row.get("environment") or "uncertain"),
        latitude=lat, longitude=lng,
        hazard_type=hazard["hazard_type"], severity_tier=hazard["severity_tier"],
        occurrence_date=_wx_occ_token(row, occ_date),
    )


def _wx_local(dt_utc) -> str:
    return dt_utc.astimezone(sched.MYT).isoformat()


def _wx_headline_with_time(hazard: dict, start_utc) -> str:
    a = start_utc.astimezone(sched.MYT)
    h = a.hour % 12 or 12
    ap = "am" if a.hour < 12 else "pm"
    clock = f"{h}:{a.minute:02d} {ap}"
    base = hazard["headline"].rstrip(".")
    return f"{base}, around {clock}."


def _wx_decision_row(user_id: str, fp: str):
    conn = get_db()
    try:
        r = conn.execute(
            "SELECT * FROM weather_decisions WHERE user_id = ? AND fingerprint = ?",
            (user_id, fp),
        ).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _wx_stamp_stale(user_id: str) -> None:
    """Invalidate weather decisions whose event was deleted or whose
    decision-critical data (time / environment / place location) changed, so the
    (now different) hazard is recomputed truthfully."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, event_id, fingerprint, hazard_type, severity_tier, occurrence_date "
            "FROM weather_decisions WHERE user_id = ? AND stale_at IS NULL",
            (user_id,),
        ).fetchall()
        if not rows:
            return
        overrides = _occ_overrides_by_series(user_id, conn)
        stale_ids = []
        for d in rows:
            ev = conn.execute(
                f"SELECT {_EVENT_COLS} FROM schedule_events WHERE id = ? AND user_id = ?",
                (d["event_id"], user_id),
            ).fetchone()
            if ev is None:
                stale_ids.append(d["id"]); continue
            ev = dict(ev)
            coords = _wx_event_coords(ev)
            if coords is None:
                stale_ids.append(d["id"]); continue
            occ_date = d["occurrence_date"] if "occurrence_date" in d.keys() else None
            if occ_date:
                # recurring occurrence: re-expand just that occurrence
                occ = _sc_occ_for_date(conn, user_id, ev, occ_date)
                if occ is None:
                    stale_ids.append(d["id"]); continue
                try:
                    s_utc, e_utc = weather_core.validate_window(occ["start"], occ["end"])
                except weather_core.WeatherWindowError:
                    stale_ids.append(d["id"]); continue
                start_iso, end_iso = s_utc.isoformat(), e_utc.isoformat()
            else:
                win = _wx_event_utc_window(ev)
                if win is None:
                    stale_ids.append(d["id"]); continue
                start_iso, end_iso = win[0].isoformat(), win[1].isoformat()
            try:
                fp_now = wxc.hazard_fingerprint(
                    event_id=ev["id"], start_iso=start_iso, end_iso=end_iso,
                    environment=(ev.get("environment") or "uncertain"),
                    latitude=coords[0], longitude=coords[1],
                    hazard_type=d["hazard_type"], severity_tier=d["severity_tier"],
                    occurrence_date=_wx_occ_token(ev, occ_date),
                )
            except Exception:
                stale_ids.append(d["id"]); continue
            if fp_now != d["fingerprint"]:
                stale_ids.append(d["id"])
        if stale_ids:
            now = get_iso_now()
            conn.executemany(
                "UPDATE weather_decisions SET stale_at = ? WHERE id = ? AND user_id = ? AND stale_at IS NULL",
                [(now, sid, user_id) for sid in stale_ids],
            )
            conn.commit()
    finally:
        conn.close()


def _wx_public_conflict(row: dict, start_utc, end_utc, hazard: dict, fp: str,
                        forecast: dict, decision, occ_date: Optional[str] = None) -> dict:
    return {
        "event_id": row["id"],
        "fingerprint": fp,
        "hazard_type": hazard["hazard_type"],
        "severity_tier": hazard["severity_tier"],
        "headline": _wx_headline_with_time(hazard, start_utc),
        "scope": "occurrence" if occ_date else "series",
        "occurrence_date": occ_date,
        "occurrence_label": sconf.occurrence_date_label(occ_date),
        "event": {
            "id": row["id"], "title": row.get("title"),
            "start": _wx_local(start_utc), "end": _wx_local(end_utc),
        },
        "forecast": {
            "provider": forecast.get("provider"),
            "fetched_at": forecast.get("fetched_at"),
            "stale": weather_core.is_stale(forecast.get("fetched_at")),
        },
        "decision": (decision["decision"] if decision else None),
    }


async def _wx_match_hazard(conn_user_id: str, fingerprint: str):
    """Recompute the owner's eligible outdoor occurrences (one-off + recurring,
    overrides applied) and return
    (event_row, start_utc, end_utc, lat, lng, hazard, forecast, occ_date) for the
    one whose current fingerprint matches, else None. A fingerprint that belongs
    to another user's event never matches here."""
    user_id = conn_user_id
    now = _wx_now()
    conn = get_db()
    try:
        rows = [dict(r) for r in conn.execute(
            f"SELECT {_EVENT_COLS} FROM schedule_events "
            f"WHERE user_id = ? AND status != 'cancelled'",
            (user_id,),
        ).fetchall()]
        overrides = _occ_overrides_by_series(user_id, conn)
    finally:
        conn.close()
    for row in rows:
        for start_utc, end_utc, lat, lng, occ_date in _wx_eligible_occurrences(
            row, now, overrides.get(row["id"])
        ):
            forecast, err = await _wx_forecast_for(lat, lng)
            if forecast is None:
                continue
            window = _wx_window_forecast(forecast, start_utc, end_utc, now)
            hazard = wxc.material_hazard(window) if window else None
            if hazard is None:
                continue
            if _wx_hazard_fp(row, start_utc, end_utc, lat, lng, hazard, occ_date) == fingerprint:
                return row, start_utc, end_utc, lat, lng, hazard, forecast, occ_date
    return None


def _wx_replay(user_id: str, dec: dict) -> dict:
    occ_date = dec["occurrence_date"] if "occurrence_date" in dec.keys() else None
    out = {
        "status": dec["decision"], "decision_id": dec["id"], "decision": dec["decision"],
        "scope": "occurrence" if occ_date else "series", "occurrence_date": occ_date,
        "already": True,
    }
    if dec["decision"] == "moved":
        try:
            out["resolution"] = json.loads(dec["applied_json"]) if dec.get("applied_json") else None
            out["from"] = json.loads(dec["move_from_json"]) if dec.get("move_from_json") else None
        except (TypeError, ValueError):
            pass
        ev = _get_owned_event(user_id, dec.get("event_id") or "")
        out["event"] = _event_out(ev) if ev else None
    return out


def _wx_fp_common(fingerprint: str, current: dict):
    fp = (fingerprint or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", fp):
        raise HTTPException(status_code=409, detail=_WX_STALE_MSG)
    return fp, current["id"]


@app.get("/api/schedule/weather-conflicts")
async def get_weather_conflicts(current: dict = Depends(_rl("weather"))):
    """Material weather hazards over the caller's eligible OUTDOOR events inside
    the provider forecast horizon. Ineligible events are silently skipped; a
    provider failure yields `provider_unavailable: true` and NO fabricated
    conflicts. Nothing is persisted here."""
    user_id = current["id"]
    _wx_stamp_stale(user_id)
    now = _wx_now()

    conn = get_db()
    try:
        rows = [dict(r) for r in conn.execute(
            f"SELECT {_EVENT_COLS} FROM schedule_events "
            f"WHERE user_id = ? AND status != 'cancelled' "
            f"ORDER BY start_time ASC",
            (user_id,),
        ).fetchall()]
        drows = conn.execute(
            "SELECT fingerprint, decision FROM weather_decisions "
            "WHERE user_id = ? AND stale_at IS NULL",
            (user_id,),
        ).fetchall()
        overrides = _occ_overrides_by_series(user_id, conn)
    finally:
        conn.close()
    decisions = {d["fingerprint"]: dict(d) for d in drows}

    # (row, start_utc, end_utc, lat, lng, occ_date) per eligible occurrence —
    # a recurring outdoor series contributes one entry per matching day in the
    # forecast horizon; a one-off contributes 0 or 1.
    eligible = []
    for row in rows:
        for occ in _wx_eligible_occurrences(row, now, overrides.get(row["id"])):
            eligible.append((row, *occ))
            if len(eligible) >= _WX_MAX_EVENTS_EVALUATED:
                break
        if len(eligible) >= _WX_MAX_EVENTS_EVALUATED:
            break

    conflicts = []
    provider_unavailable = False
    for row, start_utc, end_utc, lat, lng, occ_date in eligible:
        forecast, err = await _wx_forecast_for(lat, lng)
        if forecast is None:
            provider_unavailable = True
            continue
        window = _wx_window_forecast(forecast, start_utc, end_utc, now)
        hazard = wxc.material_hazard(window) if window else None
        if hazard is None:
            continue
        fp = _wx_hazard_fp(row, start_utc, end_utc, lat, lng, hazard, occ_date)
        conflicts.append(_wx_public_conflict(row, start_utc, end_utc, hazard, fp, forecast,
                                             decisions.get(fp), occ_date))

    return {
        "conflicts": conflicts,
        "unresolved": [c for c in conflicts if not c["decision"]],
        "provider_unavailable": provider_unavailable,
        "attribution": weather_provider.OPEN_METEO_ATTRIBUTION,
    }


@app.post("/api/schedule/weather-conflicts/{fingerprint}/keep")
async def keep_weather_conflict(fingerprint: str, current: dict = Depends(_rl("schedule_decision"))):
    """Record that the owner keeps the plan. Nothing about the event changes.
    The same unchanged hazard (same severity tier) stops appearing; a strictly
    higher tier later produces a new warning. Idempotent."""
    fp, user_id = _wx_fp_common(fingerprint, current)

    prior = _wx_decision_row(user_id, fp)
    if prior is not None:
        return _wx_replay(user_id, prior)

    match = await _wx_match_hazard(user_id, fp)
    if match is None:
        raise HTTPException(status_code=409, detail=_WX_STALE_MSG)
    row, start_utc, end_utc, lat, lng, hazard, forecast, occ_date = match

    did = "wxd_" + uuid.uuid4().hex
    now = get_iso_now()
    evidence = {
        "hazard_type": hazard["hazard_type"], "severity_tier": hazard["severity_tier"],
        "values": hazard.get("values") or {},
        "window": {"start": start_utc.isoformat(), "end": end_utc.isoformat()},
        "forecast_fetched_at": forecast.get("fetched_at"),
    }
    try:
        with db_layer.transaction() as conn:
            conn.execute(
                """INSERT INTO weather_decisions
                   (id, user_id, event_id, fingerprint, hazard_type, severity_tier, environment,
                    event_start, event_end, forecast_provider, forecast_fetched_at, evidence_json,
                    decision, applied_json, move_from_json, exec_token, created_at, decided_at,
                    stale_at, occurrence_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'kept', NULL, NULL, ?, ?, ?, NULL, ?)""",
                (did, user_id, row["id"], fp, hazard["hazard_type"], hazard["severity_tier"],
                 row.get("environment") or "uncertain",
                 _wx_local(start_utc), _wx_local(end_utc),
                 forecast.get("provider"), forecast.get("fetched_at"),
                 json.dumps(evidence, sort_keys=True, default=str),
                 "wxd:" + did, now, now, occ_date),
            )
    except sqlite3.IntegrityError:
        again = _wx_decision_row(user_id, fp)
        if again is not None:
            return _wx_replay(user_id, again)
        raise HTTPException(status_code=500, detail="Couldn't save that choice. Please try again.")

    return {"status": "kept", "decision_id": did, "decision": "kept", "already": False,
            "scope": "occurrence" if occ_date else "series", "occurrence_date": occ_date}


@app.post("/api/schedule/weather-conflicts/{fingerprint}/alternatives")
async def weather_conflict_alternatives(fingerprint: str, current: dict = Depends(_rl("weather"))):
    """Server-generated candidate reschedule slots for a material weather
    conflict. Every candidate avoids the hazard (same policy), preserves the
    event's duration, does not overlap another timed event, and stays inside the
    forecast horizon. `Find another time` NEVER modifies the schedule."""
    fp, user_id = _wx_fp_common(fingerprint, current)

    match = await _wx_match_hazard(user_id, fp)
    if match is None:
        raise HTTPException(status_code=409, detail=_WX_STALE_MSG)
    row, start_utc, end_utc, lat, lng, hazard, forecast, occ_date = match

    duration = end_utc - start_utc
    now = _wx_now()
    horizon_end = now + datetime.timedelta(hours=weather_core.FORECAST_HORIZON_HOURS)
    conn = get_db()
    try:
        busy = _wx_busy_intervals(conn, user_id, row["id"], occ_date, now)
    finally:
        conn.close()

    candidates = wxc.propose_slots(
        duration=duration,
        hourly_rows=forecast.get("hourly") or [],
        busy_intervals=busy,
        now_utc=now,
        horizon_end_utc=horizon_end,
        max_slots=3,
    )
    ev_pub = {"id": row["id"], "title": row.get("title"),
              "start": _wx_local(start_utc), "end": _wx_local(end_utc)}
    haz_pub = {"hazard_type": hazard["hazard_type"], "severity_tier": hazard["severity_tier"],
               "headline": _wx_headline_with_time(hazard, start_utc)}
    scope = "occurrence" if occ_date else "series"

    if not candidates:
        return {"proposal_id": None, "event": ev_pub, "hazard": haz_pub,
                "candidates": [], "message": _WX_NO_ALT_MSG,
                "scope": scope, "occurrence_date": occ_date}

    pid = "wxp_" + uuid.uuid4().hex
    created = get_iso_now()
    expires = (now + datetime.timedelta(seconds=_WX_MOVE_PROPOSAL_TTL_S)).isoformat()
    try:
        with db_layer.transaction() as conn:
            conn.execute(
                """INSERT INTO weather_move_proposals
                   (id, user_id, event_id, fingerprint, hazard_type, severity_tier, status,
                    candidates_json, chosen_index, event_start, event_end, forecast_fetched_at,
                    exec_token, created_at, expires_at, confirmed_at, occurrence_date)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, NULL, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                (pid, user_id, row["id"], fp, hazard["hazard_type"], hazard["severity_tier"],
                 json.dumps(candidates, sort_keys=True, default=str),
                 _wx_local(start_utc), _wx_local(end_utc), forecast.get("fetched_at"),
                 "wxp:" + pid, created, expires, occ_date),
            )
    except Exception as e:
        print(f"WX_ALT_ERROR: {type(e).__name__}")
        raise HTTPException(status_code=500, detail="Couldn't prepare alternatives. Please try again.")

    return {"proposal_id": pid, "event": ev_pub, "hazard": haz_pub,
            "candidates": candidates, "message": None,
            "scope": scope, "occurrence_date": occ_date}


def _wx_get_owned_proposal(pid: str, user_id: str):
    conn = get_db()
    try:
        r = conn.execute(
            "SELECT * FROM weather_move_proposals WHERE id = ? AND user_id = ?",
            (pid, user_id),
        ).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


@app.post("/api/schedule/weather-conflicts/alternatives/{proposal_id}/cancel")
def cancel_weather_alternatives(proposal_id: str, current: dict = Depends(_rl("schedule_decision"))):
    user_id = current["id"]
    p = _wx_get_owned_proposal(proposal_id, user_id)
    if p is None:
        raise HTTPException(status_code=404, detail="That proposal isn’t available.")
    if p["status"] == "confirmed":
        raise HTTPException(status_code=409, detail="This move was already applied.")
    if p["status"] in ("cancelled", "expired"):
        return {"status": "cancelled", "already": True}
    with db_layer.transaction() as conn:
        conn.execute(
            "UPDATE weather_move_proposals SET status = 'cancelled' "
            "WHERE id = ? AND user_id = ? AND status = 'pending'",
            (proposal_id, user_id),
        )
    return {"status": "cancelled", "already": False}


@app.post("/api/schedule/weather-conflicts/alternatives/{proposal_id}/confirm")
async def confirm_weather_move(proposal_id: str, req: WeatherMoveConfirm,
                               current: dict = Depends(_rl("schedule_decision"))):
    """Apply the chosen server candidate through the SAME canonical event-update
    path a manual Plan edit uses, and record the decision — atomically, exactly
    once. Re-reads the event and re-verifies the candidate against a fresh
    forecast + a fresh schedule read; a stale proposal is rejected without
    mutating anything, and a newer manual edit is preserved."""
    user_id = current["id"]
    p = _wx_get_owned_proposal(proposal_id, user_id)
    if p is None:
        raise HTTPException(status_code=404, detail="That proposal isn’t available.")

    fp = p["fingerprint"]

    # replay an already-applied move
    if p["status"] == "confirmed":
        dec = _wx_decision_row(user_id, fp)
        if dec is not None:
            return _wx_replay(user_id, dec)
        raise HTTPException(status_code=409, detail=_WX_STALE_MSG)
    if p["status"] == "cancelled":
        raise HTTPException(status_code=409, detail="This proposal was cancelled.")
    now = _wx_now()
    if p["status"] == "expired" or weather_core.parse_aware_iso(p["expires_at"]) <= now:
        with db_layer.transaction() as conn:
            conn.execute("UPDATE weather_move_proposals SET status = 'expired' "
                         "WHERE id = ? AND user_id = ? AND status = 'pending'",
                         (proposal_id, user_id))
        raise HTTPException(status_code=409, detail="This proposal expired. Search again.")

    try:
        candidates = json.loads(p["candidates_json"]) or []
    except (TypeError, ValueError):
        candidates = []
    idx = req.candidate_index
    if not isinstance(idx, int) or idx < 0 or idx >= len(candidates):
        raise HTTPException(status_code=422, detail="Choose one of the offered times.")
    chosen = candidates[idx]

    # a prior kept/moved decision for this hazard? -> replay (exactly once)
    prior = _wx_decision_row(user_id, fp)
    if prior is not None:
        return _wx_replay(user_id, prior)

    # re-verify the hazard is still the current one for this event / occurrence
    match = await _wx_match_hazard(user_id, fp)
    if match is None:
        raise HTTPException(status_code=409, detail=_WX_STALE_MSG)
    row, start_utc, end_utc, lat, lng, hazard, forecast, occ_date = match

    try:
        cand_start_utc = weather_core.parse_aware_iso(chosen["start"])
        cand_end_utc = weather_core.parse_aware_iso(chosen["end"])
    except (weather_core.WeatherWindowError, KeyError, TypeError):
        raise HTTPException(status_code=409, detail=_WX_STALE_MSG)

    duration = end_utc - start_utc
    horizon_end = now + datetime.timedelta(hours=weather_core.FORECAST_HORIZON_HOURS)
    # candidate must still be valid: preserves duration, not past, within horizon
    if (cand_end_utc - cand_start_utc) != duration or cand_start_utc <= now \
            or cand_end_utc > horizon_end:
        raise HTTPException(status_code=409, detail=_WX_STALE_MSG)
    # ...still hazard-free under the same policy on a fresh forecast...
    cand_window = _wx_window_forecast(forecast, cand_start_utc, cand_end_utc, now)
    if cand_window is None or wxc.material_hazard(cand_window) is not None:
        raise HTTPException(status_code=409, detail=_WX_STALE_MSG)

    did = "wxd_" + uuid.uuid4().hex
    recurring = bool(occ_date)
    applied = {"event_id": row["id"], "start": chosen["start"], "end": chosen["end"],
               "occurrence_date": occ_date, "scope": "occurrence" if recurring else "series"}
    move_from = {"start": _wx_local(start_utc), "end": _wx_local(end_utc)}
    evidence = {
        "hazard_type": hazard["hazard_type"], "severity_tier": hazard["severity_tier"],
        "values": hazard.get("values") or {},
        "window": {"start": start_utc.isoformat(), "end": end_utc.isoformat()},
        "forecast_fetched_at": forecast.get("fetched_at"),
        "moved_to": {"start": chosen["start"], "end": chosen["end"]},
    }
    try:
        with db_layer.transaction() as conn:
            # ...still no collision on a FRESH schedule read (sibling occurrences
            # of the same series included; the target occurrence excluded)...
            for bs, be in _wx_busy_intervals(conn, user_id, row["id"], occ_date, now):
                if sched.intervals_overlap(chosen["start"], chosen["end"], bs, be):
                    raise _ScStale()
            existing = _get_owned_event(user_id, row["id"])
            if existing is None:
                raise _ScStale()
            nowi = get_iso_now()
            if recurring:
                # Move exactly ONE occurrence — an occurrence override. The series
                # row, its recurrence rule, and every other occurrence are
                # untouched. UNIQUE(user_id, series_event_id, occurrence_date) is
                # the exactly-once gate.
                norm = _validate_event_fields(
                    user_id, require_start=False,
                    start=chosen["start"], end=chosen["end"],
                )
                ns, ne = norm.get("start_time"), norm.get("end_time")
                if not ns or not ne:
                    raise _ScStale()
                conn.execute(
                    """INSERT INTO schedule_occurrence_overrides
                       (id, user_id, series_event_id, occurrence_date, start_time, end_time,
                        status, origin, exec_token, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'moved', 'weather', ?, ?, ?)""",
                    ("soo_" + uuid.uuid4().hex, user_id, row["id"], occ_date, ns, ne,
                     "wxd:" + did, nowi, nowi),
                )
            else:
                fields = _event_update_columns(user_id, existing,
                                               {"start": chosen["start"], "end": chosen["end"]})
                _exec_event_update(conn, user_id, row["id"], fields)
            conn.execute(
                """INSERT INTO weather_decisions
                   (id, user_id, event_id, fingerprint, hazard_type, severity_tier, environment,
                    event_start, event_end, forecast_provider, forecast_fetched_at, evidence_json,
                    decision, applied_json, move_from_json, exec_token, created_at, decided_at,
                    stale_at, occurrence_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'moved', ?, ?, ?, ?, ?, NULL, ?)""",
                (did, user_id, row["id"], fp, hazard["hazard_type"], hazard["severity_tier"],
                 row.get("environment") or "uncertain",
                 move_from["start"], move_from["end"],
                 forecast.get("provider"), forecast.get("fetched_at"),
                 json.dumps(evidence, sort_keys=True, default=str),
                 json.dumps(applied, sort_keys=True, default=str),
                 json.dumps(move_from, sort_keys=True, default=str),
                 "wxd:" + did, nowi, nowi, occ_date),
            )
            conn.execute(
                "UPDATE weather_move_proposals SET status = 'confirmed', chosen_index = ?, "
                "confirmed_at = ? WHERE id = ? AND user_id = ? AND status = 'pending'",
                (idx, nowi, proposal_id, user_id),
            )
    except _ScStale:
        raise HTTPException(status_code=409, detail=_WX_STALE_MSG)
    except HTTPException:
        raise
    except sqlite3.IntegrityError:
        again = _wx_decision_row(user_id, fp)
        if again is not None:
            return _wx_replay(user_id, again)
        raise HTTPException(status_code=500, detail="Couldn't apply that move. Please try again.")
    except Exception as e:
        print(f"WX_MOVE_ERROR: {type(e).__name__}")
        raise HTTPException(status_code=500, detail="Couldn't apply that move. Please try again.")

    ev = _get_owned_event(user_id, row["id"])
    return {
        "status": "moved", "decision_id": did, "decision": "moved", "already": False,
        "from": move_from, "resolution": applied,
        "scope": "occurrence" if recurring else "series", "occurrence_date": occ_date,
        "event": _event_out(ev) if ev else None,
    }


# --------------------------------------------------------------------------- #
# B7: Task <-> event schedule conflicts
# --------------------------------------------------------------------------- #
# A task participates ONLY when it has a real work window: planned_at (start) AND
# scheduled_end, end > start. A due_date / reminder_at alone never conflicts.
_TE_STALE_MSG = "This conflict has changed. Reload your plan and try again."
_TE_MOVE_TTL_S = 30 * 60
_TE_NO_ALT_MSG = "No suitable free time is available in the next two weeks."
_TE_NO_ALT_BEFORE_DUE_MSG = "No suitable free time is available before this task’s deadline."
_TE_HORIZON_DAYS = 14          # how far ahead candidate task windows may land
_TE_RECOMPUTE_BACK_DAYS = 1
_TE_RECOMPUTE_FWD_DAYS = sched.MAX_WINDOW_DAYS
_TE_MAX_TASKS = 12


class TaskMoveConfirm(BaseModel):
    candidate_index: int


def _te_now():
    return sched.myt_now()


def _te_scheduled_tasks(conn, user_id: str):
    """(task_row, start_dt, end_dt) for the owner's non-completed tasks that have
    a real [planned_at, scheduled_end) work window, window end still on/after
    yesterday."""
    rows = conn.execute(
        f"SELECT {_TASK_COLS} FROM tasks WHERE user_id = ? "
        f"AND (is_completed IS NULL OR is_completed = 0)",
        (user_id,),
    ).fetchall()
    cutoff = _te_now() - datetime.timedelta(days=_TE_RECOMPUTE_BACK_DAYS)
    out = []
    for r in rows:
        r = dict(r)
        win = tec.task_window(r)
        if win is None:
            continue
        if win[1] <= cutoff:
            continue
        out.append((r, win[0], win[1]))
    return out


def _te_event_occurrences(conn, user_id: str, exclude_task_related: bool = False):
    """Every concrete event occurrence (one-off + recurring, B6 overrides
    applied) across the recompute window."""
    rows = [dict(r) for r in conn.execute(
        f"SELECT {_EVENT_COLS} FROM schedule_events "
        f"WHERE user_id = ? AND status != 'cancelled'",
        (user_id,),
    ).fetchall()]
    obs = _occ_overrides_by_series(user_id, conn)
    now = _te_now()
    a = (now - datetime.timedelta(days=_TE_RECOMPUTE_BACK_DAYS)).isoformat()
    b = (now + datetime.timedelta(days=_TE_RECOMPUTE_FWD_DAYS)).isoformat()
    win_start, win_end = sched.clamp_window(a, b)
    occ = []
    for r in rows:
        occ.extend(sched.expand_event(r, win_start, win_end, obs.get(r["id"])))
    return [o for o in occ if not o.get("all_day") and o.get("start") and o.get("end")]


def _te_fp(task_row: dict, task_win, occ: dict) -> str:
    return tec.conflict_fingerprint(
        task_id=task_row["id"],
        task_start_iso=task_win[0].isoformat(), task_end_iso=task_win[1].isoformat(),
        event_id=occ["id"], event_start_iso=occ["start"], event_end_iso=occ["end"],
        occurrence_date=(occ.get("occurrence_date") if occ.get("is_recurring_instance") else None),
    )


def _te_busy_intervals(conn, user_id: str, exclude_task_id: str):
    """The set a candidate task window must avoid: every timed event occurrence
    (recurring occurrences included) + every OTHER scheduled task window."""
    out = [(o["start"], o["end"]) for o in _te_event_occurrences(conn, user_id)]
    for tr, s, e in _te_scheduled_tasks(conn, user_id):
        if tr["id"] == exclude_task_id:
            continue
        out.append((s.isoformat(), e.isoformat()))
    return out


def _te_due_end(task_row: dict):
    """The task's deadline as an aware datetime, or None. Candidates must end on
    or before this; it is NEVER modified."""
    raw = task_row.get("due_date")
    if not raw:
        return None
    try:
        return datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None


def _te_candidates(conn, user_id: str, task_row: dict, task_win, busy=None):
    now = _te_now()
    horizon = now + datetime.timedelta(days=_TE_HORIZON_DAYS)
    if busy is None:
        busy = _te_busy_intervals(conn, user_id, task_row["id"])
    else:
        # drop this task's own window from a shared busy list
        own = (task_win[0].isoformat(), task_win[1].isoformat())
        busy = [b for b in busy if b != own]
    return tec.propose_task_slots(
        duration=task_win[1] - task_win[0],
        busy_intervals=busy,
        now_utc=now, horizon_end_utc=horizon,
        original_start=task_win[0],
        due_end=_te_due_end(task_row),
        max_slots=3,
    )


def _te_detect(conn, user_id: str):
    """List of {task_row, task_win, occ} — one per real task-window / event-
    occurrence overlap."""
    tasks = _te_scheduled_tasks(conn, user_id)[:_TE_MAX_TASKS]
    if not tasks:
        return []
    occs = _te_event_occurrences(conn, user_id)
    found = []
    for task_row, ts, te in tasks:
        for o in occs:
            if sched.intervals_overlap(ts.isoformat(), te.isoformat(), o["start"], o["end"]):
                found.append({"task_row": task_row, "task_win": (ts, te), "occ": o})
    return found


def _te_public(task_row: dict, task_win, occ: dict, fp: str, proposal, decision) -> dict:
    recurring = bool(occ.get("is_recurring_instance"))
    occ_date = occ.get("occurrence_date") if recurring else None
    return {
        "fingerprint": fp,
        "conflict_type": tec.CONFLICT_TYPE,
        "scope": "occurrence" if recurring else "series",
        "occurrence_date": occ_date,
        "occurrence_label": tec.occurrence_date_label(occ_date),
        "explanation": tec.explain(
            task_row.get("title"), occ.get("title"), task_win,
            (sched._parse(occ["start"]), sched._parse(occ["end"])),
            tec.occurrence_date_label(occ_date),
        ),
        "task": {
            "id": task_row["id"], "title": task_row.get("title"),
            "start": task_win[0].isoformat(), "end": task_win[1].isoformat(),
            "due_date": task_row.get("due_date"),
        },
        "event": {
            "id": occ["id"], "title": occ.get("title"),
            "start": occ["start"], "end": occ["end"], "recurring": recurring,
        },
        "proposal": proposal,
        "decision": (decision["decision"] if decision else None),
    }


def _te_stamp_stale(user_id: str) -> None:
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, fingerprint, task_id, event_id, occurrence_date "
            "FROM task_event_conflict_decisions WHERE user_id = ? AND stale_at IS NULL",
            (user_id,),
        ).fetchall()
        if not rows:
            return
        live = {}
        for d in _te_detect(conn, user_id):
            fp = _te_fp(d["task_row"], d["task_win"], d["occ"])
            live[fp] = True
        stale_ids = [d["id"] for d in rows if d["fingerprint"] not in live]
        if stale_ids:
            now = get_iso_now()
            conn.executemany(
                "UPDATE task_event_conflict_decisions SET stale_at = ? "
                "WHERE id = ? AND user_id = ? AND stale_at IS NULL",
                [(now, sid, user_id) for sid in stale_ids],
            )
            conn.commit()
    finally:
        conn.close()


def _te_conflicts_payload(user_id: str) -> dict:
    _te_stamp_stale(user_id)
    conn = get_db()
    try:
        drows = conn.execute(
            "SELECT fingerprint, decision FROM task_event_conflict_decisions "
            "WHERE user_id = ? AND stale_at IS NULL",
            (user_id,),
        ).fetchall()
        decisions = {d["fingerprint"]: dict(d) for d in drows}
        detected = _te_detect(conn, user_id)
        shared_busy = _te_busy_intervals(conn, user_id, "") if detected else []
        out = []
        for d in detected:
            fp = _te_fp(d["task_row"], d["task_win"], d["occ"])
            cands = _te_candidates(conn, user_id, d["task_row"], d["task_win"], busy=shared_busy)
            proposal = None
            if cands:
                c0 = cands[0]
                proposal = {
                    "task_id": d["task_row"]["id"],
                    "current_start": d["task_win"][0].isoformat(),
                    "current_end": d["task_win"][1].isoformat(),
                    "start": c0["start"], "end": c0["end"], "summary": c0["summary"],
                }
            out.append(_te_public(d["task_row"], d["task_win"], d["occ"], fp,
                                  proposal, decisions.get(fp)))
    finally:
        conn.close()
    return {"conflicts": out, "unresolved": [c for c in out if not c["decision"]]}


def _te_match(conn, user_id: str, fingerprint: str):
    for d in _te_detect(conn, user_id):
        if _te_fp(d["task_row"], d["task_win"], d["occ"]) == fingerprint:
            return d["task_row"], d["task_win"], d["occ"]
    return None


def _te_decision_row(user_id: str, fp: str):
    conn = get_db()
    try:
        r = conn.execute(
            "SELECT * FROM task_event_conflict_decisions WHERE user_id = ? AND fingerprint = ?",
            (user_id, fp),
        ).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _te_replay(user_id: str, dec: dict) -> dict:
    occ_date = dec["occurrence_date"] if "occurrence_date" in dec.keys() else None
    out = {
        "status": dec["decision"], "decision_id": dec["id"], "decision": dec["decision"],
        "conflict_type": tec.CONFLICT_TYPE,
        "scope": "occurrence" if occ_date else "series", "occurrence_date": occ_date,
        "already": True,
    }
    if dec["decision"] == "moved":
        try:
            out["resolution"] = json.loads(dec["applied_json"]) if dec.get("applied_json") else None
        except (TypeError, ValueError):
            out["resolution"] = None
        t = _get_owned_task(user_id, dec.get("task_id") or "")
        out["task"] = _task_out(t) if t else None
    return out


def _te_fp_common(fingerprint: str, current: dict):
    fp = (fingerprint or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", fp):
        raise HTTPException(status_code=409, detail=_TE_STALE_MSG)
    return fp, current["id"]


def _te_get_owned_proposal(pid: str, user_id: str):
    conn = get_db()
    try:
        r = conn.execute(
            "SELECT * FROM task_move_proposals WHERE id = ? AND user_id = ?",
            (pid, user_id),
        ).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


@app.get("/api/schedule/task-conflicts")
def get_task_conflicts(current: dict = Depends(_rl("schedule_decision"))):
    """Overlaps between a scheduled task work window and a timed event occurrence
    (recurring occurrences included). Tasks without a real [planned_at,
    scheduled_end) window are silently ignored; a due date or reminder is never a
    time-block conflict. Nothing is persisted here."""
    return _te_conflicts_payload(current["id"])


@app.post("/api/schedule/task-conflicts/{fingerprint}/keep")
def keep_task_conflict(fingerprint: str, current: dict = Depends(_rl("schedule_decision"))):
    """Record that the owner keeps both the task window and the event as they
    are. Nothing is edited. The exact unchanged overlap stops appearing.
    Idempotent."""
    fp, user_id = _te_fp_common(fingerprint, current)
    prior = _te_decision_row(user_id, fp)
    if prior is not None:
        return _te_replay(user_id, prior)

    try:
        with db_layer.transaction() as conn:
            m = _te_match(conn, user_id, fp)
            if m is None:
                raise _ScStale()
            task_row, task_win, occ = m
            recurring = bool(occ.get("is_recurring_instance"))
            occ_date = occ.get("occurrence_date") if recurring else None
            did = "ted_" + uuid.uuid4().hex
            now = get_iso_now()
            original = {
                "task": {"start": task_win[0].isoformat(), "end": task_win[1].isoformat()},
                "event": {"id": occ["id"], "start": occ["start"], "end": occ["end"]},
            }
            conn.execute(
                """INSERT INTO task_event_conflict_decisions
                   (id, user_id, fingerprint, task_id, event_id, occurrence_date, decision,
                    original_json, proposal_json, applied_json, exec_token,
                    created_at, decided_at, stale_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'kept', ?, NULL, NULL, ?, ?, ?, NULL)""",
                (did, user_id, fp, task_row["id"], occ["id"], occ_date,
                 json.dumps(original, sort_keys=True, default=str),
                 "ted:" + did, now, now),
            )
    except _ScStale:
        raise HTTPException(status_code=409, detail=_TE_STALE_MSG)
    except sqlite3.IntegrityError:
        again = _te_decision_row(user_id, fp)
        if again is not None:
            return _te_replay(user_id, again)
        raise HTTPException(status_code=500, detail="Couldn't save that choice. Please try again.")

    return {"status": "kept", "decision_id": did, "decision": "kept",
            "conflict_type": tec.CONFLICT_TYPE,
            "scope": "occurrence" if recurring else "series",
            "occurrence_date": occ_date, "already": False}


@app.post("/api/schedule/task-conflicts/{fingerprint}/alternatives")
def task_conflict_alternatives(fingerprint: str, current: dict = Depends(_rl("schedule_decision"))):
    """Server-generated candidate windows that MOVE THE TASK only. Each preserves
    the task's exact duration, is not in the past, stays within the next two
    weeks, and does not overlap a timed event, a recurring event occurrence, or
    another scheduled task window. Never modifies anything."""
    fp, user_id = _te_fp_common(fingerprint, current)
    conn = get_db()
    try:
        m = _te_match(conn, user_id, fp)
        if m is None:
            raise HTTPException(status_code=409, detail=_TE_STALE_MSG)
        task_row, task_win, occ = m
        recurring = bool(occ.get("is_recurring_instance"))
        occ_date = occ.get("occurrence_date") if recurring else None
        cands = _te_candidates(conn, user_id, task_row, task_win)
    finally:
        conn.close()

    task_pub = {"id": task_row["id"], "title": task_row.get("title"),
                "start": task_win[0].isoformat(), "end": task_win[1].isoformat(),
                "due_date": task_row.get("due_date")}
    scope = "occurrence" if recurring else "series"
    if not cands:
        msg = _TE_NO_ALT_BEFORE_DUE_MSG if task_row.get("due_date") else _TE_NO_ALT_MSG
        return {"proposal_id": None, "task": task_pub, "candidates": [],
                "message": msg, "scope": scope, "occurrence_date": occ_date}

    pid = "tmp_" + uuid.uuid4().hex
    now = _te_now()
    created = get_iso_now()
    expires = (now + datetime.timedelta(seconds=_TE_MOVE_TTL_S)).isoformat()
    window_json = json.dumps({"start": task_win[0].isoformat(), "end": task_win[1].isoformat()},
                             sort_keys=True)
    try:
        with db_layer.transaction() as conn:
            conn.execute(
                """INSERT INTO task_move_proposals
                   (id, user_id, fingerprint, task_id, event_id, occurrence_date, status,
                    candidates_json, chosen_index, task_window_json, exec_token,
                    created_at, expires_at, confirmed_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, NULL, ?, ?, ?, ?, NULL)""",
                (pid, user_id, fp, task_row["id"], occ["id"], occ_date,
                 json.dumps(cands, sort_keys=True, default=str), window_json,
                 "tmp:" + pid, created, expires),
            )
    except Exception as e:
        print(f"TE_ALT_ERROR: {type(e).__name__}")
        raise HTTPException(status_code=500, detail="Couldn't prepare alternatives. Please try again.")

    return {"proposal_id": pid, "task": task_pub, "candidates": cands,
            "message": None, "scope": scope, "occurrence_date": occ_date}


@app.post("/api/schedule/task-conflicts/alternatives/{proposal_id}/cancel")
def cancel_task_alternatives(proposal_id: str, current: dict = Depends(_rl("schedule_decision"))):
    user_id = current["id"]
    p = _te_get_owned_proposal(proposal_id, user_id)
    if p is None:
        raise HTTPException(status_code=404, detail="That proposal isn’t available.")
    if p["status"] == "confirmed":
        raise HTTPException(status_code=409, detail="This move was already applied.")
    if p["status"] in ("cancelled", "expired"):
        return {"status": "cancelled", "already": True}
    with db_layer.transaction() as conn:
        conn.execute(
            "UPDATE task_move_proposals SET status = 'cancelled' "
            "WHERE id = ? AND user_id = ? AND status = 'pending'",
            (proposal_id, user_id),
        )
    return {"status": "cancelled", "already": False}


@app.post("/api/schedule/task-conflicts/alternatives/{proposal_id}/confirm")
def confirm_task_move(proposal_id: str, req: TaskMoveConfirm,
                      current: dict = Depends(_rl("schedule_decision"))):
    """Apply the chosen server candidate to the TASK WINDOW ONLY — planned_at +
    scheduled_end — through the same canonical task-update path a manual edit
    uses. The task's due_date is never touched and the event is never moved.
    Atomic, exactly once; a stale proposal is rejected without mutating
    anything and a newer manual edit is preserved."""
    user_id = current["id"]
    p = _te_get_owned_proposal(proposal_id, user_id)
    if p is None:
        raise HTTPException(status_code=404, detail="That proposal isn’t available.")
    fp = p["fingerprint"]

    if p["status"] == "confirmed":
        dec = _te_decision_row(user_id, fp)
        if dec is not None:
            return _te_replay(user_id, dec)
        raise HTTPException(status_code=409, detail=_TE_STALE_MSG)
    if p["status"] == "cancelled":
        raise HTTPException(status_code=409, detail="This proposal was cancelled.")
    now = _te_now()
    if p["status"] == "expired" or datetime.datetime.fromisoformat(p["expires_at"]) <= now:
        with db_layer.transaction() as conn:
            conn.execute("UPDATE task_move_proposals SET status = 'expired' "
                         "WHERE id = ? AND user_id = ? AND status = 'pending'",
                         (proposal_id, user_id))
        raise HTTPException(status_code=409, detail="This proposal expired. Search again.")

    try:
        cands = json.loads(p["candidates_json"]) or []
    except (TypeError, ValueError):
        cands = []
    idx = req.candidate_index
    if not isinstance(idx, int) or idx < 0 or idx >= len(cands):
        raise HTTPException(status_code=422, detail="Choose one of the offered times.")
    chosen = cands[idx]

    prior = _te_decision_row(user_id, fp)
    if prior is not None:
        return _te_replay(user_id, prior)

    try:
        cs = datetime.datetime.fromisoformat(chosen["start"])
        ce = datetime.datetime.fromisoformat(chosen["end"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=409, detail=_TE_STALE_MSG)

    did = "ted_" + uuid.uuid4().hex
    try:
        with db_layer.transaction() as conn:
            m = _te_match(conn, user_id, fp)
            if m is None:
                raise _ScStale()
            task_row, task_win, occ = m
            recurring = bool(occ.get("is_recurring_instance"))
            occ_date = occ.get("occurrence_date") if recurring else None

            # candidate must still be valid: same duration, not past, in horizon
            if (ce - cs) != (task_win[1] - task_win[0]) or cs <= now \
                    or ce > now + datetime.timedelta(days=_TE_HORIZON_DAYS + 1):
                raise _ScStale()
            # ...and still finish on or before the (possibly newly edited) deadline
            existing = _get_owned_task(user_id, task_row["id"])
            if existing is None:
                raise _ScStale()
            due_end = _te_due_end(existing)
            if due_end is not None and ce > due_end:
                raise _ScStale()
            # ...and still collision-free on a FRESH read (events + other tasks)
            for bs, be in _te_busy_intervals(conn, user_id, task_row["id"]):
                if sched.intervals_overlap(chosen["start"], chosen["end"], bs, be):
                    raise _ScStale()
            fields = _task_update_columns(
                existing, {"planned_at": chosen["start"], "scheduled_end": chosen["end"]}
            )
            _exec_task_update(conn, user_id, task_row["id"], fields)

            applied = {"task_id": task_row["id"], "start": chosen["start"], "end": chosen["end"]}
            original = {
                "task": {"start": task_win[0].isoformat(), "end": task_win[1].isoformat()},
                "event": {"id": occ["id"], "start": occ["start"], "end": occ["end"]},
            }
            nowi = get_iso_now()
            conn.execute(
                """INSERT INTO task_event_conflict_decisions
                   (id, user_id, fingerprint, task_id, event_id, occurrence_date, decision,
                    original_json, proposal_json, applied_json, exec_token,
                    created_at, decided_at, stale_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'moved', ?, ?, ?, ?, ?, ?, NULL)""",
                (did, user_id, fp, task_row["id"], occ["id"], occ_date,
                 json.dumps(original, sort_keys=True, default=str),
                 json.dumps(chosen, sort_keys=True, default=str),
                 json.dumps(applied, sort_keys=True, default=str),
                 "ted:" + did, nowi, nowi),
            )
            conn.execute(
                "UPDATE task_move_proposals SET status = 'confirmed', chosen_index = ?, "
                "confirmed_at = ? WHERE id = ? AND user_id = ? AND status = 'pending'",
                (idx, nowi, proposal_id, user_id),
            )
    except _ScStale:
        raise HTTPException(status_code=409, detail=_TE_STALE_MSG)
    except HTTPException:
        raise
    except sqlite3.IntegrityError:
        again = _te_decision_row(user_id, fp)
        if again is not None:
            return _te_replay(user_id, again)
        raise HTTPException(status_code=500, detail="Couldn't apply that move. Please try again.")
    except Exception as e:
        print(f"TE_MOVE_ERROR: {type(e).__name__}")
        raise HTTPException(status_code=500, detail="Couldn't apply that move. Please try again.")

    t = _get_owned_task(user_id, task_row["id"])
    return {
        "status": "moved", "decision_id": did, "decision": "moved", "already": False,
        "conflict_type": tec.CONFLICT_TYPE,
        "scope": "occurrence" if recurring else "series", "occurrence_date": occ_date,
        "resolution": applied, "task": _task_out(t) if t else None,
    }


# --- tasks CRUD ---
@app.post("/api/tasks", status_code=status.HTTP_201_CREATED)
def create_task_endpoint(req: TaskCreate, response: Response,
                         current: dict = Depends(get_current_user)):
    user_id = current["id"]
    try:
        token = idem.normalize_token(req.client_token)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    fields = _validate_task_fields(
        require_title=True, title=req.title, notes=req.notes,
        due_date=req.due_date, planned_at=req.planned_at,
        scheduled_end=req.scheduled_end, reminder_at=req.reminder_at,
    )
    _task_window_check(fields.get("planned_at"), fields.get("scheduled_end"))
    fp = idem.request_fingerprint({k: fields.get(k) for k in
                                   ("title", "notes", "due_date", "planned_at", "reminder_at")})

    def _replay(prior):
        prior_id, prior_hash = prior
        if prior_hash is not None and prior_hash != fp:
            raise idem.IdempotencyConflict()
        response.status_code = status.HTTP_200_OK
        return {"status": "SUCCESS", "idempotent": True,
                "task": _task_out(_get_owned_task(user_id, prior_id))}

    if token:
        prior = _prior_create("tasks", user_id, token)
        if prior is not None:
            return _replay(prior)

    task_id = str(uuid.uuid4())
    now = get_iso_now()
    reminder_status = "PENDING" if fields.get("reminder_at") else "NONE"
    try:
        with db_layer.transaction() as conn:
            conn.execute(
                """INSERT INTO tasks
                   (id, user_id, title, notes, is_completed, due_date, planned_at,
                    scheduled_end, reminder_at, reminder_status, completed_at, created_at, updated_at,
                    client_token, client_request_hash)
                   VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)""",
                (task_id, user_id, fields["title"], fields.get("notes"),
                 fields.get("due_date"), fields.get("planned_at"), fields.get("scheduled_end"),
                 fields.get("reminder_at"),
                 reminder_status, now, now, token, fp if token else None),
            )
    except sqlite3.IntegrityError:
        prior = _prior_create("tasks", user_id, token) if token else None
        if prior is not None:
            return _replay(prior)
        raise HTTPException(status_code=500, detail="Couldn't save that task. Please try again.")
    return {"status": "SUCCESS", "task": _task_out(_get_owned_task(user_id, task_id))}


@app.patch("/api/tasks/{task_id}")
def update_task_endpoint(task_id: str, req: TaskUpdate, current: dict = Depends(get_current_user)):
    user_id = current["id"]
    existing = _get_owned_task(user_id, task_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    supplied = req.model_dump(exclude_unset=True)
    fields = _task_update_columns(existing, supplied)
    if not fields:
        raise HTTPException(status_code=422, detail="No fields to update.")
    with db_layer.transaction() as conn:
        _exec_task_update(conn, user_id, task_id, fields)
    return {"status": "SUCCESS", "task": _task_out(_get_owned_task(user_id, task_id))}


def _set_task_completion(user_id: str, task_id: str, completed: bool) -> dict:
    if _get_owned_task(user_id, task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    now = get_iso_now()
    with db_layer.transaction() as conn:
        if completed:
            conn.execute(
                "UPDATE tasks SET is_completed = 1, completed_at = ?, "
                "reminder_status = CASE WHEN reminder_at IS NULL THEN reminder_status ELSE 'NONE' END, "
                "updated_at = ? WHERE id = ? AND user_id = ?",
                (now, now, task_id, user_id),
            )
        else:
            conn.execute(
                "UPDATE tasks SET is_completed = 0, completed_at = NULL, "
                "reminder_status = CASE WHEN reminder_at IS NULL THEN 'NONE' "
                "  WHEN reminder_at <= ? THEN 'DUE' ELSE 'PENDING' END, "
                "updated_at = ? WHERE id = ? AND user_id = ?",
                (now, now, task_id, user_id),
            )
    return {"status": "SUCCESS", "task": _task_out(_get_owned_task(user_id, task_id))}


@app.post("/api/tasks/{task_id}/complete")
def complete_task(task_id: str, current: dict = Depends(get_current_user)):
    return _set_task_completion(current["id"], task_id, True)


@app.post("/api/tasks/{task_id}/uncomplete")
def uncomplete_task(task_id: str, current: dict = Depends(get_current_user)):
    return _set_task_completion(current["id"], task_id, False)


@app.delete("/api/tasks/{task_id}")
def delete_task_endpoint(task_id: str, current: dict = Depends(get_current_user)):
    user_id = current["id"]
    with db_layer.transaction() as conn:
        cur = conn.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id))
        deleted = cur.rowcount
    if not deleted:
        raise HTTPException(status_code=404, detail="Task not found.")
    return {"status": "SUCCESS"}


# --- In-app DUE reminder delivery (B11.1) -----------------------------------
# The scheduler still owns ONLY PENDING -> DUE. This surface lets a signed-in
# user see THEIR OWN due, unacknowledged reminders while they are using /
# reopening MONOA, and acknowledge them. There is NO push / Web Push / service
# worker / VAPID / FCM / APNs / email / SMS anywhere: acknowledgement is a
# fourth terminal `reminder_status` value ('ACKNOWLEDGED') written on the task
# row itself, so it persists across refresh, restart, logout and login and the
# reminder never reappears. The existing task edit / complete / uncomplete /
# delete paths already reset `reminder_status` (PENDING/NONE/DUE) whenever
# `reminder_at` or completion changes, so editing, clearing or rescheduling a
# reminder correctly clears the acknowledgement and the scheduler re-promotes
# it to DUE at the new time — no competing task model, no new lifecycle.
class DueReminder(BaseModel):
    task_id: str
    title: str
    reminder_at: str
    due_date: Optional[str] = None


class DueRemindersResult(BaseModel):
    reminders: list[DueReminder] = []
    # False when the account's `notifications` preference is OFF: the DUE task
    # rows are left completely untouched in the DB, they are simply not
    # surfaced. Turn the preference back on and this endpoint returns them.
    notifications_enabled: bool = True


@app.get("/api/reminders/due", response_model=DueRemindersResult)
def get_due_reminders(current: dict = Depends(get_current_user)):
    user_id = current["id"]
    conn = get_db()
    try:
        pref = conn.execute(
            "SELECT notifications FROM user_preferences WHERE user_id = ?", (user_id,),
        ).fetchone()
        notifications_enabled = True if pref is None else bool(pref["notifications"])
        if not notifications_enabled:
            return DueRemindersResult(reminders=[], notifications_enabled=False)
        rows = conn.execute(
            "SELECT id, title, reminder_at, due_date FROM tasks "
            "WHERE user_id = ? AND reminder_status = 'DUE' "
            "  AND (is_completed IS NULL OR is_completed = 0) "
            "  AND reminder_at IS NOT NULL AND reminder_at <> '' "
            "ORDER BY reminder_at ASC, id ASC",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    return DueRemindersResult(
        reminders=[
            DueReminder(
                task_id=r["id"], title=r["title"],
                reminder_at=r["reminder_at"], due_date=r["due_date"],
            )
            for r in rows
        ],
        notifications_enabled=True,
    )


@app.post("/api/reminders/{task_id}/ack")
def acknowledge_reminder(task_id: str, current: dict = Depends(get_current_user)):
    """Acknowledge a delivered DUE reminder. Owner-scoped (a 404 for anyone
    else's task id) and idempotent: a repeat call on an already-acknowledged
    or no-longer-DUE reminder is a successful no-op. Mutates nothing except
    `reminder_status` DUE -> ACKNOWLEDGED on the caller's own task."""
    user_id = current["id"]
    if _get_owned_task(user_id, task_id) is None:
        raise HTTPException(status_code=404, detail="Reminder not found.")
    now = get_iso_now()
    with db_layer.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET reminder_status = 'ACKNOWLEDGED', updated_at = ? "
            "WHERE id = ? AND user_id = ? AND reminder_status = 'DUE'",
            (now, task_id, user_id),
        )
    return {"status": "SUCCESS"}


@app.post("/api/recommendations/update")
def update_recommendation(req: RecommendationUpdate, current: dict = Depends(get_current_user)):
    """ACCEPTED / DISMISSED set a terminal status. SNOOZE keeps status PENDING
    but sets snooze_until so the card genuinely reappears after that time
    (a truthful 'Remind me later', not a hide-until-refresh)."""
    user_id = current["id"]
    action = (req.action or "").strip().upper()
    conn = get_db()
    try:
        owned = conn.execute(
            "SELECT id FROM ai_recommendations WHERE id = ? AND user_id = ?",
            (req.recommendation_id, user_id),
        ).fetchone()
        if owned is None:
            raise HTTPException(status_code=404, detail="Recommendation not found.")

        if action == "SNOOZE":
            minutes = req.snooze_minutes if (req.snooze_minutes and req.snooze_minutes > 0) else 180
            minutes = min(minutes, 60 * 24 * 14)  # cap 14 days
            until = (sched.myt_now() + datetime.timedelta(minutes=minutes)).isoformat()
            conn.execute(
                "UPDATE ai_recommendations SET snooze_until = ?, updated_at = ? "
                "WHERE id = ? AND user_id = ?",
                (until, get_iso_now(), req.recommendation_id, user_id),
            )
            conn.commit()
            return {"status": "SUCCESS", "snoozed_until": until}

        if action not in ("ACCEPTED", "DISMISSED"):
            raise HTTPException(status_code=422, detail="Invalid action.")
        conn.execute(
            "UPDATE ai_recommendations SET status = ?, snooze_until = NULL, updated_at = ? "
            "WHERE id = ? AND user_id = ?",
            (action, get_iso_now(), req.recommendation_id, user_id),
        )
        conn.commit()
        return {"status": "SUCCESS"}
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        print(f"DB_ERROR (update_recommendation): {str(e)}")
        raise HTTPException(status_code=500, detail="Couldn't update that item. Please try again.")
    finally:
        conn.close()

# --- 4. DASHBOARD & ACTION INBOX ENDPOINTS ---
def _dashboard_task_anchor_date(t: dict):
    """The MYT calendar date a task belongs to, mirroring the exact convention
    the Plan/Schedule day view already uses client-side (`taskAnchorKey` in
    app/schedule/page.tsx): planned_at, else due_date, else reminder_at. A task
    with none of those is undated and has no anchor date at all — Schedule's
    own day view buckets an undated open task into TODAY specifically (and
    only today), so Today reproduces that same rule rather than inventing a
    second one."""
    raw = t.get("planned_at") or t.get("due_date") or t.get("reminder_at")
    if not raw:
        return None
    dt = sched._parse(raw)
    return dt.date() if dt else None


def _dashboard_open_task_order(t: dict) -> tuple:
    # Soonest actionable time first (planned start, else due date); untimed
    # tasks after the timed ones; final tiebreak on created_at then id so
    # two identical requests always resolve to the same task.
    when = t.get("planned_at") or t.get("due_date") or ""
    return (when == "", when, t.get("created_at") or "", str(t.get("id") or ""))


@app.get("/api/dashboard-data")
def get_dashboard_data(current: dict = Depends(get_current_user)):
    """Today's authoritative summary. Each real source below (schedule,
    finance, activity) is fetched independently: one source failing produces
    a truthful per-section "unavailable" state, not a collapsed whole-page
    error. Nothing here is fabricated — an absent/failed source is reported
    as absent, never backfilled with an invented value."""
    user_id = current["id"]
    now = sched.myt_now()
    today_date = now.date()

    # --- schedule (independent source) -------------------------------- #
    schedule_ok = True
    schedule_error: Optional[str] = None
    next_item = "Nothing scheduled"
    next_event_today: Optional[dict] = None
    events_remaining_today = 0
    open_tasks_today: list[dict] = []
    unresolved_conflicts_today = 0
    hero = "Here's your day."
    try:
        f_iso, t_iso = sched.myt_day_bounds(today_date.isoformat())
        day = _load_schedule(user_id, f_iso, t_iso)
        now_iso = now.isoformat()
        upcoming = [e for e in day["events"]
                    if e.get("status") != "cancelled" and e.get("start") and e["start"] >= now_iso]
        events_remaining_today = len(upcoming)
        if upcoming:
            next_event_today = {"title": upcoming[0]["title"], "start": upcoming[0]["start"]}

        # B6/Batch-04 fix: a task only belongs to Today if it is anchored to
        # today's MYT date, OR has no anchor date at all (matching Schedule's
        # own undated-task-buckets-into-today rule). Previously every open
        # task the user had ever created — regardless of date, including ones
        # years in the future — was treated as "today's" task.
        open_tasks_today = sorted(
            (t for t in day["tasks"]
             if not t["is_completed"] and _dashboard_task_anchor_date(t) in (None, today_date)),
            key=_dashboard_open_task_order,
        )
        unresolved_conflicts_today = len(day["unresolved_conflicts"]) + len(day["unresolved_task_conflicts"])

        if upcoming:
            next_item = upcoming[0]["title"]
        elif open_tasks_today:
            next_item = open_tasks_today[0]["title"]

        ev_remaining = events_remaining_today
        open_count = len(open_tasks_today)
        if ev_remaining or open_count:
            bits = []
            if ev_remaining:
                bits.append(f"{ev_remaining} event{'' if ev_remaining == 1 else 's'} still to come today")
            if open_count:
                bits.append(f"{open_count} open task{'' if open_count == 1 else 's'}")
            hero = "You have " + " and ".join(bits) + "."
        else:
            hero = "You have no upcoming events today and no open tasks."
    except Exception as e:
        schedule_ok = False
        schedule_error = "Couldn't load your schedule right now."
        print(f"DASHBOARD_SCHEDULE_ERROR: {type(e).__name__}: {e}")

    # --- finance (independent source) ---------------------------------- #
    finance_ok = True
    finance_error: Optional[str] = None
    total_spending: Optional[float] = None
    finance_section: Optional[dict] = None
    try:
        snap = fin.get_snapshot(user_id)
        total_spending = snap["today_spending"]
        finance_section = {
            "currency": snap["currency"],
            "today_spending": snap["today_spending"],
            "daily_budget": snap["daily_budget"],
            "daily_used_pct": snap["daily_used_pct"],
            "daily_over_budget": snap["daily_over_budget"],
        }
    except Exception as e:
        finance_ok = False
        finance_error = "Couldn't load your spending right now."
        print(f"DASHBOARD_FINANCE_ERROR: {type(e).__name__}: {e}")

    # --- activity (independent source) ---------------------------------- #
    activity_ok = True
    activity_error: Optional[str] = None
    active_recommendations: list[dict] = []
    try:
        active_recommendations = _get_activity_recommendations(user_id)
    except Exception as e:
        activity_ok = False
        activity_error = "Couldn't load Activity right now."
        print(f"DASHBOARD_ACTIVITY_ERROR: {type(e).__name__}: {e}")

    # --- deterministic priority stack (contextual, not learned) --------- #
    # Ranked, real-data-only signals. Lower rank = shown first. Ties keep the
    # order they were appended in below. Empty when nothing is urgent — the
    # frontend then falls back to the plain `hero_insight` summary.
    priority: list[dict] = []
    if schedule_ok and unresolved_conflicts_today > 0:
        n = unresolved_conflicts_today
        priority.append({"rank": 0, "kind": "conflict",
                          "message": f"{n} schedule conflict{'' if n == 1 else 's'} need{'s' if n == 1 else ''} your decision."})
    if schedule_ok and next_event_today:
        start_dt = sched._parse(next_event_today["start"])
        if start_dt and datetime.timedelta(0) <= (start_dt - now) <= datetime.timedelta(minutes=DASHBOARD_EVENT_SOON_MINUTES):
            priority.append({"rank": 1, "kind": "event_soon",
                              "message": f"{next_event_today['title']} starts soon."})
    if finance_ok and finance_section and finance_section["daily_budget"] > 0:
        if finance_section["daily_over_budget"]:
            priority.append({"rank": 1, "kind": "budget_over",
                              "message": "You're over today's budget."})
        elif finance_section["daily_used_pct"] >= DASHBOARD_BUDGET_NEAR_PCT:
            priority.append({"rank": 2, "kind": "budget_near",
                              "message": f"You're at {finance_section['daily_used_pct']:.0f}% of today's budget."})
    if activity_ok and active_recommendations:
        priority.append({"rank": 2, "kind": "activity",
                          "message": active_recommendations[0]["message"]})
    if schedule_ok and open_tasks_today:
        n = len(open_tasks_today)
        priority.append({"rank": 3, "kind": "open_task",
                          "message": f"{n} open task{'' if n == 1 else 's'} for today."})
    priority.sort(key=lambda p: p["rank"])

    return {
        # Preserved exactly for existing callers/tests: total spending, the
        # single next item (event outranks task), and the plain-language
        # summary line.
        "total_spending": total_spending,
        "next_task": next_item,
        "hero_insight": hero,

        "date": today_date.isoformat(),
        "schedule_status": "ok" if schedule_ok else "error",
        "schedule_error": schedule_error,
        "finance_status": "ok" if finance_ok else "error",
        "finance_error": finance_error,
        "activity_status": "ok" if activity_ok else "error",
        "activity_error": activity_error,

        "finance": finance_section,
        "next_event_today": next_event_today,
        "events_remaining_today": events_remaining_today if schedule_ok else None,
        "open_tasks_today": len(open_tasks_today) if schedule_ok else None,
        "unresolved_conflicts_today": unresolved_conflicts_today if schedule_ok else None,
        "activity": {"active_recommendations": active_recommendations} if activity_ok else None,
        "priority": priority,
    }

import json

def extract_amount_from_text(text: str) -> Optional[float]:
    match = re.search(r"rm\s*([0-9]+(?:\.[0-9]+)?)|([0-9]+(?:\.[0-9]+)?)\s*rm", text, re.IGNORECASE)
    if not match:
        return None
    value = match.group(1) or match.group(2)
    try:
        return float(value)
    except ValueError:
        return None


def build_transaction_fallback(user_input: str) -> Optional[Dict[str, Any]]:
    amount = extract_amount_from_text(user_input)
    if amount is None:
        return None

    lowered = user_input.lower()
    description_candidates = [
        "breakfast", "lunch", "dinner", "coffee", "tea", "meal", "groceries",
        "bus", "taxi", "train", "petrol", "fuel", "parking", "grab", "ride",
        "medicine", "snacks", "movie", "shopping", "tickets", "milk"
    ]
    description = ""
    for candidate in description_candidates:
        if candidate in lowered:
            description = candidate
            break

    if not description:
        for marker in [" on ", " for ", " at "]:
            idx = lowered.find(marker)
            if idx != -1:
                tail = lowered[idx + len(marker):]
                words = re.split(r"\s+", tail)
                for word in words:
                    word = re.sub(r"[^a-z]", "", word)
                    if word and word not in {"today", "tomorrow", "yesterday", "this", "that", "there", "was"}:
                        description = word
                        break
                if description:
                    break

    if not description:
        return {"clarification": f"What did you spend the RM {amount:g} on?"}

    category_map = {
        "breakfast": "Food", "lunch": "Food", "dinner": "Food", "coffee": "Food", "tea": "Food",
        "meal": "Food", "groceries": "Food", "snacks": "Food", "milk": "Food",
        "bus": "Transport", "taxi": "Transport", "train": "Transport", "petrol": "Transport",
        "fuel": "Transport", "parking": "Transport", "grab": "Transport", "ride": "Transport",
        "medicine": "Health", "movie": "Entertainment", "shopping": "Shopping", "tickets": "Entertainment"
    }

    category = category_map.get(description, "Other")
    return {
        "amount": round(amount, 2),
        "type": "expense",
        "description": description,
        "category": category,
        "date": get_iso_now(),
    }


# --- AI SERVICE & ROUTER ---
class AIService:
    @staticmethod
    def interpret(user_input: str, context_date: Optional[str] = None,
                 context_entity: Optional[dict] = None,
                 user_interests: Optional[dict] = None) -> MoInterpretation:
        if not ai_client:
            raise ValueError("API_KEY_MISSING")

        current_time = get_iso_now()
        # Weekday-name lookup for the next 8 days — gpt-4o-mini is unreliable at
        # computing "next Monday" from a bare ISO timestamp by itself (verified
        # against the real model during the final correctness patch), so hand it
        # the concrete date for every weekday name instead of making it do the
        # arithmetic. Deterministic, bounded, no new capability — just removes a
        # source of model error for GET_SCHEDULE_SUMMARY / plan_day / any other
        # "next <weekday>" phrasing.
        try:
            _now_dt = datetime.datetime.fromisoformat(current_time)
        except ValueError:
            _now_dt = datetime.datetime.now(datetime.timezone.utc).astimezone()
        upcoming_days_line = "\n".join(
            f"  {(_now_dt + datetime.timedelta(days=_i)).strftime('%A')}: "
            f"{(_now_dt + datetime.timedelta(days=_i)).date().isoformat()}"
            for _i in range(8)
        )
        context_line = (
            f'\nRECENT CONVERSATIONAL CONTEXT: the user\'s most recent request concerned '
            f'the date {context_date}. Use this ONLY as a weak hint for "target_date" or a '
            f'relative date word ("today"/"that day") if THIS message does not itself specify '
            f'a date — never override an explicit date the user just gave.\n'
            if context_date else ""
        )
        entity_title = (context_entity or {}).get("entity_title")
        entity_type = (context_entity or {}).get("entity_type")
        entity_line = (
            f'\nRECENT CONVERSATIONAL CONTEXT: the {entity_type or "item"} most recently discussed '
            f'or acted on was "{entity_title}". If THIS message refers to it only by a bare pronoun '
            f'("it"/"that"/"this one"), OR this message supplies ONLY a detail Mo just asked for '
            f'(e.g. only a time, date, or amount, naming no record at all — such as replying "11:30 AM" '
            f'right after being asked when to move something), treat THIS message as still being about '
            f'"{entity_title}" and use it as the target_description — but never override an explicit '
            f'different name/record the user just gave.\n'
            if entity_title else ""
        )
        interests_line = _mo_interests_context_line(user_interests or {})

        system_prompt = f"""
You are Mo, MONOA's focused AI life-assistant and action layer.

Your job is to understand the user's ENTIRE message and return ONE valid JSON
object with this exact top-level shape:

{{
  "mode": "read" | "single_operation" | "multi_operation" | "plan_day" | "clarification" | "unsupported",
  "operations": [ ... zero or more operation objects, see below ... ],
  "unsupported_items": [ ... zero or more SHORT strings, see UNSUPPORTED MIXED WITH SUPPORTED below ... ],
  "narrative": "a short natural sentence, ONLY for plan_day, or omit",
  "clarification_question": "a short question, ONLY when mode is clarification, or omit"
}}

Each entry of "operations" has EXACTLY this shape (same as a single intent):

{{
  "intent": "<one of the allowed intents below>",
  "parameters": {{ ... intent-specific fields, see below ... }},
  "constraint": "hard" | "soft"   (OPTIONAL — only meaningful inside plan_day, see CONSTRAINTS below),
  "condition": "if_free"          (OPTIONAL — only meaningful on a CREATE_EVENT/CREATE_TASK item, see CONDITIONS below)
}}

Current system time:
{current_time}

The next 8 days by weekday name (use this table for "Monday"/"next Thursday"/
etc. — do not compute the date yourself, look it up here):
{upcoming_days_line}

The current system time above is ONLY for resolving relative words like
"today"/"tomorrow"/"next Monday". NEVER copy it into an event, task or
reminder's own start/due/reminder time as a filler value when the user did
not give one — see NEVER INVENT A TIME OR A CADENCE below.
{context_line}{entity_line}{interests_line}
Base currency: MYR (Malaysian Ringgit).

========================
MODE SELECTION
========================

- "read": the user is ONLY asking a question (schedule/spending/today, a basic
  everyday utility question, or "when should I do X" advice) — exactly one
  operations[] entry: a GET_* intent, GENERAL_UTILITY, or SUGGEST_FREE_TIME.
- "single_operation": the user is requesting exactly ONE create/update/delete.
- "multi_operation": the user is requesting TWO OR MORE create/update/delete
  actions in one message (may mix create, update and delete freely).
- "plan_day": the user is asking you to help PLAN a day/period around several
  items they describe (e.g. "can you plan my day, I have X, Y, Z"), not just
  listing items to add verbatim. Represent every item as an operations[] entry
  (usually CREATE_EVENT/CREATE_TASK) and write a short "narrative" sentence.
- "clarification": something essential is missing or ambiguous and you cannot
  safely proceed with ANY part of the request. operations[] is empty.
- "unsupported": the ENTIRE request is outside MONOA's supported actions
  (see UNSUPPORTED REQUESTS below). operations[] is empty.

CRITICAL RULE — NEVER SILENTLY DROP AN ITEM:
If the user's message names or describes more than one relevant item, EVERY
item must end up in exactly ONE of these three places:
  - an operations[] entry (Mo can act on it), OR
  - "clarification_question" (something essential about it is missing), OR
  - "unsupported_items" (it is real and understood, but outside what Mo can
    do — see UNSUPPORTED MIXED WITH SUPPORTED below).
Never quietly represent only some items and ignore the rest.

ALLOWED INTENTS (used inside each operations[] entry):
- CREATE_TRANSACTION
- GET_SPENDING_SUMMARY
- CREATE_TASK
- CREATE_REMINDER
- CREATE_EVENT
- GET_SCHEDULE_SUMMARY
- UPDATE_TASK
- DELETE_TASK
- UPDATE_EVENT
- DELETE_EVENT
- GET_TODAY_SUMMARY
- GENERAL_UTILITY
- SUGGEST_FREE_TIME

ALLOWED TRANSACTION CATEGORIES:
- Food
- Transport
- Shopping
- Personal Care
- Bills
- Entertainment
- Health
- Education
- Travel
- Other

========================
IMPORTANT OUTPUT RULES
========================

Return JSON ONLY.

Never return markdown.
Never return explanations outside JSON.

Every section below (CREATE_TRANSACTION, CREATE_TASK, CREATE_EVENT, ...) shows
the shape of ONE operations[] entry — its "Return: {{ "intent": ..., "parameters":
{{...}} }}" example is exactly one item to place inside the top-level
"operations" array, with "mode" set correctly ("read" for a GET_* entry,
"single_operation" for exactly one create/update/delete, "multi_operation" or
"plan_day" when there is more than one).

========================
CREATE_TRANSACTION
========================

When the user says they spent or earned money and provides enough information:

Use EXACTLY these parameter names:

{{
  "amount": number,
  "type": "expense" or "income",
  "description": "short description",
  "category": "Food",
  "date": "ISO-8601 datetime with timezone"
}}

Example:

User:
"I spent RM45 on Shopee"

Correct:

{{
  "intent": "CREATE_TRANSACTION",
  "parameters": {{
    "amount": 45,
    "type": "expense",
    "description": "Shopee purchase",
    "category": "Shopping",
    "date": "{current_time}"
  }}
}}

If the user says:

"I spent RM20"

There is not enough information about what was purchased.

Return:

{{
  "intent": "CLARIFICATION_REQUIRED",
  "parameters": null,
  "clarification_question": "What did you spend the RM 20 on?"
}}

Do NOT invent a description when the user has provided none.

========================
GET_SPENDING_SUMMARY
========================

If the user asks how much they spent, their spending today, or a similar spending-summary question:

Return:

{{
  "intent": "GET_SPENDING_SUMMARY",
  "parameters": null,
  "clarification_question": null
}}

========================
CREATE_TASK
========================

For a task such as:

"Remind me to work on my FYP tomorrow."

or:

"I need to work on my FYP tomorrow."

Use EXACTLY these parameter names:

{{
  "title": "Work on my FYP",
  "due_date": "ISO-8601 datetime with timezone or null",
  "reminder_at": "ISO-8601 datetime with timezone or null"
}}

IMPORTANT:

For tasks, ALWAYS use "title".

NEVER use:
"description"
"task"
"name"

Example:

User:
"I need to work on my FYP tomorrow at 10 AM."

Return (illustrative — "due_date" here is the RESOLVED "tomorrow at 10 AM",
computed from the current system time and weekday table above; it is NEVER
the current system time itself):

{{
  "intent": "CREATE_TASK",
  "parameters": {{
    "title": "Work on my FYP",
    "due_date": "2026-09-16T10:00:00+08:00",
    "reminder_at": null
  }}
}}

Adjust the date/time correctly based on the current system time — but always
compute the time the user actually stated. Never substitute the current
system time for a stated time you did not fully resolve.

========================
CREATE_REMINDER
========================

For requests such as:

"Remind me to work on my FYP tomorrow at 10 AM."

Return:

{{
  "intent": "CREATE_REMINDER",
  "parameters": {{
    "title": "Work on my FYP",
    "due_date": null,
    "reminder_at": "2026-08-30T10:00:00+08:00"
  }}
}}

IMPORTANT:

For reminders, ALWAYS use:

"title"
"due_date"
"reminder_at"

NEVER use "description".

The reminder time must be placed in "reminder_at".

========================
CREATE_EVENT
========================

For a TIME-BASED COMMITMENT (a meeting, class, session, appointment, reservation)
such as:

"Add a meeting tomorrow at 3pm"
"Badminton on Saturday 6-8pm at the community club"
"Dentist appointment Friday 10am"

Use EXACTLY these parameter names:

{{
  "title": "Meeting",
  "start": "ISO-8601 datetime with timezone",
  "end": "ISO-8601 datetime with timezone or null",
  "location": "human-readable place or null",
  "all_day": false
}}

Return:

{{
  "intent": "CREATE_EVENT",
  "parameters": {{
    "title": "Meeting",
    "start": "2026-09-03T15:00:00+08:00",
    "end": null,
    "location": null,
    "all_day": false
  }}
}}

Use CREATE_EVENT for scheduled commitments; use CREATE_TASK for things the user
must DO (finish slides, submit assignment) that are not themselves a fixed
time-slot commitment.

========================
EVENT vs TASK vs REMINDER — semantic classification (not "clock time = event")
========================

Classify by MEANING, not merely by whether a clock time is present. A stated
time attached to an ACTIVITY the user will DO AT that time is an event; a
stated time that is a DEADLINE BY WHICH something must be finished is a task;
"remind me at/to ..." is a reminder. In every case, the exact date AND time
the user gave must be fully resolved and carried through — NEVER drop it to a
date-only value, and never substitute the current system time for it.

"study session tomorrow at 7 PM"    -> CREATE_EVENT, start = tomorrow 19:00
"gym tomorrow at 8"                 -> CREATE_EVENT, start = tomorrow 20:00
"football training at 5"            -> CREATE_EVENT, start = today/implied 17:00
"lunch at 1:30"                     -> CREATE_EVENT, start = today/implied 13:30
"Submit assignment by 7 PM"         -> CREATE_TASK, due_date = today 19:00
"Finish report before 9"            -> CREATE_TASK, due_date = today 09:00
"Remind me at 7 to call Mum"        -> CREATE_REMINDER, reminder_at = today 19:00

A session/class/practice/appointment/training/meeting-shaped activity with a
stated time is an event even if the word "session" alone might sound like a
to-do — the presence of a specific clock time the user will BE somewhere or
DOING something at is the signal, not the word itself.

========================
GET_SCHEDULE_SUMMARY
========================

If the user asks what their day / schedule looks like, whether they are busy,
or asks to look at / discuss a SPECIFIC day ("let's look at Monday", "what's
Thursday looking like", "show me next Tuesday"):

Return:

{{
  "intent": "GET_SCHEDULE_SUMMARY",
  "parameters": {{
    "target_date": "resolved ISO-8601 date (YYYY-MM-DD) ONLY if the user named a specific day, else omit entirely"
  }},
  "clarification_question": null
}}

For a general "what's my day/schedule like" question with no specific day
named, omit "target_date" (or set "parameters" to null) — that means today.

========================
UPDATE_TASK vs UPDATE_EVENT — which one?
========================

Same distinction as CREATE_TASK vs CREATE_EVENT above: a request that moves a
CLOCK TIME (e.g. "move gym from 8 to 7", "push my meeting to 5pm") is almost
always about a scheduled EVENT — use UPDATE_EVENT unless the user's own words
say "task" or "reminder". Use UPDATE_TASK only for something the user must DO
(finish slides, submit assignment) or explicitly calls a task/reminder.

========================
UPDATE_TASK (covers reminders too — a reminder is a task with a reminder time)
========================

For a request to CHANGE an existing task or reminder, such as:

"Move my FYP task to Friday"
"Change the reminder for Dr Tan to 3pm"
"Rename my grocery task to Weekly shopping"

You are NEVER given the task's ID and must NEVER invent one. Identify the task
only by a short phrase taken from what the user said, plus a resolved date/time
if the conversation makes one clear — the system looks it up deterministically
from these hints, never from an ID you supply. Only include a "new_*" field for
something the user actually asked to change; never invent a value for a field
they did not mention.

{{
  "intent": "UPDATE_TASK",
  "parameters": {{
    "target_description": "a short phrase identifying the task",
    "target_date": "resolved ISO-8601 date (YYYY-MM-DD) if the conversation implies one, else omit",
    "target_time": "the task's CURRENT/ORIGINAL due_date or reminder_at if the user referenced it (e.g. 'move gym from 8 to 7' implies 20:00), else omit",
    "new_title": "string, or omit if unchanged",
    "new_due_date": "ISO-8601 datetime with timezone, or omit if unchanged",
    "new_reminder_at": "ISO-8601 datetime with timezone, or omit if unchanged",
    "new_planned_at": "ISO-8601 datetime with timezone, or omit if unchanged"
  }}
}}

========================
DELETE_TASK (covers reminders too)
========================

For a request to REMOVE an existing task or reminder, such as:

"Delete my FYP task"
"Remove the reminder to email Dr Tan"

{{
  "intent": "DELETE_TASK",
  "parameters": {{
    "target_description": "a short phrase identifying the task",
    "target_date": "resolved ISO-8601 date, if the conversation implies one, else omit",
    "target_time": "the task's current due_date/reminder_at if referenced, else omit"
  }}
}}

========================
UPDATE_EVENT
========================

For a request to CHANGE an existing event, such as:

"Move my meeting to 5pm"
"Change my dentist appointment to next Tuesday"
"Rename my call with the supervisor"

Same ID rule as UPDATE_TASK: NEVER invent an ID, identify only by a short
phrase plus a resolved date/time hint when the conversation makes one clear.
Only include a "new_*" field for something the user actually asked to change.

CRITICAL — a new time vs a new time-of-day only: you are NEVER told the
event's real current date (target resolution happens later, deterministically,
against the real database) — so you must NEVER guess one. If the user's
request names a NEW CLOCK TIME but does NOT name any new date (no "tomorrow",
no weekday name, no explicit calendar date, no other relative day word),
return "new_start_time_only" (just "HH:MM", 24-hour) and do NOT return
"new_start" — the server applies that time to the event's OWN existing date.
Only return "new_start" (a full resolved ISO-8601 datetime) when the user's
OWN words name a new date.

  "Move Design Review to 7 PM"            -> {{"new_start_time_only": "19:00"}}
  "Move Design Review to tomorrow at 7pm" -> {{"new_start": "<tomorrow's date>T19:00:00+08:00"}}

{{
  "intent": "UPDATE_EVENT",
  "parameters": {{
    "target_description": "a short phrase identifying the event",
    "target_date": "resolved ISO-8601 date (YYYY-MM-DD) if the conversation implies one, else omit",
    "target_time": "the event's CURRENT/ORIGINAL start datetime if the user referenced it (e.g. 'move gym from 8 to 7' implies today/that day at 20:00), else omit",
    "new_title": "string, or omit if unchanged",
    "new_start": "ISO-8601 datetime with timezone — ONLY when the user named an explicit new date; omit otherwise",
    "new_start_time_only": "\"HH:MM\" 24-hour clock — ONLY when the user named a new time with NO new date; omit otherwise",
    "new_end": "ISO-8601 datetime with timezone, or omit if unchanged",
    "new_location": "string, or omit if unchanged"
  }}
}}

========================
DELETE_EVENT
========================

For a request to REMOVE an existing event, such as:

"Delete my dentist appointment"
"Cancel my meeting tomorrow"

{{
  "intent": "DELETE_EVENT",
  "parameters": {{
    "target_description": "a short phrase identifying the event",
    "target_date": "resolved ISO-8601 date, if the conversation implies one, else omit",
    "target_time": "the event's current start datetime if referenced, else omit"
  }}
}}

========================
GET_TODAY_SUMMARY
========================

If the user asks a broad "what do I have going on" / "how's my day looking
overall" question that is about BOTH their schedule and their spending
together (not only one of them):

{{
  "intent": "GET_TODAY_SUMMARY",
  "parameters": null,
  "clarification_question": null
}}

If the question is clearly ONLY about schedule, use GET_SCHEDULE_SUMMARY.
If it is clearly ONLY about spending, use GET_SPENDING_SUMMARY.

========================
GENERAL_UTILITY (mode: read) — bounded everyday intelligence
========================

For a simple, self-contained everyday question that has nothing to do with
MONOA's own data — arithmetic, a percentage, a unit conversion, a time
difference, a short definition, or simple common-sense reasoning:

{{
  "intent": "GENERAL_UTILITY",
  "parameters": {{
    "kind": "arithmetic" | "time_diff" | "general",
    "expression": "a PLAIN numeric expression using only + - * / % ** and parentheses — ONLY for kind=arithmetic, e.g. \"150 * 0.20\" for '20% of 150', or \"2.5 * 60\" for 'minutes in 2.5 hours'",
    "unit_suffix": "a short unit to append to the computed number, e.g. \" minutes\", or omit",
    "from_time": "ISO-8601 datetime — ONLY for kind=time_diff",
    "to_time": "ISO-8601 datetime — ONLY for kind=time_diff",
    "answer_text": "your own short, direct answer — used for kind=general, and as a fallback if a kind=arithmetic expression cannot be built"
  }},
  "clarification_question": null
}}

The backend computes "arithmetic"/"time_diff" itself from your expression —
it does NOT trust your own arithmetic, so always give a clean "expression"
(or from_time/to_time) rather than only prose. For "kind": "general" (a
definition or simple explanation), your own "answer_text" is used directly —
keep it to one or two short sentences, no preamble.

Examples:
"What is 1 + 1?" -> kind "arithmetic", expression "1 + 1"
"20% of 150" -> kind "arithmetic", expression "150 * 0.20"
"How many minutes are in 2.5 hours?" -> kind "arithmetic", expression "2.5 * 60", unit_suffix " minutes"
"What does procrastination mean?" -> kind "general", answer_text "Putting off tasks despite knowing it'll cost you later."

Do NOT use GENERAL_UTILITY for anything about the user's own MONOA data
(schedule, spending, tasks) — those already have their own intents above. Do
NOT use it for open-ended research, current events, or anything requiring
outside information Mo cannot compute itself.

========================
SUGGEST_FREE_TIME (mode: read) — personalized judgment from the real schedule
========================

For "when should I study/work/do X" / "where's my free time" type questions:

{{
  "intent": "SUGGEST_FREE_TIME",
  "parameters": {{
    "target_date": "resolved ISO-8601 date (YYYY-MM-DD), or omit for today",
    "purpose": "a short label for what they're finding time for, e.g. \"study\", or omit",
    "duration_minutes": "how long a block they need, ONLY if the user said one, else omit (backend default applies)"
  }},
  "clarification_question": null
}}

The backend reads the user's REAL schedule and finds an actual open block —
you never guess a time yourself; do not put a time in "answer_text" or
anywhere else for this intent.

========================
MULTI-CREATE (mode: multi_operation)
========================

User:
"Add a meeting at 10 AM, lunch at 2 PM, car oil change at 6 PM, and gym at 8 PM."

Return:

{{
  "mode": "multi_operation",
  "operations": [
    {{"intent": "CREATE_EVENT", "parameters": {{"title": "Meeting", "start": "...T10:00:00+08:00", "end": null, "location": null, "all_day": false}}}},
    {{"intent": "CREATE_EVENT", "parameters": {{"title": "Lunch", "start": "...T14:00:00+08:00", "end": null, "location": null, "all_day": false}}}},
    {{"intent": "CREATE_EVENT", "parameters": {{"title": "Car oil change", "start": "...T18:00:00+08:00", "end": null, "location": null, "all_day": false}}}},
    {{"intent": "CREATE_EVENT", "parameters": {{"title": "Gym", "start": "...T20:00:00+08:00", "end": null, "location": null, "all_day": false}}}}
  ]
}}

Every item the user named MUST appear. Never return only the first one.

========================
MIXED OPERATIONS (mode: multi_operation)
========================

User:
"Move my meeting to 5, delete my dentist appointment, and add gym tomorrow at 8."

Return operations containing, in this case, one UPDATE_EVENT, one DELETE_EVENT
and one CREATE_EVENT — each shaped exactly as its own section above describes.

========================
PLAN_DAY (mode: plan_day)
========================

Use "plan_day" when the user asks you to PLAN or organize a day/period around
several items, e.g. "can you plan my day, I have a meeting at 9, lunch at
1:30, football at 5, and recovery at 7."

Represent every stated item as a CREATE_EVENT or CREATE_TASK operations[]
entry (use CREATE_TASK only for a flexible to-do with no fixed time, like
"I need to study for two hours"). Write one short "narrative" sentence
explaining the plan in plain language — you do NOT need to check for
conflicts or existing commitments yourself; the backend does that
deterministically against the user's real schedule after you respond.

If an item genuinely needs a real time slot (a class, a session, an
appointment) and the user did not give one, do not guess a plausible-sounding
time for it — see NEVER INVENT A TIME OR A CADENCE below. Only fall back to a
flexible, timeless CREATE_TASK when the item was never meant to have a fixed
slot in the first place (e.g. "study for two hours" with no stated time).

========================
NEVER INVENT A TIME OR A CADENCE
========================

If the user asks you to create one or more time-based items (CREATE_EVENT, or
a CREATE_TASK with a due/reminder/planned time) and a piece that materially
affects WHEN it happens is missing, do NOT guess it — not the current system
time, not a "reasonable-sounding" time, not an arbitrary spread across days.
Use "mode": "clarification" instead and ask ONLY for the specific piece that
is missing (never re-ask for something the user already gave).

Two things can be missing independently:
- TIME — the clock time (and, for a single item, the date) the item happens at.
- CADENCE — for MULTIPLE similar items, how they are spread out (which days,
  how many per week, over how long) — never assume "every day starting today"
  or any other specific pattern the user did not state or clearly imply.

Examples:

"Make 10 gym sessions." — both missing. mode "clarification":
"clarification_question": "What time do you prefer, and how should I spread
the 10 sessions — for example 3–4 times a week?"
(You may suggest a sensible cadence in the question itself, like the example
above, but you must NOT act on your own suggestion — only the user's answer
can supply it.)

"Make 10 gym sessions at 7 PM." — time given, cadence missing. mode
"clarification", ask ONLY: "How should I spread the 10 sessions — for example
3–4 times a week?"

"Schedule gym every Monday and Wednesday for 5 weeks." — cadence fully given
(which days, how many weeks); only time is missing. mode "clarification", ask
ONLY: "What time should I put gym at on those days?"

This applies equally inside plan_day and multi_operation — a narrative or a
batch is never an excuse to fabricate a time or a spread you were not given.

========================
CONSTRAINTS — hard vs soft (used inside plan_day, optional elsewhere)
========================

Set "constraint": "hard" when a time is fixed and should not move, e.g. "at
exactly 6 PM", "meeting at 9 AM" (a plainly stated time is hard by default —
you never need to set "constraint" for it; omit the field and it is treated
as hard).

Set "constraint": "soft" only when the user's own wording signals flexibility,
e.g. "gym after 6", "dinner around 8", "I prefer studying before dinner". A
soft item may be nudged to a nearby free slot by the backend if it conflicts;
a hard item is never moved.

Only include a "duration_minutes" style detail inside "parameters" if the user
gave one explicitly, or if it is already implied by an explicit start+end. Do
NOT invent a duration you were not given and MONOA does not already know.

========================
CONDITIONS — the ONLY supported conditional (read + action)
========================

User:
"What do I have tomorrow, and add gym at 8 PM if I'm free."

Return TWO operations: a GET_SCHEDULE_SUMMARY read, and a CREATE_EVENT for gym
with "condition": "if_free". Do not invent any other conditional meaning —
"if_free" is the only recognized value, and it means: propose this item only
if it does not conflict with the real schedule; otherwise leave it out
entirely (the backend enforces this, you do not need to check availability
yourself).

========================
DESCRIBING VS REQUESTING — do not invent mutations
========================

Only create/update/delete something when the user's wording is an ACTUAL
request to change MONOA (add / schedule / create / plan / put / log / move /
delete / remove / cancel / change / update, or an unambiguous equivalent).

"I have a meeting at 10." (on its own, no action verb) -> mode "read", empty
operations — this is the user describing their day, not asking you to create
anything.

"I have a meeting at 10. What can I do before it?" -> mode "read" — answer
using their real schedule; do NOT create the meeting.

"Add a meeting at 10." -> mode "single_operation", CREATE_EVENT.

"Can you plan my day? I have a meeting at 10." -> mode "plan_day" — the
meeting is a candidate item for the plan (the backend will recognize it is
already on the schedule and will not duplicate it).

========================
UNSUPPORTED MIXED WITH SUPPORTED
========================

User:
"Add gym tomorrow at 8 and buy me new football shoes."

Return "mode": "multi_operation" with ONE operations[] entry for the gym
CREATE_EVENT, AND put a short description of the unsupported part in
"unsupported_items":

{{
  "mode": "multi_operation",
  "operations": [ {{ "intent": "CREATE_EVENT", "parameters": {{ ... gym ... }} }} ],
  "unsupported_items": ["buying football shoes"]
}}

The shoes purchase is outside MONOA's scope (no purchases, no payments, no
banking, no external services) — never add an operations[] entry for it, and
never leave it out of "unsupported_items" either. This is the ONLY correct way
to represent a mixed supported+unsupported request: it must NEVER be silently
missing from the response, and it must NEVER be actioned.

========================
DATES AND TIMES
========================

Convert relative dates and times such as:

today
tomorrow
tomorrow at 10 AM
next Monday
this evening

into complete ISO-8601 date/time values with the correct timezone.

Use the current system time provided above. For any weekday name ("Monday",
"next Thursday", etc.), use the weekday table above — never compute it
yourself.

Malaysia uses timezone +08:00.

========================
CLARIFICATION (mode: clarification)
========================

Use "mode": "clarification" — with EMPTY operations[] and a real
"clarification_question" — ONLY when something essential is missing or
ambiguous for the ENTIRE request. Ask only for what materially blocks safe
understanding; never interrogate for optional detail.

For a MULTI-item request where only ONE item is unclear and the rest are
perfectly clear, do NOT fall back to "clarification" for everything — instead
return "mode": "multi_operation" with operations[] containing every CLEAR
item, and put a short question about the unclear one in
"clarification_question" (e.g. user says "Meeting at 10, lunch later, gym at
8" -> operations = [Meeting, Gym], clarification_question = "What time should
I use for lunch?"). Never silently drop the unclear item without mentioning it.

Examples:

"I spent RM45 on Shopee" — has enough information (mode: single_operation).
"I spent RM20" — does not (mode: clarification, ask what it was for).

CLARIFICATION IS NEVER A PERMISSION QUESTION:
"clarification_question" exists ONLY to recover MISSING information Mo cannot
safely proceed without — it is never a way to ask the user's permission for
something Mo already fully understands. If the request is fully specified and
you could build a real operations[] entry for it, DO THAT — return
"single_operation"/"multi_operation"/"plan_day" instead; the app itself will
show the user a Confirm/Cancel choice before anything happens, so you never
need to ask "Do you want me to do this?" yourself. NEVER write a
"clarification_question" shaped like "Do you want me to <the exact
fully-specified thing you already understood>?" — if you can name the exact
thing, you can propose it instead of asking about it.

========================
UNSUPPORTED REQUESTS (mode: unsupported)
========================

If the ENTIRE user request is outside the supported MONOA actions (profile/
settings changes, purchases, payments, banking, external services, or
anything not in ALLOWED INTENTS), return:

{{
  "mode": "unsupported",
  "operations": [],
  "unsupported_items": ["a short description of what was asked for"]
}}

Always fill in "unsupported_items" here too (not just for the mixed case) —
it is what lets the response name the specific thing it can't do instead of a
generic refusal.

If only PART of a multi-item request is unsupported, do not mark the whole
thing unsupported — see UNSUPPORTED MIXED WITH SUPPORTED above.

BULK / WHOLE-RANGE CLEAR REQUESTS ARE UNSUPPORTED:
A request to delete or clear MANY items at once by a broad description rather
than by naming each one — e.g. "clear my whole schedule for this month",
"delete all my tasks", "wipe next week", "remove everything in September" —
is NOT something Mo can safely do in one action today. Return "mode":
"unsupported" with "unsupported_items" naming the bulk request (e.g.
["clearing your whole schedule for September"]) — do NOT return
"clarification" asking the user to confirm a mass delete, and do NOT return
DELETE_EVENT/DELETE_TASK operations for it (those only ever target ONE named,
resolvable item — see UPDATE_EVENT/DELETE_EVENT and UPDATE_TASK/DELETE_TASK
below). Deleting one specifically named event or task the user describes is
still fully supported and must use DELETE_EVENT/DELETE_TASK as normal.

========================
FINAL REQUIREMENT
========================

The top-level JSON must contain "mode", "operations" (an array, possibly
empty) and "unsupported_items" (an array, possibly empty). Each operations[]
entry must contain "intent" and, when required, "parameters" exactly as its
section above specifies.

For CREATE_TASK and CREATE_REMINDER, parameters MUST contain:

"title"
"due_date"
"reminder_at"

For CREATE_TRANSACTION, parameters MUST contain:

"amount"
"type"
"description"
"category"
"date"

For UPDATE_TASK, DELETE_TASK, UPDATE_EVENT and DELETE_EVENT, parameters MUST
contain "target_description". Never include a field id — the system resolves
the real target from "target_description" (+ "target_date"/"target_time" when
known) and the current owner's data.

Never substitute one field name for another. Never invent a database id
anywhere in the response.
"""

        try:
            _create_kwargs = dict(
                model=MO_MODEL,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt
                    },
                    {
                        "role": "user",
                        "content": user_input
                    }
                ],
            )
            if MO_MODEL.startswith("gpt-4o"):
                _create_kwargs["temperature"] = 0.0
            response = ai_client.chat.completions.create(**_create_kwargs)

            response_text = response.choices[0].message.content

            if not response_text:
                raise ValueError("Empty AI response")

            json_data = json.loads(response_text)

            # Defensive back-compat: if the model regresses to the old flat
            # {"intent": ..., "parameters": ...} shape despite the instructions,
            # wrap it into the new envelope rather than failing the whole turn.
            if "mode" not in json_data and "intent" in json_data:
                _intent = json_data.get("intent")
                if _intent == "CLARIFICATION_REQUIRED":
                    json_data = {"mode": "clarification", "operations": [],
                                 "clarification_question": json_data.get("clarification_question")}
                elif _intent == "ACTION_NOT_SUPPORTED":
                    json_data = {"mode": "unsupported", "operations": []}
                else:
                    json_data = {"mode": "single_operation",
                                 "operations": [{"intent": _intent, "parameters": json_data.get("parameters")}]}

            operations = json_data.get("operations") or []

            for op in operations:
                if not isinstance(op, dict):
                    continue
                op_intent = op.get("intent")
                params = op.get("parameters") or {}

                # TRANSACTION SAFETY FALLBACK — per-item, same rule as before.
                if op_intent == "CREATE_TRANSACTION":
                    required = ["amount", "type", "description", "category", "date"]
                    if not all(key in params for key in required):
                        fallback = build_transaction_fallback(user_input)
                        if fallback and "clarification" in fallback:
                            op["intent"] = "CLARIFICATION_REQUIRED_ITEM"  # marker; resolved below
                            op["_clarification"] = fallback["clarification"]
                        elif fallback:
                            op["parameters"] = fallback

                # TASK SAFETY
                if op_intent in ("CREATE_TASK", "CREATE_REMINDER"):
                    if "title" not in params and "description" in params:
                        params["title"] = params.pop("description")
                    params.setdefault("due_date", None)
                    params.setdefault("reminder_at", None)
                    op["parameters"] = params

                # EVENT SAFETY
                if op_intent == "CREATE_EVENT":
                    if "title" not in params and "description" in params:
                        params["title"] = params.pop("description")
                    params.setdefault("end", None)
                    params.setdefault("location", None)
                    params.setdefault("all_day", False)
                    op["parameters"] = params

            # A per-item transaction-safety fallback that itself needed
            # clarification is never silently dropped: if it's the ONLY
            # operation, the whole turn becomes a clarification (unchanged
            # single-item behavior); if it's one of SEVERAL, it is pulled out
            # and named in clarification_question while the rest proceed.
            unclear_items = [o for o in operations if o.get("intent") == "CLARIFICATION_REQUIRED_ITEM"]
            if unclear_items:
                remaining = [o for o in operations if o.get("intent") != "CLARIFICATION_REQUIRED_ITEM"]
                if not remaining:
                    json_data = {"mode": "clarification", "operations": [],
                                 "clarification_question": unclear_items[0]["_clarification"]}
                    operations = []
                else:
                    json_data["operations"] = remaining
                    json_data["clarification_question"] = unclear_items[0]["_clarification"]
                    operations = remaining

            # CLARIFICATION SAFETY (top-level)
            if json_data.get("mode") == "clarification" and not json_data.get("clarification_question"):
                amount = extract_amount_from_text(user_input)
                if amount is not None:
                    json_data["clarification_question"] = f"What did you spend the RM {amount:g} on?"
                else:
                    json_data["clarification_question"] = "Could you give me a little more detail?"

            # UNSUPPORTED-ITEM SAFETY: keep the field structurally bounded so it
            # can never become an unbounded/oversized display surface, and never
            # let a malformed value crash the turn — malformed simply means
            # "nothing to surface", never "fail the whole response".
            raw_unsupported = json_data.get("unsupported_items")
            if isinstance(raw_unsupported, list):
                json_data["unsupported_items"] = [
                    s.strip()[:120] for s in raw_unsupported
                    if isinstance(s, str) and s.strip()
                ][:5]
            else:
                json_data["unsupported_items"] = []

            if json_data.get("mode") not in MO_MODES:
                json_data["mode"] = "unsupported"
                json_data["operations"] = []

            return MoInterpretation(**json_data)

        except Exception as e:
            # Phase 5H-B6 (log privacy): a pydantic ValidationError stringifies
            # with the offending input_value, which on this path can be
            # AI-echoed financial text / a title / a location. Log the class,
            # not the value.
            print(f"AI_PARSE_ERROR: {type(e).__name__}")
            raise ValueError("MALFORMED_AI_OUTPUT")


# ========================================================================== #
# Mo — human-in-the-loop (B2)
#
#   POST /api/process-input            understand only; NEVER mutates domain data
#   POST /api/process-input/{id}/confirm   execute the stored proposal EXACTLY ONCE
#   POST /api/process-input/{id}/cancel    stop it for good
#   GET  /api/process-input/pending        restore an in-flight proposal after refresh
#
# The confirm handler is the ONLY path from Mo to a domain write, and it goes
# through the SAME AppServices.create_* functions the manual Plan / Wallet
# endpoints use. There is no authenticated immediate-execution route any more —
# the old `execute_action` helper and its execute-on-send behaviour are removed.
# ========================================================================== #

_MO_MUTATING_INTENT = {
    IntentEnum.CREATE_TRANSACTION: "create_transaction",
    IntentEnum.CREATE_TASK: "create_task",
    IntentEnum.CREATE_REMINDER: "create_task",
    IntentEnum.CREATE_EVENT: "create_event",
}
_MO_ACTIVE = ("pending", "executing")


def _mo_now() -> str:
    return get_iso_now()


def _mo_strip_identity(params) -> dict:
    p = dict(params or {})
    for k in ("user_id", "userId", "owner", "owner_id", "account", "account_id"):
        p.pop(k, None)
    return p


def _mo_get_owned(proposal_id: str, user_id: str):
    conn = get_db()
    try:
        r = conn.execute(
            "SELECT * FROM mo_proposals WHERE id = ? AND user_id = ?",
            (proposal_id, user_id),
        ).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _mo_expire_if_due(row: dict) -> dict:
    if row and mo.is_expired(row["status"], row["expires_at"], _mo_now()):
        with db_layer.transaction() as conn:
            conn.execute(
                "UPDATE mo_proposals SET status='expired' WHERE id=? AND status='pending'",
                (row["id"],),
            )
        return {**row, "status": "expired"}
    return row


# --------------------------------------------------------------------------- #
# Mo target resolution — UPDATE_TASK / DELETE_TASK / UPDATE_EVENT / DELETE_EVENT
#
# The model is NEVER given a task/event id and must never invent one (system
# prompt says so explicitly). It only extracts a short "target_description"
# phrase from what the user said; the ACTUAL lookup happens here, deterministically,
# against the caller's own rows only. Zero matches -> honest "not found" error.
# More than one match -> CLARIFICATION_REQUIRED rather than guessing which one.
# --------------------------------------------------------------------------- #
def _mo_find_tasks(user_id: str, query: str) -> list[dict]:
    q = (query or "").strip().lower()
    if not q:
        return []
    conn = get_db()
    try:
        rows = conn.execute(
            f"SELECT {_TASK_COLS} FROM tasks WHERE user_id = ? AND is_completed = 0",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows if q in (r["title"] or "").lower()]


def _mo_find_events(user_id: str, query: str) -> list[dict]:
    q = (query or "").strip().lower()
    if not q:
        return []
    conn = get_db()
    try:
        rows = conn.execute(
            f"SELECT {_EVENT_COLS} FROM schedule_events WHERE user_id = ? AND status != 'cancelled'",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows if q in (r["title"] or "").lower()]


def _mo_entity_named_in_text(user_id: str, text: str) -> tuple[Optional[str], Optional[str]]:
    """The reverse of _mo_find_events/_mo_find_tasks: scan `text` for exactly
    one of the caller's own (non-cancelled event / incomplete task) titles
    appearing as a substring. Returns (None, None) on zero or on more than
    one match — a hint is only ever remembered when it is unambiguous, never
    a guess between several of the user's own records."""
    t = (text or "").strip().lower()
    if not t:
        return None, None
    conn = get_db()
    try:
        ev_titles = [r["title"] for r in conn.execute(
            "SELECT DISTINCT title FROM schedule_events WHERE user_id = ? AND status != 'cancelled'",
            (user_id,),
        ).fetchall() if r["title"]]
        task_titles = [r["title"] for r in conn.execute(
            "SELECT DISTINCT title FROM tasks WHERE user_id = ? AND is_completed = 0",
            (user_id,),
        ).fetchall() if r["title"]]
    finally:
        conn.close()
    hits = [("event", title) for title in ev_titles if title.lower() in t]
    hits += [("task", title) for title in task_titles if title.lower() in t]
    if len(hits) == 1:
        return hits[0]
    return None, None


def _mo_narrow_by_discriminators(matches: list[dict], *, date_field: str, time_field: str,
                                 target_date, target_time, context_date) -> list[dict]:
    """Narrow an ambiguous match set using STRUCTURED discriminators only —
    never a substring/fuzzy pass. Order: explicit target_date, then the weaker
    context_date hint, then explicit target_time. Each step only applies if it
    actually narrows the set (never expands, never picks on a step that
    eliminates everything — a bad hint is simply ignored, not fatal)."""
    narrowed = matches
    for date_hint in (target_date, context_date):
        if not date_hint:
            continue
        cand = [m for m in narrowed if (m.get(date_field) or "")[:10] == date_hint[:10]]
        if cand:
            narrowed = cand
        if len(narrowed) == 1:
            return narrowed
    if target_time:
        cand = [m for m in narrowed if (m.get(time_field) or "") == target_time]
        if cand:
            narrowed = cand
    return narrowed


def _mo_resolve_task_target(user_id: str, target_description, *,
                            target_date=None, target_time=None, context_date=None):
    """-> (task_row, None) on exactly one SAFE match, or (None, understand_tuple)
    to return as-is (clarification / not-found — never guesses). Resolution is
    entirely deterministic and server-side: the caller (OpenAI) never supplies
    or invents a row id; target_date/target_time/context_date are hints only,
    used solely to narrow — the DB match is what actually decides."""
    desc = (target_description or "").strip()
    if not desc:
        return None, ("clarification_required",
                       "Which task or reminder did you mean? Tell me its title.", [])
    matches = _mo_find_tasks(user_id, desc)
    if not matches:
        return None, ("error",
                       f"I couldn’t find a task or reminder called “{desc}”. "
                       "Check your Plan and try again.", [])
    if len(matches) > 1:
        matches = _mo_narrow_by_discriminators(
            matches, date_field="due_date", time_field="due_date",
            target_date=target_date, target_time=target_time, context_date=context_date,
        )
    if len(matches) > 1:
        return None, ("clarification_required",
                       f"I found {len(matches)} tasks matching “{desc}” — "
                       "could you be more specific (add a date or more of the title)?", [])
    return matches[0], None


def _mo_closest_event_title(user_id: str, desc: str) -> Optional[str]:
    """A substring search found nothing for `desc` — before giving up, check
    for ONE close-spelling candidate among this user's own (non-cancelled)
    event titles (e.g. "FYB Presentation" typed/misheard for "FYP
    Presentation"). Returns that title only when exactly one candidate clears
    the similarity bar; returns None on zero or multiple candidates so the
    caller never has to guess between several plausible events — it only
    ever offers a review, never silently picks."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT DISTINCT title FROM schedule_events WHERE user_id = ? AND status != 'cancelled'",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    titles = [r["title"] for r in rows if r["title"]]
    if not titles:
        return None
    close = difflib.get_close_matches(desc, titles, n=2, cutoff=0.6)
    return close[0] if len(close) == 1 else None


def _mo_resolve_event_target(user_id: str, target_description, *,
                             target_date=None, target_time=None, context_date=None):
    desc = (target_description or "").strip()
    if not desc:
        return None, ("clarification_required",
                       "Which event did you mean? Tell me its title.", [])
    matches = _mo_find_events(user_id, desc)
    if not matches:
        candidate = _mo_closest_event_title(user_id, desc)
        if candidate:
            return None, ("clarification_required",
                           f"I couldn’t find an event called “{desc}”. "
                           f"Did you mean “{candidate}”?", [])
        return None, ("error",
                       f"I couldn’t find an event called “{desc}”. "
                       "Check your Plan and try again.", [])
    if len(matches) > 1:
        matches = _mo_narrow_by_discriminators(
            matches, date_field="start_time", time_field="start_time",
            target_date=target_date, target_time=target_time, context_date=context_date,
        )
    if len(matches) > 1:
        return None, ("clarification_required",
                       f"I found {len(matches)} events matching “{desc}” — "
                       "could you be more specific (add a date or more of the title)?", [])
    return matches[0], None


# Product Completion Batch 05: a bare clock time ("move X to 7pm") with no
# date named by the user must resolve against the RESOLVED TARGET EVENT'S OWN
# current date — never against "today". The model is never given the event's
# real date (target resolution is deterministic, server-side, DB-backed), so
# it cannot reliably compute "that day at 7pm" itself; instead it is asked to
# return the bare time only (new_start_time_only) whenever the user named no
# date, and THIS function combines it deterministically with the already-
# resolved target's real date. Mirrors this file's existing "hand the model
# concrete dates instead of making it do date arithmetic" precedent (see
# AIService.interpret's upcoming_days_line).
_BARE_TIME_FORMATS = ("%H:%M:%S", "%H:%M", "%I:%M %p", "%I:%M%p", "%I %p", "%I%p")


def _mo_combine_bare_time_with_date(existing_start_iso: Optional[str], time_str: str) -> Optional[str]:
    """Combine a bare clock time with the calendar date of `existing_start_iso`
    (kept in the SAME tzinfo, i.e. MYT). Returns None if either can't be
    parsed — callers must treat that as "couldn't understand", never guess."""
    if not existing_start_iso:
        return None
    existing = sched._parse(existing_start_iso)
    if existing is None:
        return None
    s = (time_str or "").strip().upper()
    if not s:
        return None
    parsed_time = None
    for fmt in _BARE_TIME_FORMATS:
        try:
            parsed_time = datetime.datetime.strptime(s, fmt).time()
            break
        except ValueError:
            continue
    if parsed_time is None:
        return None
    return datetime.datetime.combine(existing.date(), parsed_time, tzinfo=existing.tzinfo).isoformat()


def _fmt_when(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    return dt.strftime("%a %d %b, %I:%M %p")


def _mo_event_overlaps(existing_events: list[dict], start_iso: str, end_iso: str) -> Optional[dict]:
    """First real existing event (non-cancelled) overlapping [start_iso,
    end_iso), or None. `existing_events` are the OCCURRENCE dicts _load_schedule
    returns (keys "start"/"end", not the raw DB "start_time"/"end_time").
    Reuses sched.intervals_overlap — the SAME overlap rule the rest of the app
    already uses; no new conflict logic invented here."""
    for ev in existing_events:
        es, ee = ev.get("start"), ev.get("end")
        if not es or not ee:
            continue
        if sched.intervals_overlap(start_iso, end_iso, es, ee):
            return ev
    return None


def _mo_find_duplicate_event(existing_events: list[dict], title: str, start_iso: str) -> Optional[dict]:
    """An existing event with the SAME normalized title starting within 15
    minutes of `start_iso` — checked for every CREATE_EVENT (single, batch, or
    plan_day) so an item the user re-describes or re-lists (already on their
    real schedule) is recognized instead of quietly duplicated."""
    try:
        target = datetime.datetime.fromisoformat(start_iso)
    except (TypeError, ValueError):
        return None
    norm_title = (title or "").strip().lower()
    if not norm_title:
        return None
    for ev in existing_events:
        if (ev.get("title") or "").strip().lower() != norm_title:
            continue
        es = ev.get("start")
        try:
            es_dt = datetime.datetime.fromisoformat(es) if es else None
        except (TypeError, ValueError):
            es_dt = None
        if es_dt is not None and abs((es_dt - target).total_seconds()) <= 15 * 60:
            return ev
    return None


def _mo_find_duplicate_task(user_id: str, title: str, when_iso: Optional[str]) -> Optional[dict]:
    """The task/task-shaped-reminder counterpart to _mo_find_duplicate_event:
    an existing, not-yet-completed task with the SAME normalized title — and,
    when the new item has a resolved due/reminder/planned time, one whose own
    time is within 15 minutes of it; when the new item has NO time, one that
    is likewise undated (a plain to-do). Checked for every CREATE_TASK /
    CREATE_REMINDER so a re-described task is recognized instead of quietly
    duplicated."""
    norm_title = (title or "").strip().lower()
    if not norm_title:
        return None
    conn = get_db()
    try:
        rows = conn.execute(
            f"SELECT {_TASK_COLS} FROM tasks WHERE user_id = ? AND is_completed = 0",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    candidates = [dict(r) for r in rows if (r["title"] or "").strip().lower() == norm_title]
    if not candidates:
        return None
    if not when_iso:
        for t in candidates:
            if not (t.get("due_date") or t.get("reminder_at") or t.get("planned_at")):
                return t
        return None
    try:
        target = datetime.datetime.fromisoformat(when_iso)
    except (TypeError, ValueError):
        return None
    for t in candidates:
        for k in ("due_date", "reminder_at", "planned_at"):
            v = t.get(k)
            if not v:
                continue
            try:
                v_dt = datetime.datetime.fromisoformat(v)
            except (TypeError, ValueError):
                continue
            if abs((v_dt - target).total_seconds()) <= 15 * 60:
                return t
    return None


def _mo_find_alt_slot(existing_events: list[dict], start_iso: str, end_iso: str):
    """A SMALL, bounded, deterministic search for a nearby free slot for a SOFT
    constraint that conflicts — never a generic solver. Tries the same
    duration shifted by a few fixed offsets, same calendar day only; the first
    candidate with no overlap wins. Returns (new_start_iso, new_end_iso) or None."""
    try:
        s = datetime.datetime.fromisoformat(start_iso)
        e = datetime.datetime.fromisoformat(end_iso)
    except (TypeError, ValueError):
        return None
    duration = e - s
    day = s.date()
    for delta_min in (30, 60, 90, -30, -60):
        cand_s = s + datetime.timedelta(minutes=delta_min)
        if cand_s.date() != day:
            continue
        cand_e = cand_s + duration
        if _mo_event_overlaps(existing_events, cand_s.isoformat(), cand_e.isoformat()) is None:
            return cand_s.isoformat(), cand_e.isoformat()
    return None


def _mo_extract_date_hint(item: dict) -> Optional[str]:
    """The calendar date (YYYY-MM-DD) implied by ONE resolved operation
    payload, or None. Looks at the same fields a real create/update always
    carries — never invents or guesses one."""
    if not isinstance(item, dict):
        return None
    for k in ("start_time", "due_date", "planned_at", "reminder_at", "target_date"):
        v = item.get(k)
        if isinstance(v, str) and len(v) >= 10 and v[4:5] == "-":
            return v[:10]
        fields = item.get("fields")
        if isinstance(fields, dict):
            v2 = fields.get(k)
            if isinstance(v2, str) and len(v2) >= 10 and v2[4:5] == "-":
                return v2[:10]
    return None


def _mo_context_date_from_proposal(action_type: str, payload: dict) -> Optional[str]:
    """Same date-hint extraction as _mo_extract_date_hint, but shaped for a
    just-built proposal spec: a batch's real, ISO-dated per-item payloads live
    under payload["ops"] (payload["items"] is human-formatted display text,
    never machine-parseable — never scan that for a date); a single-item
    proposal's own payload is the candidate directly."""
    if action_type == "batch_operations":
        for op in (payload.get("ops") or []):
            if isinstance(op, dict):
                d = _mo_extract_date_hint(op.get("payload") or {})
                if d:
                    return d
        return None
    return _mo_extract_date_hint(payload)


_MO_ENTITY_TYPE_BY_ACTION = {
    "create_event": "event", "update_event": "event", "delete_event": "event",
    "create_task": "task", "update_task": "task", "delete_task": "task",
}


def _mo_context_entity_from_result(action_type: Optional[str], display_item: Optional[dict]) -> tuple:
    """(entity_type, entity_title) implied by ONE resolved single-item
    operation, or (None, None). Only event/task actions carry a meaningful
    "entity" for pronoun follow-ups — a transaction is never referred to as
    "it" the same way, so it is deliberately excluded."""
    etype = _MO_ENTITY_TYPE_BY_ACTION.get(action_type or "")
    title = (display_item or {}).get("title") if etype else None
    return (etype, title) if title else (None, None)


def _mo_remember_context(user_id: str, *, date_iso: Optional[str] = None,
                         entity_type: Optional[str] = None, entity_title: Optional[str] = None) -> None:
    """Persist the short-term conversational hint: exactly one row per user,
    upserted in place — bounded, cannot grow. Each of date/entity is updated
    ONLY when a new value is actually resolved THIS turn; an unrelated turn in
    between (e.g. a spending question) never erases what "it" still refers
    to — the whole row still expires together after 10 minutes regardless
    (see _mo_recent_context). Called after EVERY understood turn that
    resolves one of these, whether a pure read ("let's look at Monday") or a
    proposal ("move gym earlier"), so the NEXT turn can recover it even when
    this turn created no mo_proposals row at all. A HINT ONLY."""
    if not date_iso and not entity_title:
        return
    try:
        with db_layer.transaction() as conn:
            existing = conn.execute(
                "SELECT context_date, entity_type, entity_title FROM mo_context WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            # mo_context.context_date is NOT NULL (0018); an entity resolved
            # with no date of its own (e.g. a reminder whose own date field
            # wasn't captured) still needs a value — today is a safe, neutral
            # default, never a guess about the entity's real date.
            final_date = date_iso or (existing["context_date"] if existing else None) or _mo_now()[:10]
            final_etype = entity_type or (existing["entity_type"] if existing else None)
            final_etitle = entity_title or (existing["entity_title"] if existing else None)
            conn.execute(
                "INSERT INTO mo_context (user_id, context_date, entity_type, entity_title, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "context_date = excluded.context_date, entity_type = excluded.entity_type, "
                "entity_title = excluded.entity_title, updated_at = excluded.updated_at",
                (user_id, final_date, final_etype, final_etitle, _mo_now()),
            )
    except Exception as e:
        # A hint write is never allowed to break the turn that produced it.
        print(f"DB_ERROR (mo_context write): {str(e)}")


def _mo_recent_context(user_id: str) -> dict:
    """Smallest safe short-term conversational context: what the user most
    recently discussed or acted on, within the last 10 minutes — a date hint
    (context_date) and/or a named entity (entity_type/entity_title, e.g. the
    "Gym" event just moved), or all None. user_id-scoped (no cross-user
    leakage), bounded to one row per user (no long-term profiling / no
    embeddings / no transcript). A HINT ONLY: every target resolver still
    requires a real, structural DB match before acting — this never decides
    WHICH row gets mutated, only helps the model fill in an omitted name/date."""
    empty = {"date": None, "entity_type": None, "entity_title": None}
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT context_date, entity_type, entity_title, updated_at FROM mo_context WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return empty
    try:
        updated = datetime.datetime.fromisoformat(row["updated_at"])
        now = datetime.datetime.fromisoformat(_mo_now())
        if (now - updated) > datetime.timedelta(minutes=10):
            return empty
    except (TypeError, ValueError):
        return empty
    return {"date": row["context_date"], "entity_type": row["entity_type"], "entity_title": row["entity_title"]}


def _mo_recent_context_date(user_id: str) -> Optional[str]:
    return _mo_recent_context(user_id)["date"]


def _mo_join_items(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " or " + items[-1]


def _mo_unsupported_note(items: list[str]) -> str:
    """A short, honest suffix naming what Mo recognized but can't act on.
    Empty string when there is nothing to name — final correctness patch: a
    mixed supported+unsupported request must never leave the unsupported part
    unmentioned, no matter which response shape it ends up attached to."""
    if not items:
        return ""
    return f" I can’t help with {_mo_join_items(items)} — that’s outside what I can do here."


def _mo_natural_clause(r: dict) -> str:
    """A short natural-language clause describing ONE resolved batch item —
    e.g. "move “Gym” to 6:30 PM", "add “Dinner” at 8:00 PM" — built only from
    the already-validated payload, never invented. Used to compose the
    conversational lead sentence of a batch proposal (Section 4/16 —
    "MO INTERACTION + ACTION UX CLOSURE": say what Mo understood in plain
    language instead of "N changes."). The structured proposal card
    (mo.build_display) stays the exact source of truth for times/fields."""
    at = r["action_type"]
    label = r["label"]
    payload = r["payload"] or {}
    if at == "create_event":
        t = None if payload.get("all_day") else _fmt_time_only(payload.get("start_time"))
        return f"add “{label}”" + (f" at {t}" if t else "")
    if at == "create_task":
        when_iso = payload.get("reminder_at") or payload.get("due_date")
        t = _fmt_time_only(when_iso) if when_iso else None
        return f"add “{label}”" + (f" for {t}" if t else "")
    if at == "create_transaction":
        return f"log the {label} expense"
    if at == "update_event":
        f = payload.get("fields") or {}
        if "start_time" in f:
            return f"move “{label}” to {_fmt_time_only(f['start_time'])}"
        return f"update “{label}”"
    if at == "update_task":
        return f"update “{label}”"
    if at in ("delete_event", "delete_task"):
        return f"remove “{label}”"
    if at == "schedule_task_block":
        return f"block time for “{label}”"
    if at == "complete_task":
        return f"mark “{label}” complete"
    return f"update “{label}”"


def _mo_join_and(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


_NATURAL_VERB = {"add": "add", "change": "move", "delete": "remove"}


def _mo_natural_summary(ok_items: list[dict]) -> str:
    """The concise, conversational lead sentence for a batch proposal — "I can
    move 'Gym' to 6:30 and add 'Dinner' at 8." — NEVER "N changes." (Section
    4/16). A run of consecutive items with the SAME (op, title) collapses into
    one count-based clause ("add 10 'Gym Session' entries") so a large uniform
    batch never repeats an identical clause N times, mirroring the same
    grouping _mo_batch_effect already uses for the structured "effect" line."""
    groups: list[list] = []  # [(op, title), count, representative r]
    for r in ok_items:
        key = (r["display_item"]["op"], r["display_item"]["title"])
        if groups and groups[-1][0] == key:
            groups[-1][1] += 1
        else:
            groups.append([key, 1, r])
    clauses = []
    for (op, title), count, r in groups:
        if count == 1:
            clauses.append(_mo_natural_clause(r))
        else:
            clauses.append(f"{_NATURAL_VERB.get(op, 'update')} {count} “{title}” entries")
    return "I can " + _mo_join_and(clauses) + "."


def _mo_batch_effect(ok_items: list[dict]) -> str:
    """A deterministic, prospective, non-redundant "what will happen" line for
    a batch proposal — built entirely from resolved data, never the model's
    own free-form narrative, so it can never accidentally read as a completed
    mutation (Confirm/Cancel is always still pending) and never balloons into
    N repeated identical lines for a large uniform batch (e.g. 10 "Gym
    Session" adds collapse to one line instead of ten)."""
    verb_word = {"add": "Add", "change": "Change", "delete": "Delete"}
    groups: list[list] = []
    for r in ok_items:
        d = r["display_item"]
        verb = verb_word.get(d["op"], "Update")
        title = d["title"]
        if groups and groups[-1][0] == verb and groups[-1][1] == title:
            groups[-1][2] += 1
        else:
            groups.append([verb, title, 1])
    parts = []
    for verb, title, count in groups:
        if count == 1:
            parts.append(f"{verb} “{title}”.")
        else:
            parts.append(f"{verb} {count} “{title}” entries.")
    return " ".join(parts)


def _op_ok(action_type: str, payload: dict, label: str, display_item: dict) -> dict:
    return {"outcome": "ok", "action_type": action_type, "payload": payload,
            "label": label, "message": None, "display_item": display_item}


def _op_error(message: str, label: str = "that") -> dict:
    return {"outcome": "error", "action_type": None, "payload": None,
            "label": label, "message": message, "display_item": None}


def _op_clarify(message: str, label: str = "that") -> dict:
    return {"outcome": "clarification", "action_type": None, "payload": None,
            "label": label, "message": message, "display_item": None}


def _mo_valid_date_str(v: Any) -> Optional[str]:
    """A strict YYYY-MM-DD string, or None for anything else (never raises)."""
    if not isinstance(v, str) or len(v) != 10 or v[4:5] != "-" or v[7:8] != "-":
        return None
    try:
        datetime.date.fromisoformat(v)
    except ValueError:
        return None
    return v


_MO_SAFE_ARITH_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def _mo_eval_arithmetic(expr: str) -> Optional[float]:
    """Deterministically evaluate a SIMPLE numeric expression (+-*/%, **,
    parens, decimals, unary +/-) — NEVER trusts the model's own arithmetic
    (Mo smartness closure: "prefer deterministic calculation over trusting
    free-form model math"). No names, no calls, no attributes, no comparisons
    — anything outside plain numeric arithmetic returns None so the caller can
    fall back safely. Not `eval()` — a whitelisted AST walk only."""
    try:
        node = ast.parse(expr, mode="eval").body
    except (SyntaxError, ValueError, TypeError):
        return None

    def _ev(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _MO_SAFE_ARITH_OPS:
            return _MO_SAFE_ARITH_OPS[type(n.op)](_ev(n.left), _ev(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _MO_SAFE_ARITH_OPS:
            return _MO_SAFE_ARITH_OPS[type(n.op)](_ev(n.operand))
        raise ValueError("unsafe expression")

    try:
        result = _ev(node)
    except (ValueError, TypeError, ZeroDivisionError, OverflowError):
        return None
    return result if isinstance(result, (int, float)) else None


def _mo_fmt_number(n: float) -> str:
    if isinstance(n, float) and n.is_integer():
        n = int(n)
    if isinstance(n, int):
        return str(n)
    return f"{n:.4g}"


def _mo_dispatch_general_utility(params: dict) -> str:
    """Bounded DIRECT-answer capability (Mo smartness closure section 5):
    arithmetic/conversions are computed deterministically server-side;
    anything else (a short definition, simple common-sense reasoning) uses
    the model's own concise "answer_text" as-is — there is no deterministic
    way to verify a definition, but there IS for a number, so numbers are
    never left to the model alone."""
    params = params or {}
    kind = params.get("kind")
    if kind == "arithmetic":
        expr = params.get("expression")
        val = _mo_eval_arithmetic(expr) if isinstance(expr, str) else None
        if val is not None:
            suffix = params.get("unit_suffix") or ""
            return f"{_mo_fmt_number(val)}{suffix}"
    elif kind == "time_diff":
        f, t = params.get("from_time"), params.get("to_time")
        try:
            f_dt, t_dt = datetime.datetime.fromisoformat(f), datetime.datetime.fromisoformat(t)
            total_min = int(round((t_dt - f_dt).total_seconds() / 60))
            if total_min < 0:
                total_min += 24 * 60
            h, m = divmod(total_min, 60)
            parts = [p for p in (f"{h} hour{'s' if h != 1 else ''}" if h else "",
                                 f"{m} minute{'s' if m != 1 else ''}" if m else "") if p]
            return " ".join(parts) or "0 minutes"
        except (TypeError, ValueError):
            pass
    answer = params.get("answer_text")
    if answer and str(answer).strip():
        return str(answer).strip()[:500]
    return "I couldn’t work that out — try rephrasing."


def _fmt_time_only(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    h = dt.hour % 12 or 12
    return f"{h}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def _mo_free_windows(events: list[dict], day_start: str, day_end: str) -> list[tuple[str, str]]:
    """Deterministic gap-finder: the free [start,end) intervals inside
    [day_start, day_end) not covered by any event, after merging overlapping/
    adjacent events. No solver, no invented preferences — pure interval math
    on the real schedule, the same primitive spirit as sched.intervals_overlap."""
    busy = sorted(
        ([e["start"], e["end"]] for e in events if e.get("start") and e.get("end")),
        key=lambda p: p[0],
    )
    merged: list[list[str]] = []
    for s, e in busy:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    gaps: list[tuple[str, str]] = []
    cursor = day_start
    for s, e in merged:
        if s > cursor:
            gaps.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < day_end:
        gaps.append((cursor, day_end))
    return gaps


def _mo_suggest_free_time(user_id: str, target_date: str, purpose: Optional[str],
                          duration_minutes: Optional[int]) -> str:
    """"When should I study today?" — real judgment from the real schedule:
    the earliest open block big enough for `duration_minutes` (default 30),
    with the next real commitment named for context. Never invents a habit or
    preference; if nothing fits, says so plainly.

    Search window: documented deterministic default of 07:00–23:00 (waking
    hours) on `target_date`, further clipped to "not before now" when
    target_date is today — never a suggestion in the middle of the night, and
    never a guess about the user's own routine beyond this fixed default."""
    today = sched.myt_now().date().isoformat()
    f_iso, t_iso = sched.myt_day_bounds(target_date)
    day_start = datetime.datetime.fromisoformat(f_iso).replace(hour=7, minute=0, second=0, microsecond=0).isoformat()
    day_end = datetime.datetime.fromisoformat(f_iso).replace(hour=23, minute=0, second=0, microsecond=0).isoformat()
    lower_bound = max(day_start, sched.myt_now().isoformat()) if target_date == today else day_start
    upper_bound = min(t_iso, day_end)
    # Load the FULL calendar day (f_iso..t_iso) so a late event just outside
    # the waking-hours window is still known about; only the SEARCH range for
    # a candidate free block is clipped to waking hours.
    data = _load_schedule(user_id, f_iso, t_iso)
    evs = sorted(
        [e for e in data["events"] if e.get("status") != "cancelled" and e.get("start") and e.get("end")],
        key=lambda e: e["start"],
    )
    need = duration_minutes if isinstance(duration_minutes, (int, float)) and duration_minutes > 0 else 30
    for s, e in _mo_free_windows(evs, lower_bound, upper_bound):
        try:
            mins = (datetime.datetime.fromisoformat(e) - datetime.datetime.fromisoformat(s)).total_seconds() / 60
        except (TypeError, ValueError):
            continue
        if mins < need:
            continue
        next_ev = next((ev for ev in evs if ev["start"] >= e), None)
        tail = f" {next_ev.get('title') or 'Something'} starts at {_fmt_time_only(next_ev['start'])}." if next_ev else ""
        return f"Between {_fmt_time_only(s)} and {_fmt_time_only(e)} works best. You’re free then.{tail}"
    label = f" for {purpose}" if purpose else ""
    return f"Your day{label} looks fully booked — no open block of at least {need} min."


def _mo_dispatch_read(intent, user_id: str, params: Optional[dict] = None):
    if intent == IntentEnum.GET_SPENDING_SUMMARY:
        return ("answer", AppServices.get_spending_summary(user_id, get_iso_now()).message, ["open_wallet"])
    if intent == IntentEnum.GET_SCHEDULE_SUMMARY:
        target_date = _mo_valid_date_str((params or {}).get("target_date"))
        outcome = AppServices.get_schedule_summary(user_id, date=target_date)
        # Context fidelity P0 fix: a schedule-summary answer's own resolved
        # date (e.g. "tomorrow") travels into any mutating suggestion it
        # generates, so the suggestion targets the day just discussed rather
        # than defaulting to today when clicked later.
        mutating = _mo_schedule_suggestions(user_id, target_date=target_date) if outcome.status == "SUCCESS" else []
        extra: dict = {"nav": ["open_plan"], "mutating": mutating}
        # Final correctness patch: a DISCUSSED day ("let's look at Monday") is
        # remembered as short-term context even though a read never creates a
        # proposal row — see _mo_remember_context_date / mo_context.
        if target_date:
            extra["context_date"] = target_date
        return ("answer", outcome.message, extra)
    if intent == IntentEnum.GET_TODAY_SUMMARY:
        outcome = AppServices.get_today_summary(user_id)
        return ("answer", outcome.message, {"nav": ["open_today"], "mutating": []})
    if intent == IntentEnum.GENERAL_UTILITY:
        return ("answer", _mo_dispatch_general_utility(params or {}), [])
    if intent == IntentEnum.SUGGEST_FREE_TIME:
        p = params or {}
        target_date = _mo_valid_date_str(p.get("target_date")) or sched.myt_now().date().isoformat()
        duration = p.get("duration_minutes")
        duration = duration if isinstance(duration, (int, float)) else None
        msg = _mo_suggest_free_time(user_id, target_date, p.get("purpose"), duration)
        extra: dict = {"nav": ["open_plan"], "mutating": []}
        extra["context_date"] = target_date
        return ("answer", msg, extra)
    return ("error", "I didn’t quite catch that — try rephrasing what you’d like to do.", [])


def _mo_resolve_single_op(op: MoOperation, user_id: str, *, mode: str,
                          schedule_cache: dict, context_date: Optional[str],
                          shadow_removed: Optional[dict] = None,
                          shadow_added: Optional[dict] = None) -> dict:
    """Deterministically resolve + validate ONE MoOperation. Reuses the EXACT
    same validation / target-resolution calls the single-operation path has
    always used — an isolated single item behaves identically to before; this
    is only a reusable wrapper so multi_operation/plan_day can call it once
    per item and combine the results into one batch proposal.

    ``shadow_removed``/``shadow_added`` (both ``{date_key -> ...}``, optional)
    let CREATE_EVENT's conflict/duplicate check see the PROPOSED post-change
    schedule for this same batch — event ids already resolved for deletion or
    a time-move elsewhere in the batch, and occurrences already proposed by an
    earlier item in the batch — rather than only the current real DB state.
    See _mo_understand, which builds these before resolving CREATE_EVENT items
    and attaches "shadow_remove"/"shadow_add" to DELETE_EVENT/UPDATE_EVENT/
    CREATE_EVENT results so it can maintain them across the whole batch."""
    intent = op.intent
    params = _mo_strip_identity(op.parameters)

    if intent == IntentEnum.CREATE_TRANSACTION:
        try:
            payload = _validate_transaction_fields(
                amount=params.get("amount"), type_=params.get("type"),
                category=params.get("category"), description=params.get("description"),
                date=params.get("date"),
            )
        except HTTPException as e:
            return _op_error(str(e.detail) or "I couldn’t read that expense — try another way.")
        label = payload.get("description") or payload.get("category") or "transaction"
        return _op_ok("create_transaction", payload, label,
                      {"op": "add", "title": label, "when": _fmt_when(payload.get("date"))})

    if intent in (IntentEnum.CREATE_TASK, IntentEnum.CREATE_REMINDER):
        try:
            tp = TaskParams(**params)
        except Exception as e:
            # Phase 5H-B6 (log privacy): a pydantic ValidationError stringifies
            # with the offending input_value, which here can be AI-echoed
            # user text. Log the class, not the value.
            print(f"VALIDATION_ERROR: {type(e).__name__}")
            return _op_error("I couldn’t read that task — try saying it another way.")
        payload = tp.model_dump()
        payload["is_reminder"] = intent == IntentEnum.CREATE_REMINDER or bool(tp.reminder_at)
        label = tp.title
        when_iso = tp.due_date or tp.reminder_at or tp.planned_at
        dup_task = _mo_find_duplicate_task(user_id, label, when_iso)
        if dup_task is not None:
            dup_when = _fmt_when(dup_task.get("due_date") or dup_task.get("reminder_at")
                                 or dup_task.get("planned_at"))
            return {"outcome": "duplicate", "action_type": None, "payload": None, "label": label,
                    "message": f"“{label}” is already on your list"
                               + (f" for {dup_when}" if dup_when else "") + " — I left it as is.",
                    "display_item": None}
        when = _fmt_when(when_iso) or "No date"
        return _op_ok("create_task", payload, label, {"op": "add", "title": label, "when": when})

    if intent == IntentEnum.CREATE_EVENT:
        try:
            payload = _validate_event_fields(
                user_id, require_start=True,
                title=params.get("title"), start=params.get("start"),
                end=params.get("end"), all_day=params.get("all_day"),
                location=params.get("location"),
            )
        except HTTPException as e:
            return _op_error(str(e.detail) or "I couldn’t read that event — try another way.")
        label = payload.get("title") or "event"
        if payload.get("all_day") or not payload.get("start_time"):
            when = _fmt_when(payload.get("start_time"))
            return _op_ok("create_event", payload, label,
                          {"op": "add", "title": label, "when": (when + " (all day)") if when else ""})

        date_key = payload["start_time"][:10]
        day_data = schedule_cache.get(date_key)
        if day_data is None:
            f_iso, t_iso = sched.myt_day_bounds(date_key)
            day_data = _load_schedule(user_id, f_iso, t_iso)
            schedule_cache[date_key] = day_data
        existing_events = [e for e in day_data["events"] if e.get("status") != "cancelled"]
        # Batch post-change reasoning (Section 2/3): reflect what THIS SAME
        # batch has already resolved before this item — an event elsewhere in
        # the batch that's being deleted or moved away from this date/time no
        # longer occupies it, and an event this batch already proposed to
        # create does. Real DB state is never touched; this is a display-only
        # overlay for validating the rest of the batch. See _mo_understand.
        removed_ids = (shadow_removed or {}).get(date_key)
        if removed_ids:
            existing_events = [e for e in existing_events if e.get("id") not in removed_ids]
        existing_events = existing_events + ((shadow_added or {}).get(date_key) or [])

        # Checked for every CREATE_EVENT — single, batch or plan_day — never
        # only plan_day: a re-listed/re-described item that's already on the
        # real schedule is recognized instead of quietly duplicated.
        dup = _mo_find_duplicate_event(existing_events, label, payload["start_time"])
        if dup is not None:
            return {"outcome": "duplicate", "action_type": None, "payload": None, "label": label,
                    "message": f"“{label}” is already on your schedule at "
                               f"{_fmt_when(dup.get('start_time'))} — I left it as is.",
                    "display_item": None}

        end_for_check = payload.get("end_time") or payload["start_time"]
        conflict = _mo_event_overlaps(existing_events, payload["start_time"], end_for_check)
        if conflict is not None:
            constraint = op.constraint if op.constraint in ("hard", "soft") else "hard"
            if constraint == "soft" and op.condition != "if_free":
                alt = _mo_find_alt_slot(existing_events, payload["start_time"], end_for_check)
                if alt is not None:
                    new_start, new_end = alt
                    moved = dict(payload)
                    moved["start_time"], moved["end_time"] = new_start, new_end
                    r = _op_ok("create_event", moved, label,
                              {"op": "add", "title": label, "when": _fmt_when(new_start),
                               "old_when": _fmt_when(payload["start_time"]) + " (moved — conflicted)"})
                    r["shadow_add"] = (new_start[:10], {"start": new_start, "end": new_end, "title": label})
                    return r
            return {"outcome": "conflict", "action_type": None, "payload": None, "label": label,
                    "message": f"“{label}” at {_fmt_when(payload['start_time'])} overlaps with "
                               f"“{conflict.get('title')}” from {_fmt_when(conflict.get('start'))} "
                               f"to {_fmt_when(conflict.get('end'))}.",
                    "display_item": None}

        r = _op_ok("create_event", payload, label,
                   {"op": "add", "title": label, "when": _fmt_when(payload["start_time"])})
        r["shadow_add"] = (date_key, {"start": payload["start_time"],
                                       "end": payload.get("end_time") or payload["start_time"], "title": label})
        return r

    if intent in (IntentEnum.UPDATE_TASK, IntentEnum.DELETE_TASK):
        target, err = _mo_resolve_task_target(
            user_id, params.get("target_description"),
            target_date=params.get("target_date"), target_time=params.get("target_time"),
            context_date=context_date,
        )
        if err is not None:
            kind, msg, _extra = err
            outcome = "clarification" if kind == "clarification_required" else "error"
            return {"outcome": outcome, "action_type": None, "payload": None,
                    "label": params.get("target_description") or "that task", "message": msg, "display_item": None}
        if intent == IntentEnum.DELETE_TASK:
            payload = {"task_id": target["id"], "task_title": target.get("title")}
            when = _fmt_when(target.get("due_date") or target.get("planned_at"))
            return _op_ok("delete_task", payload, target.get("title") or "task",
                          {"op": "delete", "title": target.get("title"), "when": when})
        supplied = {}
        if params.get("new_title") is not None:
            supplied["title"] = params.get("new_title")
        if "new_due_date" in params:
            supplied["due_date"] = params.get("new_due_date")
        if "new_reminder_at" in params:
            supplied["reminder_at"] = params.get("new_reminder_at")
        if "new_planned_at" in params:
            supplied["planned_at"] = params.get("new_planned_at")
        if not supplied:
            return _op_clarify(f"What would you like to change about “{target.get('title')}”?", target.get("title"))
        try:
            fields = _task_update_columns(target, supplied)
        except HTTPException as e:
            return _op_error(str(e.detail) or "I couldn’t read that change — try saying it another way.",
                             target.get("title"))
        if not fields:
            return _op_clarify(f"What would you like to change about “{target.get('title')}”?", target.get("title"))
        payload = {"task_id": target["id"], "task_title": target.get("title"), "fields": fields}
        old_when = _fmt_when(target.get("due_date") or target.get("planned_at"))
        new_when = (_fmt_when(fields.get("due_date") or fields.get("planned_at"))
                    if ("due_date" in fields or "planned_at" in fields) else None)
        return _op_ok("update_task", payload, target.get("title"),
                      {"op": "change", "title": fields.get("title") or target.get("title"),
                       "when": new_when or old_when,
                       "old_when": old_when if (new_when and new_when != old_when) else None})

    if intent in (IntentEnum.UPDATE_EVENT, IntentEnum.DELETE_EVENT):
        target, err = _mo_resolve_event_target(
            user_id, params.get("target_description"),
            target_date=params.get("target_date"), target_time=params.get("target_time"),
            context_date=context_date,
        )
        if err is not None:
            kind, msg, _extra = err
            outcome = "clarification" if kind == "clarification_required" else "error"
            return {"outcome": outcome, "action_type": None, "payload": None,
                    "label": params.get("target_description") or "that event", "message": msg, "display_item": None}
        if intent == IntentEnum.DELETE_EVENT:
            payload = {"event_id": target["id"], "event_title": target.get("title")}
            r = _op_ok("delete_event", payload, target.get("title") or "event",
                      {"op": "delete", "title": target.get("title"), "when": _fmt_when(target.get("start_time"))})
            old_date = (target.get("start_time") or "")[:10]
            if old_date:
                r["shadow_remove"] = (old_date, target["id"])
            return r
        supplied = {}
        if params.get("new_title") is not None:
            supplied["title"] = params.get("new_title")
        if params.get("new_start") is not None:
            # An explicit new date (with or without a time) — the user's own
            # words named it, so it wins outright over any bare-time hint.
            supplied["start"] = params.get("new_start")
        elif params.get("new_start_time_only") is not None:
            # Batch 05 fix: a bare clock time with NO new date named must
            # preserve the RESOLVED TARGET EVENT'S OWN current calendar date —
            # never "today". Combined deterministically here, server-side,
            # never guessed by the model (see _mo_combine_bare_time_with_date).
            combined = _mo_combine_bare_time_with_date(
                target.get("start_time"), params.get("new_start_time_only")
            )
            if combined is None:
                return _op_error(
                    f"I couldn’t understand that time for “{target.get('title')}” — "
                    "try saying it like '7pm' or '19:00'.",
                    target.get("title"),
                )
            supplied["start"] = combined
        if "new_end" in params:
            supplied["end"] = params.get("new_end")
        if params.get("new_location") is not None:
            supplied["location"] = params.get("new_location")
        if not supplied:
            return _op_clarify(f"What would you like to change about “{target.get('title')}”?", target.get("title"))
        try:
            fields = _event_update_columns(user_id, target, supplied)
        except HTTPException as e:
            return _op_error(str(e.detail) or "I couldn’t read that change — try saying it another way.",
                             target.get("title"))
        if not fields:
            return _op_clarify(f"What would you like to change about “{target.get('title')}”?", target.get("title"))
        if "start_time" in fields:
            # Proposal-fidelity P0 fix: an explicit "move X to <time>" is a
            # HARD user intent — Mo must detect a conflict at the EXACT
            # requested time BEFORE ever proposing it, and never silently
            # substitute a different time (no alt-slot auto-nudge here, unlike
            # a soft PLAN_DAY create — the user named an exact time). Reuses
            # the SAME deterministic overlap check + shadow-aware
            # existing_events construction CREATE_EVENT already uses;
            # self-excluded by event id so the event is never "in conflict"
            # with its own current slot.
            new_start = fields["start_time"]
            new_end = fields.get("end_time") or target.get("end_time") or new_start
            date_key = new_start[:10]
            day_data = schedule_cache.get(date_key)
            if day_data is None:
                f_iso, t_iso = sched.myt_day_bounds(date_key)
                day_data = _load_schedule(user_id, f_iso, t_iso)
                schedule_cache[date_key] = day_data
            existing_events = [e for e in day_data["events"]
                              if e.get("status") != "cancelled" and e.get("id") != target["id"]]
            removed_ids = (shadow_removed or {}).get(date_key)
            if removed_ids:
                existing_events = [e for e in existing_events if e.get("id") not in removed_ids]
            existing_events = existing_events + ((shadow_added or {}).get(date_key) or [])
            conflict = _mo_event_overlaps(existing_events, new_start, new_end)
            if conflict is not None:
                alt = _mo_find_alt_slot(existing_events, new_start, new_end)
                alt_note = (f" {_fmt_time_only(alt[0])} is free instead — say so if you’d like that time,"
                           f" or ask me to move “{conflict.get('title')}”.") if alt else ""
                return {"outcome": "conflict", "action_type": None, "payload": None,
                        "label": target.get("title") or "that event",
                        "message": f"Moving “{target.get('title')}” to {_fmt_when(new_start)} would overlap "
                                   f"“{conflict.get('title')}” from {_fmt_when(conflict.get('start'))} "
                                   f"to {_fmt_when(conflict.get('end'))}.{alt_note}",
                        "display_item": None}

        payload = {"event_id": target["id"], "event_title": target.get("title"), "fields": fields}
        old_when = _fmt_when(target.get("start_time"))
        new_when = _fmt_when(fields.get("start_time")) if "start_time" in fields else None
        r = _op_ok("update_event", payload, target.get("title"),
                  {"op": "change", "title": fields.get("title") or target.get("title"),
                   "when": new_when or old_when,
                   "old_when": old_when if (new_when and new_when != old_when) else None})
        if "start_time" in fields:
            # Moving the event: the batch's shadow must vacate the OLD slot and
            # occupy the NEW one so a sibling CREATE_EVENT in the same batch is
            # validated against the proposed post-change schedule (Section 3).
            old_date = (target.get("start_time") or "")[:10]
            new_start = fields.get("start_time")
            new_date = (new_start or "")[:10]
            if old_date:
                r["shadow_remove"] = (old_date, target["id"])
            if new_date:
                new_end = fields.get("end_time") or target.get("end_time") or new_start
                r["shadow_add"] = (new_date, {"start": new_start, "end": new_end,
                                              "title": fields.get("title") or target.get("title")})
        return r

    if intent in (IntentEnum.GET_SPENDING_SUMMARY, IntentEnum.GET_SCHEDULE_SUMMARY, IntentEnum.GET_TODAY_SUMMARY):
        _kind, message, _extra = _mo_dispatch_read(intent, user_id)
        return {"outcome": "read", "action_type": None, "payload": None,
                "label": "that", "message": message, "display_item": None}

    return _op_error("I didn’t quite catch part of that — try rephrasing what you’d like to do.")


def _mo_understand(interp: MoInterpretation, user_id: str):
    """MoInterpretation -> ("<kind>", message, extra). NEVER mutates domain
    data. mode "read"/"clarification"/"unsupported" behave exactly as the old
    single-intent path did (same messages, same suggestions). mode
    "single_operation" with exactly one item produces the IDENTICAL proposal
    shape (action_type/payload/display) the old per-intent branches always
    built — full backward compatibility whenever a message requests exactly
    one thing. mode "multi_operation"/"plan_day" combine every cleanly
    resolved item into ONE "batch_operations" proposal and NEVER silently
    drop an item that didn't resolve — it is named in the returned message."""
    mode = interp.mode
    unsupported_note = _mo_unsupported_note(interp.unsupported_items)

    if mode == "clarification":
        return ("clarification_required",
                (interp.clarification_question or "Could you give me a little more detail?")
                + unsupported_note, [])
    if mode == "unsupported":
        # Phase — Mo capability wording: this copy must describe Mo's ACTUAL
        # bounded capability (individual items, with confirmation), never
        # broad/plural phrasing like "add, change or remove a task" that a
        # user can reasonably read as covering bulk/mass mutation too (e.g.
        # after Mo correctly refuses "delete all my tasks and events" via the
        # BULK / WHOLE-RANGE CLEAR REQUESTS branch above, which also lands
        # here with unsupported_items naming the bulk request).
        base = ("I can help create, update, or remove individual tasks, reminders, "
                "events, and supported expense entries. Changes that affect your "
                "data require confirmation.")
        if interp.unsupported_items:
            base = (f"I can’t help with {_mo_join_items(interp.unsupported_items)}. "
                    "I can help create, update, or remove individual tasks, reminders, "
                    "events, and supported expense entries. Changes that affect your "
                    "data require confirmation.")
        return ("unsupported", base, mo.default_suggestions("unsupported"))
    if mode == "read":
        op = interp.operations[0] if interp.operations else None
        if op is None:
            return ("error", "I didn’t quite catch that — try rephrasing what you’d like to do.", [])
        kind, message, extra = _mo_dispatch_read(op.intent, user_id, op.parameters)
        if kind == "answer":
            message = (message or "") + unsupported_note
        return (kind, message, extra)
    if mode not in ("single_operation", "multi_operation", "plan_day"):
        return ("error", "I didn’t quite catch that — try rephrasing what you’d like to do.", [])
    if not interp.operations:
        return ("clarification_required",
                (interp.clarification_question or "Could you give me a little more detail?")
                + unsupported_note, [])

    context_date = _mo_recent_context_date(user_id)
    schedule_cache: dict = {}
    # Batch post-change reasoning (Section 2/3 — "MO INTERACTION + ACTION UX
    # CLOSURE"): a multi-operation request must be validated against the
    # PROPOSED future schedule, not only today's real one. Resolve every
    # DELETE_EVENT / UPDATE_EVENT first — target resolution is a pure DB
    # lookup by title, independent of any other item in the batch — and use
    # their outcomes to build a small in-memory shadow (vacated slots +
    # moved-to slots). Every remaining item (including CREATE_EVENT) is then
    # resolved against that shadow, in original request order, so e.g. moving
    # Gym away from 8 PM in the same batch actually frees 8 PM for a new
    # Dinner. Nothing here touches the real DB — see _mo_resolve_single_op.
    shadow_removed: dict[str, set] = {}
    shadow_added: dict[str, list] = {}

    def _apply_shadow(r: dict) -> None:
        rem = r.get("shadow_remove")
        if rem:
            d, eid = rem
            shadow_removed.setdefault(d, set()).add(eid)
        add = r.get("shadow_add")
        if add:
            d, entry = add
            shadow_added.setdefault(d, []).append(entry)

    schedule_affecting = {IntentEnum.DELETE_EVENT, IntentEnum.UPDATE_EVENT}
    results: list = [None] * len(interp.operations)
    for i, op in enumerate(interp.operations):
        if op.intent in schedule_affecting:
            r = _mo_resolve_single_op(op, user_id, mode=mode, schedule_cache=schedule_cache,
                                      context_date=context_date,
                                      shadow_removed=shadow_removed, shadow_added=shadow_added)
            results[i] = r
            if r["outcome"] == "ok":
                _apply_shadow(r)
    for i, op in enumerate(interp.operations):
        if results[i] is not None:
            continue
        r = _mo_resolve_single_op(op, user_id, mode=mode, schedule_cache=schedule_cache,
                                  context_date=context_date,
                                  shadow_removed=shadow_removed, shadow_added=shadow_added)
        results[i] = r
        if r["outcome"] == "ok":
            _apply_shadow(r)

    # Exactly one item -> the SAME shape a single-op request has always used,
    # regardless of which mode the model happened to report — UNLESS a soft
    # constraint was nudged to a different time than requested (old_when set):
    # that case falls through to the batch presentation below, because only
    # the batch display shows the old-time -> new-time comparison. Section 10
    # requires an explained move to always be visible, never silent, even for
    # a single-item plan_day.
    if len(results) == 1 and not (results[0]["outcome"] == "ok"
                                  and results[0]["display_item"].get("old_when")):
        r = results[0]
        if r["outcome"] == "ok":
            display = mo.build_display(r["action_type"], r["payload"])
            if unsupported_note:
                display["note"] = unsupported_note.strip()
            return ("proposal_required", None, {
                "action_type": r["action_type"], "payload": r["payload"],
                "summary": mo.build_summary(r["action_type"]),
                "effect": mo.build_effect(r["action_type"], r["payload"]),
                "display": display,
            })
        if r["outcome"] == "read":
            return ("answer", (r["message"] or "") + unsupported_note, [])
        if r["outcome"] == "duplicate":
            # already exists — informational, not an error; nothing to confirm.
            return ("answer", (r["message"] or "") + unsupported_note, [])
        if r["outcome"] == "clarification":
            return ("clarification_required",
                    (r["message"] or "Could you give me a little more detail?") + unsupported_note, [])
        return ("error",
                (r["message"] or "I didn’t quite catch that — try rephrasing what you’d like to do.")
                + unsupported_note, [])

    # Two or more items.
    ok_items = [r for r in results if r["outcome"] == "ok"]
    read_items = [r for r in results if r["outcome"] == "read"]
    excluded = [r for r in results if r["outcome"] not in ("ok", "read")]
    read_messages = [r["message"] for r in read_items if r["message"]]

    if not ok_items:
        if read_messages and not excluded:
            return ("answer", " ".join(read_messages) + unsupported_note, [])
        if excluded and all(r["outcome"] == "duplicate" for r in excluded):
            # Every item already exists — concise and informational, never an
            # error, and every one is still named (never just the first).
            if len(excluded) == 1:
                note = excluded[0]["message"]
            else:
                titles = ", ".join(f"“{r['label']}”" for r in excluded)
                note = f"{titles} are already on your schedule — nothing new to add."
            if read_messages:
                note = " ".join(read_messages) + " " + note
            return ("answer", note + unsupported_note, [])
        any_clarify = any(r["outcome"] == "clarification" for r in excluded)
        # EVERY excluded item must be named — never only the first (a
        # multi-item request that hits several conflicts/duplicates/errors
        # must surface all of them, not silently drop all but one).
        note = " ".join(r["message"] or f"I couldn’t include “{r['label']}”." for r in excluded) if excluded else (
            interp.clarification_question or "Could you give me a little more detail?"
        )
        if read_messages:
            note = " ".join(read_messages) + " " + note
        return ("clarification_required" if any_clarify else "error", note + unsupported_note, [])

    items = [r["display_item"] for r in ok_items]
    # The EXECUTABLE half of the batch — each ok item's real action_type +
    # validated payload, stored alongside the human-facing display items so
    # Confirm can actually run each one through its existing canonical path
    # (see _mo_exec_batch). "items" (display) and "ops" (executable) stay in
    # the same order so a confirm-time failure can be reported against the
    # same human-readable label.
    ops = [{"action_type": r["action_type"], "payload": r["payload"]} for r in ok_items]
    n = len(items)
    # The model's free-form "narrative" is kept in the payload for reference
    # only — it is NEVER used to build pre-confirmation copy. A model-written
    # sentence like "I have scheduled your classes..." reads as an already-done
    # mutation, but nothing is committed until Confirm; "effect"/"summary" must
    # stay deterministic and tense-safe (see the real-device correction: Mo
    # must never imply a mutation happened before Confirm). Reused verbatim
    # (via "Done — " + lowerFirst) as the post-confirmation message, so it also
    # has to read correctly in both places — see _mo_batch_effect.
    narrative = (interp.narrative or "").strip()
    # A concise, conversational lead sentence — "I can move 'Gym' to 6:30 and
    # add 'Dinner' at 8." — NEVER "N changes." (Section 4/16). Every item this
    # batch resolved is named in plain language; the exact structured detail
    # (old time -> new time, etc.) lives only in the proposal card itself.
    summary = _mo_natural_summary(ok_items)
    effect = _mo_batch_effect(ok_items)

    # Anything that could NOT be included stays a SEPARATE, secondary note —
    # never appended into the primary summary sentence (Section 5: a warning
    # like "I couldn't find a dentist appointment" is a small secondary line,
    # not mixed into the lead message or the proposal mechanics).
    note_parts = list(read_messages)
    dup_excluded = [r for r in excluded if r["outcome"] == "duplicate"]
    other_excluded = [r for r in excluded if r["outcome"] != "duplicate"]
    # Several duplicates collapse into ONE concise mention rather than one
    # repeated sentence per item — everything else keeps its own reason
    # (a conflict or a clarification needs its own detail to be useful).
    if len(dup_excluded) == 1:
        note_parts.append(dup_excluded[0]["message"])
    elif len(dup_excluded) > 1:
        titles = ", ".join(f"“{r['label']}”" for r in dup_excluded)
        note_parts.append(f"{titles} were already on your schedule, so I left those as is.")
    for r in other_excluded:
        note_parts.append(r["message"] or f"I couldn’t include “{r['label']}”.")
    note = " ".join(note_parts + ([unsupported_note.strip()] if unsupported_note else []))

    payload = {"items": items, "ops": ops, "narrative": narrative}
    display = mo.build_display("batch_operations", payload)
    if note:
        display["note"] = note
    return ("proposal_required", None, {
        "action_type": "batch_operations",
        "payload": payload,
        "summary": summary,
        "effect": effect,
        "display": display,
    })


# Confirmation vs clarification (Section 9/10 — "MO INTERACTION + ACTION UX
# CLOSURE"): a typed "yes"/"no" is honored ONLY as a fallback for an already
# fully-specified pending proposal, and ONLY when the user has exactly one —
# never as a free-standing conversational trigger. This is a closed, exact
# vocabulary match (never a substring/fuzzy match) so an ordinary sentence
# that happens to start with "no" or "ok" is never misread as a confirmation.
_MO_AFFIRM_PHRASES = {
    "yes", "yes please", "yeah", "yep", "yup", "sure", "ok", "okay",
    "go ahead", "do it", "confirm", "proceed", "sounds good", "please do",
}
_MO_NEGATE_PHRASES = {
    "no", "nope", "nah", "cancel", "cancel that", "cancel it",
    "never mind", "nevermind", "don't", "dont", "stop",
}


def _mo_yes_no(text: str) -> Optional[str]:
    t = (text or "").strip().lower().rstrip(".!")
    if t in _MO_AFFIRM_PHRASES:
        return "yes"
    if t in _MO_NEGATE_PHRASES:
        return "no"
    return None


def _mo_proposal_response(row: dict) -> dict:
    p = mo.public_proposal(row)
    # Section 5: the proposal card's own "summary" is the concise conversational
    # lead sentence ONLY — any secondary note (an excluded/not-found item, a
    # capability caveat) lives separately at proposal.display.note for the UI
    # to render with lower visual weight. The top-level "message" field is kept
    # as the SUM of both (unchanged contract for any caller that only reads
    # "message", e.g. a notification or a non-conversational surface).
    note = (p.get("display") or {}).get("note")
    message = f"{p['summary']} {note}" if note else p["summary"]
    return {
        "kind": "proposal_required",
        "proposal": p,
        "message": message,
        "suggested_actions": mo.resolve_suggestions(
            mo.default_suggestions("proposal_required", row["action_type"])
        ),
    }


# --- Product Completion Batch 03: Mo voice input --------------------------- #
# Voice is ONLY an input method. This endpoint does exactly one thing: turn a
# short recorded clip into plain text. It never touches mo_proposals,
# mo_context, or any domain table — the returned transcript is handed back to
# the browser, which places it in the SAME editable text input the existing
# composer already uses, and the user still explicitly presses Send, which
# still goes through the unchanged /api/process-input path below. No second
# Mo pipeline exists.
MO_TRANSCRIBE_ALLOWED_MIME_TYPES = (
    "audio/webm",
    "audio/ogg",
    "audio/mp4",
    "audio/aac",
    "audio/mpeg",
    "audio/wav",
    "audio/x-wav",
)
# A base64 string is ~4/3 the size of the decoded bytes; padding this out
# generously so a legitimate MO_TRANSCRIBE_MAX_AUDIO_BYTES-sized clip is never
# rejected by the (cheap, pre-decode) string-length check alone.
_MO_TRANSCRIBE_MAX_B64_CHARS = int(sec.MO_TRANSCRIBE_MAX_AUDIO_BYTES * 4 / 3) + 1024


class TranscribeRequest(BaseModel):
    audio_base64: str
    mime_type: str


@app.post("/api/mo/transcribe")
def transcribe_voice_input(req: TranscribeRequest, current: dict = Depends(_rl("mo_transcribe"))):
    # Match on the BASE type only (before any ";codecs=..." parameter), never
    # the full raw string. iOS/WebKit's real recorder.mimeType / Blob.type for
    # an audio/mp4 recording is reported inconsistently across iOS versions —
    # sometimes bare "audio/mp4", sometimes with a codecs parameter in a
    # different spacing/quoting than any single hardcoded string (e.g.
    # `audio/mp4; codecs="mp4a.40.2"` with a space and quotes, not the compact
    # `audio/mp4;codecs=mp4a.40.2`). An exact-string allowlist that included
    # specific codec parameters rejected these with a 422, which the client
    # then showed as the same generic "Couldn't transcribe that" message as
    # every other failure — indistinguishable from a real provider error.
    mime = (req.mime_type or "").strip().lower()
    base_mime = mime.split(";", 1)[0].strip()
    if base_mime not in MO_TRANSCRIBE_ALLOWED_MIME_TYPES:
        print(f"TRANSCRIBE_REJECTED_MIME: {mime!r}")
        raise HTTPException(status_code=422, detail="Unsupported audio type.")

    b64 = req.audio_base64 or ""
    if not b64.strip():
        raise HTTPException(status_code=422, detail="No audio was received.")
    if len(b64) > _MO_TRANSCRIBE_MAX_B64_CHARS:
        raise HTTPException(status_code=413, detail="That recording is too long. Please keep it shorter.")

    try:
        audio_bytes = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=422, detail="Couldn't read that audio. Please try recording again.")

    if not audio_bytes:
        raise HTTPException(status_code=422, detail="No audio was received.")
    if len(audio_bytes) > sec.MO_TRANSCRIBE_MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="That recording is too long. Please keep it shorter.")

    if not ai_client:
        raise HTTPException(status_code=503, detail="Voice input isn’t set up on this server yet.")

    # In-memory only — audio_bytes is never written to disk, never inserted
    # into SQLite, never added to mo_context. It goes out of scope (and is
    # eligible for GC) the moment this function returns.
    ext = base_mime.split("/")[-1] or "webm"
    print(f"TRANSCRIBE_REQUEST: mime={base_mime!r} bytes={len(audio_bytes)}")
    try:
        response = ai_client.audio.transcriptions.create(
            file=(f"voice.{ext}", audio_bytes, base_mime),
            model=MO_TRANSCRIBE_MODEL,
            response_format="json",
        )
    except Exception as e:
        # Never leak a raw provider/SDK error (may carry account/key details)
        # to the client — log server-side only, return a safe generic 502.
        print(f"TRANSCRIBE_PROVIDER_ERROR: {type(e).__name__}: {e}")
        raise HTTPException(status_code=502, detail="Couldn't transcribe that. Please try again.")

    text = getattr(response, "text", None)
    if not isinstance(text, str):
        text = ""
    print(f"TRANSCRIBE_RESULT: chars={len(text.strip())}")
    return {"transcript": text.strip()}


@app.post("/api/process-input")
def process_natural_language(req: UserInput, current: dict = Depends(_rl("ai"))):
    user_id = current["id"]

    # 1. bound the input BEFORE the paid model (unchanged).
    if len(req.user_input or "") > sec.AI_INPUT_MAX_CHARS:
        raise HTTPException(status_code=422,
                            detail="That message is too long. Please shorten it and try again.")

    # 1b. Confirmation vs clarification (Section 9/10): a bare "yes"/"no" is
    # NEVER sent to the paid model — it can only ever mean "confirm/cancel the
    # one thing I'm already waiting on you to decide", and only when exactly
    # one such thing exists. No active proposal (zero, or more than one, which
    # never happens in today's one-proposal-at-a-time model but is handled
    # safely regardless) -> answer honestly and mutate nothing.
    yes_no = _mo_yes_no(req.user_input)
    if yes_no is not None:
        conn = get_db()
        try:
            actives = conn.execute(
                "SELECT * FROM mo_proposals WHERE user_id=? AND status='pending'",
                (user_id,),
            ).fetchall()
        finally:
            conn.close()
        if len(actives) == 1:
            row = dict(actives[0])
            if yes_no == "yes":
                return _mo_execute(row, user_id)
            with db_layer.transaction() as conn2:
                conn2.execute(
                    "UPDATE mo_proposals SET status='cancelled' WHERE id=? AND user_id=? AND status='pending'",
                    (row["id"], user_id),
                )
            return {"kind": "cancelled", "proposal": mo.public_proposal(_mo_get_owned(row["id"], user_id))}
        no_pending_msg = ("There's nothing pending for me to confirm right now."
                          if yes_no == "yes" else "There's nothing pending to cancel right now.")
        return {"kind": "answer", "message": no_pending_msg, "retryable": False,
                "suggested_actions": [], "mutating_suggestions": []}

    try:
        token = idem.normalize_token(req.client_token)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    # Proposal-dedup fingerprint = the SUBMISSION TEXT (request identity), never
    # the AI output — the same rule the old Omni-bar create-once used.
    rhash = idem.request_fingerprint({"omni_input": req.user_input or ""}) if token else None

    # 2. read-only fast path: a reused token that already has a proposal.
    if token:
        conn = get_db()
        try:
            existing = conn.execute(
                "SELECT * FROM mo_proposals WHERE user_id=? AND client_token=?",
                (user_id, token),
            ).fetchone()
        finally:
            conn.close()
        if existing is not None:
            existing = _mo_expire_if_due(dict(existing))
            if existing["client_request_hash"] and existing["client_request_hash"] != rhash:
                raise idem.IdempotencyConflict()          # token reused, different text
            if existing["status"] in _MO_ACTIVE:
                return _mo_proposal_response(existing)     # same pending proposal, no model call
            if existing["status"] == "executed":
                out = _mo_proposal_response(existing)
                out["proposal"]["exec_message"] = existing.get("exec_message")
                return out
            # cancelled / expired / failed -> fall through: re-understand and
            # re-use this SAME row (keeps one row per user+token).

    # 3. understand (the paid model runs here). recent_ctx is a weak,
    # short-term, user-scoped hint (see _mo_recent_context) — the model may
    # use it to fill in a date, or a bare "it"/"that" reference, the user
    # didn't restate; it never decides WHICH row gets mutated, only the
    # deterministic resolver does that.
    recent_ctx = _mo_recent_context(user_id)
    # Who Am I (Product Completion Batch 02): the user's explicitly SAVED
    # interests, injected as a soft personalization signal only — see
    # _mo_interests_context_line. Empty for a user who saved none; Mo's
    # behavior is unchanged in that case.
    user_interests = _get_user_interests(user_id)
    try:
        interp = AIService.interpret(req.user_input, context_date=recent_ctx["date"],
                                     context_entity=recent_ctx, user_interests=user_interests)
    except idem.IdempotencyConflict:
        raise
    except ValueError as e:
        if str(e) == "API_KEY_MISSING":
            return {"kind": "error", "message": "Mo isn’t set up on this server yet.",
                    "retryable": False, "suggested_actions": []}
        return {"kind": "error", "message": "I had trouble understanding that. Please try again.",
                "retryable": True, "suggested_actions": []}
    except Exception as e:
        print(f"UNHANDLED_EXCEPTION: {type(e).__name__}")
        return {"kind": "error", "message": "Something went wrong. Please try again.",
                "retryable": True, "suggested_actions": []}

    kind, message, extra = _mo_understand(interp, user_id)

    # Remember the date/entity THIS turn resolved (read or proposal alike) so
    # the NEXT turn can recover it even when this one created no mo_proposals
    # row — see _mo_remember_context. A TRUE multi-item batch (2+ ops) never
    # sets "entity" — after discussing several items at once, a bare "it" is
    # ambiguous. A single-item result DOES set it even when its shape is
    # "batch_operations" (a lone time-changing UPDATE_TASK/UPDATE_EVENT always
    # takes that shape so the old->new comparison can be shown — see the
    # single-item collapse rule above — but it is still exactly one entity).
    if kind == "proposal_required":
        date_hint = _mo_context_date_from_proposal(extra["action_type"], extra["payload"])
        etype, etitle = (None, None)
        if extra["action_type"] != "batch_operations":
            etype, etitle = _mo_context_entity_from_result(extra["action_type"], extra.get("display"))
        else:
            batch_ops = extra["payload"].get("ops") or []
            batch_items = extra["payload"].get("items") or []
            if len(batch_ops) == 1 and len(batch_items) == 1:
                etype, etitle = _mo_context_entity_from_result(
                    batch_ops[0].get("action_type"), {"title": batch_items[0].get("title")})
        _mo_remember_context(user_id, date_iso=date_hint, entity_type=etype, entity_title=etitle)
    elif kind == "clarification_required":
        # The CLARIFICATION prompt contract returns EMPTY operations[] by
        # design (see the system prompt's "CLARIFICATION" section) — so even
        # when the model's own question names the record correctly (e.g.
        # "When should I move the FYP Presentation to?"), nothing structured
        # survives to the next turn, and a bare reply like "11:30 AM" then
        # reads as a brand-new, targetless request. Recover the ALREADY-NAMED
        # record from THIS turn's own user text (never a guess: only an exact
        # title substring, and only remembered when exactly one of the
        # user's own events/tasks matches) so the next turn's context_entity
        # hint can resolve a bare follow-up the way it already does for "it".
        etype, etitle = _mo_entity_named_in_text(user_id, req.user_input)
        date_hint = extra.get("context_date") if isinstance(extra, dict) else None
        _mo_remember_context(user_id, date_iso=date_hint, entity_type=etype, entity_title=etitle)
    elif isinstance(extra, dict):
        _mo_remember_context(user_id, date_iso=extra.get("context_date"))

    if kind != "proposal_required":
        nav = extra.get("nav", []) if isinstance(extra, dict) else (extra if isinstance(extra, list) else [])
        mutating = extra.get("mutating", []) if isinstance(extra, dict) else []
        return {"kind": kind, "message": message, "retryable": kind == "error",
                "suggested_actions": mo.resolve_suggestions(nav),
                "mutating_suggestions": mutating}

    return _mo_upsert_proposal(user_id, extra, token, rhash)


def _mo_upsert_proposal(user_id: str, spec: dict, token: Optional[str], rhash: Optional[str]) -> dict:
    """Create — or, for a reused (user_id, client_token), refresh in place — the
    server-owned pending proposal row for `spec` and return its public response.
    Shared by POST /api/process-input and POST /api/process-input/suggested/{token}
    so a click on a mutating suggestion collapses to exactly one proposal via the
    same UNIQUE(user_id, client_token) index (rule: repeated clicks -> one row)."""
    now = _mo_now()
    pid = "prop_" + uuid.uuid4().hex
    exec_token = token or ("prop:" + pid)
    expires_at = (datetime.datetime.fromisoformat(now)
                  + datetime.timedelta(seconds=mo.PROPOSAL_TTL_SECONDS)).isoformat()
    payload_json = json.dumps(spec["payload"], default=str, sort_keys=True)
    display_json = json.dumps(spec["display"], default=str)

    with db_layer.transaction() as conn:
        reused = conn.execute(
            "SELECT id, status FROM mo_proposals WHERE user_id=? AND client_token=?",
            (user_id, token),
        ).fetchone() if token else None
        if reused is not None:
            pid = reused["id"]
            # A still-actionable proposal for this token is returned untouched so a
            # double-click never rebuilds it; a resolved one is re-opened in place.
            if reused["status"] in _MO_ACTIVE:
                return _mo_proposal_response(_mo_get_owned(pid, user_id))
            exec_token = "prop:" + pid
            conn.execute(
                """UPDATE mo_proposals SET status='pending', action_type=?, result_kind=NULL,
                       payload_json=?, summary=?, effect=?, display_json=?, client_request_hash=?,
                       exec_token=?, result_ref=NULL, exec_message=NULL, failure_reason=NULL,
                       created_at=?, expires_at=?, confirmed_at=NULL
                   WHERE id=? AND user_id=?""",
                (spec["action_type"], payload_json, spec["summary"], spec["effect"], display_json,
                 rhash, exec_token, now, expires_at, pid, user_id),
            )
        else:
            try:
                conn.execute(
                    """INSERT INTO mo_proposals
                       (id, user_id, status, action_type, payload_json, summary, effect,
                        display_json, client_token, client_request_hash, exec_token,
                        created_at, expires_at)
                       VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (pid, user_id, spec["action_type"], payload_json, spec["summary"],
                     spec["effect"], display_json, token, rhash, exec_token, now, expires_at),
                )
            except sqlite3.IntegrityError:
                other = conn.execute(
                    "SELECT * FROM mo_proposals WHERE user_id=? AND client_token=?",
                    (user_id, token),
                ).fetchone()
                if other is not None:
                    return _mo_proposal_response(dict(other))
                raise

    return _mo_proposal_response(_mo_get_owned(pid, user_id))


# ========================================================================== #
# B8 — Mo mutating SUGGESTED ACTIONS
#
# After a real GET_SCHEDULE_SUMMARY answer, Mo may surface a tiny, curated set of
# contextual mutating recommendations, each carried to the browser ONLY as an
# opaque HMAC-signed token (mo.sign_suggestion). Selecting one calls
#   POST /api/process-input/suggested/{token}
# which — after re-deriving everything from live owner data — builds a normal B2
# proposal. Nothing mutates until the user Confirms, through the SAME canonical
# task services the manual Plan UI uses (_task_update_columns / _exec_task_update
# for a work block; _set_task_completion for a completion). The client never
# sends a user_id, a task_id, a final time, an action type, or a payload.
# ========================================================================== #
_MO_SUGG_DAYTIME_ANCHOR_HOUR = 9      # unscheduled tasks default to the daytime band
_MO_SUGG_MAX_PROBE = 8               # cap how many unscheduled tasks we slot-probe


def _mo_block_candidate(user_id: str, task_row: dict, minutes: int,
                        preferred_date: Optional[str] = None):
    """One B7.1-quality candidate window for an as-yet-unscheduled task, or None.
    Same reasonable-hours + due-date cap + collision policy as a B7 task move
    (avoids one-off events, recurring event occurrences and other task windows).

    ``preferred_date`` (context fidelity P0 fix, YYYY-MM-DD, optional): when
    the task has no ANCHOR of its own (no existing planned_at), the default
    anchor's DATE is this day instead of today — so a "Block 45 min for X"
    suggestion generated while discussing a specific day (e.g. "tomorrow")
    actually prefers a slot on THAT day, not whichever day happens to be
    current when the candidate is computed. ``propose_task_slots`` still
    searches the same real-time-anchored horizon and never overlaps existing
    commitments; this only shifts which in-range slot it ranks first (closest
    to the anchor) — it can still fall back to another day if none is free on
    the preferred one. A task that already has its OWN planned_at is never
    overridden by this — that remains the strongest anchor."""
    try:
        dur = datetime.timedelta(minutes=int(minutes))
    except (TypeError, ValueError):
        return None
    if dur <= datetime.timedelta(0):
        return None
    now = _te_now()
    anchor_raw = task_row.get("planned_at")
    anchor = sched._parse(anchor_raw) if anchor_raw else None
    if anchor is None:
        anchor_date = _mo_valid_date_str(preferred_date)
        if anchor_date:
            anchor = datetime.datetime.combine(
                datetime.date.fromisoformat(anchor_date),
                datetime.time(hour=_MO_SUGG_DAYTIME_ANCHOR_HOUR),
                tzinfo=sched.MYT,
            )
        else:
            anchor = now.astimezone(sched.MYT).replace(
                hour=_MO_SUGG_DAYTIME_ANCHOR_HOUR, minute=0, second=0, microsecond=0)
    conn = get_db()
    try:
        busy = _te_busy_intervals(conn, user_id, task_row["id"])
    finally:
        conn.close()
    cands = tec.propose_task_slots(
        duration=dur, busy_intervals=busy,
        now_utc=now, horizon_end_utc=now + datetime.timedelta(days=_TE_HORIZON_DAYS),
        original_start=anchor, due_end=_te_due_end(task_row), max_slots=1,
    )
    return cands[0] if cands else None


def _mo_schedule_suggestions(user_id: str, target_date: Optional[str] = None) -> list[dict]:
    """0-2 allow-listed mutating suggestions for a schedule-summary answer.
    Deterministic, read-only, and empty when nothing is safely applicable — a
    mutation is NEVER fabricated just to give an answer an action.

    ``target_date`` (context fidelity P0 fix): the day this answer was ABOUT
    (e.g. "tomorrow" resolved to a concrete date) — passed straight through to
    the candidate-slot search AND embedded in the suggestion's own signed
    token (see mo.sign_suggestion), so a "Block 45 min for X" offered while
    discussing that day still targets it at click time, not whichever day is
    current when the user actually clicks."""
    now_iso = _mo_now()
    now_dt = _te_now()
    conn = get_db()
    try:
        rows = [dict(r) for r in conn.execute(
            f"SELECT {_TASK_COLS} FROM tasks WHERE user_id = ? "
            f"AND (is_completed IS NULL OR is_completed = 0)",
            (user_id,),
        ).fetchall()]
    finally:
        conn.close()
    if not rows:
        return []

    def _has_title(r):
        return bool((r.get("title") or "").strip())

    def _due(r):
        return _te_due_end(r)

    far = datetime.datetime.max.replace(tzinfo=sched.MYT)

    # A (primary): block time for an unscheduled, identifiable task that can still
    # get a valid B7.1 slot before its deadline. Nearest upcoming deadline first.
    blockable = sorted(
        (r for r in rows if _has_title(r) and tec.task_window(r) is None),
        key=lambda r: (_due(r) or far, r.get("created_at") or "", r["id"]),
    )
    # B (secondary): mark complete — ONLY a genuinely overdue item, so Mo never
    # nudges "complete" on something that is not even due yet.
    overdue = sorted(
        (r for r in rows if _has_title(r) and _due(r) is not None and _due(r) < now_dt),
        key=lambda r: (_due(r), r.get("created_at") or "", r["id"]),
    )

    out: list[dict] = []
    used: set[str] = set()

    for r in blockable[:_MO_SUGG_MAX_PROBE]:
        cand = _mo_block_candidate(user_id, r, mo.TASK_BLOCK_DEFAULT_MINUTES, preferred_date=target_date)
        if cand is None:
            continue
        tok, _n = mo.sign_suggestion(
            kind="schedule_task_block", user_id=user_id, task_id=r["id"],
            now_iso=now_iso, secret=sec.SESSION_SECRET,
            block_minutes=mo.TASK_BLOCK_DEFAULT_MINUTES,
            target_date=target_date,
        )
        out.append({
            "key": f"mut:schedule_task_block:{r['id']}",
            "kind": "schedule_task_block",
            "label": f"Block {mo.TASK_BLOCK_DEFAULT_MINUTES} min for {r['title']}",
            "token": tok,
            "preview": {"title": r["title"], "start": cand["start"],
                        "end": cand["end"], "summary": cand["summary"]},
        })
        used.add(r["id"])
        break

    for r in overdue:
        if r["id"] in used:
            continue
        tok, _n = mo.sign_suggestion(
            kind="complete_task", user_id=user_id, task_id=r["id"],
            now_iso=now_iso, secret=sec.SESSION_SECRET,
        )
        out.append({
            "key": f"mut:complete_task:{r['id']}",
            "kind": "complete_task",
            "label": f"Mark {r['title']} complete",
            "token": tok,
            "preview": {"title": r["title"]},
        })
        used.add(r["id"])
        break

    return out[: mo.MAX_MUTATING_SUGGESTIONS]


def _mo_spec_complete_task(task: dict) -> dict:
    payload = {"task_id": task["id"], "task_title": task.get("title")}
    return {
        "action_type": "complete_task",
        "payload": payload,
        "summary": mo.build_summary("complete_task"),
        "effect": mo.build_effect("complete_task", payload),
        "display": mo.build_display("complete_task", payload),
    }


def _mo_spec_schedule_block(task: dict, cand: dict) -> dict:
    payload = {
        "task_id": task["id"],
        "task_title": task.get("title"),
        "planned_at": cand["start"],
        "scheduled_end": cand["end"],
        "due_date": task.get("due_date"),
    }
    return {
        "action_type": "schedule_task_block",
        "payload": payload,
        "summary": mo.build_summary("schedule_task_block"),
        "effect": mo.build_effect("schedule_task_block", payload),
        "display": mo.build_display("schedule_task_block", payload),
    }


# B8.1 — every stale message: says the suggestion is no longer current, states
# nothing was changed, and NEVER implies the user cancelled it or that a blind
# retry could work.
_MO_STALE_GENERIC = mo.STALE_GENERIC_MESSAGE
_MO_STALE_TASK_CHANGED = "This suggestion is no longer current because the task changed. Nothing was changed."
_MO_STALE_TASK_GONE = "This suggestion is no longer current — that task no longer exists. Nothing was changed."
_MO_STALE_TASK_DONE = "This suggestion is no longer current — that task is already complete. Nothing was changed."
_MO_STALE_SLOT_TAKEN = "This suggestion is no longer current — that time isn’t free any more. Nothing was changed."
_MO_STALE_PAST_DUE = "This suggestion is no longer current — that time is now past the task’s deadline. Nothing was changed."
_MO_STALE_PAST_NOW = "This suggestion is no longer current — that time has already passed. Nothing was changed."
_MO_STALE_EVENT_GONE = "This suggestion is no longer current — that event no longer exists. Nothing was changed."
# Proposal-fidelity P0 fix (confirm-time revalidation, Section 5): the exact
# proposed move is no longer safe — something else now occupies that time.
# Confirm must NEVER silently pick a different time; it can only answer
# "is the EXACT proposed change still valid?" and, if not, stop honestly.
_MO_STALE_EVENT_CONFLICT = ("This change no longer fits your schedule because something else was "
                            "scheduled there. Review it again.")

# kept for the click endpoint (pre-proposal) 409s — same truthful register.
_MO_SUGG_GONE = _MO_STALE_GENERIC
_MO_SUGG_TASK_CHANGED = _MO_STALE_TASK_CHANGED


class _MoSuggStale(Exception):
    """A mutating-suggestion proposal can no longer be applied truthfully (task
    edited / completed / deleted, slot taken, deadline passed). Carries a safe,
    reason-bearing user message; maps to the terminal `stale` status — never
    `cancelled`, never an execution, never a replacement proposal."""


def _mo_exec_update_task(user_id: str, payload: dict, *, fresh_claim: bool):
    """Target existence is re-checked HERE (not trusted from proposal-build
    time) — the same fresh-claim discipline as _mo_exec_complete_task. Reuses
    the exact canonical column-update path the manual PATCH /api/tasks/{id}
    endpoint uses (_exec_task_update), so an AI-driven edit and a hand-typed
    one go through identical validation and persistence."""
    tid = payload.get("task_id")
    existing = _get_owned_task(user_id, tid) if tid else None
    if existing is None:
        raise _MoSuggStale(_MO_STALE_TASK_GONE)
    fields = payload.get("fields") or {}
    with db_layer.transaction() as conn:
        _exec_task_update(conn, user_id, tid, fields)
    updated = _get_owned_task(user_id, tid)
    title = (updated or {}).get("title") or existing.get("title") or "the task"
    return ActionOutcome(status="SUCCESS", message=f"Updated “{title}”."), tid


def _mo_exec_delete_task(user_id: str, payload: dict, *, fresh_claim: bool):
    tid = payload.get("task_id")
    existing = _get_owned_task(user_id, tid) if tid else None
    if existing is None:
        raise _MoSuggStale(_MO_STALE_TASK_GONE)
    title = existing.get("title") or "the task"
    with db_layer.transaction() as conn:
        conn.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (tid, user_id))
    return ActionOutcome(status="SUCCESS", message=f"Deleted “{title}”."), tid


def _mo_exec_update_event(user_id: str, payload: dict, *, fresh_claim: bool):
    """Same fresh-claim + canonical-path discipline as _mo_exec_update_task,
    reusing _exec_event_update (the manual PATCH /api/events/{id} path).

    Proposal-fidelity P0 fix (Section 5/6): for a time-changing move, Confirm
    re-validates that the EXACT proposed new time is STILL conflict-free right
    before writing it — the schedule may have changed since the proposal was
    built. This only ever answers "is this exact change still safe?"; it can
    never recompute or substitute a different time. If it is no longer safe,
    the proposal goes stale (truthful "review it again", nothing written)
    rather than silently applying anything else. After writing, the persisted
    row is re-read and every field this move actually confirmed is compared
    against what was proposed — a mismatch is reported as a failure, never a
    false "success" (post-write verification)."""
    eid = payload.get("event_id")
    existing = _get_owned_event(user_id, eid) if eid else None
    if existing is None:
        raise _MoSuggStale(_MO_STALE_EVENT_GONE)
    fields = payload.get("fields") or {}
    if "start_time" in fields:
        new_start = fields["start_time"]
        new_end = fields.get("end_time") or existing.get("end_time") or new_start
        f_iso, t_iso = sched.myt_day_bounds(new_start[:10])
        day_data = _load_schedule(user_id, f_iso, t_iso)
        existing_events = [e for e in day_data["events"]
                           if e.get("status") != "cancelled" and e.get("id") != eid]
        if _mo_event_overlaps(existing_events, new_start, new_end) is not None:
            raise _MoSuggStale(_MO_STALE_EVENT_CONFLICT)
    with db_layer.transaction() as conn:
        _exec_event_update(conn, user_id, eid, fields)
    updated = _get_owned_event(user_id, eid)
    if updated is None:
        raise _MoSuggStale(_MO_STALE_EVENT_GONE)
    for col in ("start_time", "end_time", "title", "location"):
        if col in fields and updated.get(col) != fields[col]:
            print(f"MO_EXEC_MISMATCH: update_event field={col}")
            return ActionOutcome(
                status="DATABASE_ERROR",
                message="That didn’t save exactly as shown — check your Plan and try again.",
            ), eid
    title = updated.get("title") or existing.get("title") or "the event"
    return ActionOutcome(status="SUCCESS", message=f"Updated “{title}”."), eid


def _mo_exec_delete_event(user_id: str, payload: dict, *, fresh_claim: bool):
    eid = payload.get("event_id")
    existing = _get_owned_event(user_id, eid) if eid else None
    if existing is None:
        raise _MoSuggStale(_MO_STALE_EVENT_GONE)
    title = existing.get("title") or "the event"
    with db_layer.transaction() as conn:
        conn.execute("DELETE FROM schedule_events WHERE id = ? AND user_id = ?", (eid, user_id))
        # B6 parity with the manual DELETE endpoint: drop any per-occurrence
        # exceptions for a deleted series (their anchor is gone).
        conn.execute(
            "DELETE FROM schedule_occurrence_overrides WHERE user_id = ? AND series_event_id = ?",
            (user_id, eid),
        )
    return ActionOutcome(status="SUCCESS", message=f"Deleted “{title}”."), eid


def _mo_exec_batch(user_id: str, payload: dict, *, fresh_claim: bool):
    """Execute every op in a batch_operations proposal through its EXISTING
    canonical path — the exact same AppServices.create_* / _mo_exec_update_* /
    _mo_exec_delete_* functions a single-item proposal already uses; no new
    business logic. Partial-failure policy (section 22): one op's target
    vanishing or any other error does NOT abort the rest — every op is
    attempted, and the aggregate message names exactly which succeeded and
    which did not; the proposal is marked 'executed' if at least one op
    succeeded, 'failed' (retryable) only if none did. Idempotent by
    construction: the OUTER proposal-status check (in confirm_proposal /
    _mo_execute) already short-circuits a repeat Confirm on an 'executed'
    batch BEFORE this function is ever called again, so no op here can run
    twice."""
    ops = payload.get("ops") or []
    items = payload.get("items") or []
    successes: list[str] = []
    failures: list[str] = []
    stale_errors: list[_MoSuggStale] = []
    result_ref = None

    for i, op in enumerate(ops):
        action_type = op.get("action_type")
        op_payload = op.get("payload") or {}
        label = (items[i].get("title") if i < len(items) else None) or "that item"
        ok = False
        try:
            if action_type == "create_transaction":
                out = AppServices.create_transaction(user_id, op_payload)
                ok = out.status == "SUCCESS"
                if ok and result_ref is None:
                    result_ref = ((out.data or {}).get("transaction") or {}).get("id")
            elif action_type == "create_task":
                tp = TaskParams(**{k: op_payload.get(k) for k in
                                   ("title", "due_date", "reminder_at", "planned_at", "notes")})
                out = AppServices.create_task(user_id, tp, is_reminder=bool(op_payload.get("is_reminder")))
                ok = out.status == "SUCCESS"
                if ok and result_ref is None:
                    result_ref = (out.data or {}).get("id")
            elif action_type == "create_event":
                out = AppServices.create_event(user_id, op_payload)
                ok = out.status == "SUCCESS"
                if ok and result_ref is None:
                    result_ref = (out.data or {}).get("event_id")
            elif action_type == "update_task":
                out, ref = _mo_exec_update_task(user_id, op_payload, fresh_claim=fresh_claim)
                ok = out.status == "SUCCESS"
                if ok and result_ref is None:
                    result_ref = ref
            elif action_type == "delete_task":
                out, ref = _mo_exec_delete_task(user_id, op_payload, fresh_claim=fresh_claim)
                ok = out.status == "SUCCESS"
                if ok and result_ref is None:
                    result_ref = ref
            elif action_type == "update_event":
                out, ref = _mo_exec_update_event(user_id, op_payload, fresh_claim=fresh_claim)
                ok = out.status == "SUCCESS"
                if ok and result_ref is None:
                    result_ref = ref
            elif action_type == "delete_event":
                out, ref = _mo_exec_delete_event(user_id, op_payload, fresh_claim=fresh_claim)
                ok = out.status == "SUCCESS"
                if ok and result_ref is None:
                    result_ref = ref
            else:
                ok = False
        except _MoSuggStale as st:
            ok = False
            stale_errors.append(st)
        except idem.IdempotencyConflict:
            raise
        except Exception as e:
            print(f"MO_BATCH_ITEM_ERROR: {type(e).__name__}")
            ok = False

        (successes if ok else failures).append(label)

    if not successes and ops and len(stale_errors) == len(ops):
        # Every op in this batch is now stale (Section 5) — the common case is
        # a plain single-item "move X to <time>" request, which always takes
        # this batch_operations shape (see the old_when fallthrough in
        # _mo_understand). Propagate as a REAL stale outcome rather than a
        # generic retryable failure: the underlying conflict is real and won't
        # resolve itself on a blind Confirm retry, so the truthful "review it
        # again" state is more honest than "try again in a moment". A batch
        # where at least one op DID succeed keeps the existing partial-success
        # message below — never regressed by this.
        raise stale_errors[0]

    if not successes:
        return ActionOutcome(
            status="DATABASE_ERROR",
            message="Nothing could be saved — " + "; ".join(failures) + ".",
        ), result_ref

    msg = f"Done — {', '.join(successes)}."
    if failures:
        msg += f" Couldn’t save: {', '.join(failures)}."
    return ActionOutcome(status="SUCCESS", message=msg), result_ref


def _mo_exec_complete_task(user_id: str, payload: dict, *, fresh_claim: bool):
    tid = payload.get("task_id")
    task = _get_owned_task(user_id, tid) if tid else None
    if task is None:
        raise _MoSuggStale(_MO_STALE_TASK_GONE)
    title = task.get("title") or "the task"
    if task.get("is_completed"):
        if fresh_claim:
            raise _MoSuggStale(_MO_STALE_TASK_DONE)
        return ActionOutcome(status="SUCCESS", message=f"Marked “{title}” complete."), tid
    res = _set_task_completion(user_id, tid, True)   # SAME canonical path as manual Complete
    title = (res.get("task") or {}).get("title") or title
    return ActionOutcome(status="SUCCESS", message=f"Marked “{title}” complete."), tid


def _mo_exec_schedule_block(user_id: str, payload: dict, *, fresh_claim: bool):
    tid = payload.get("task_id")
    s_iso, e_iso = payload.get("planned_at"), payload.get("scheduled_end")
    try:
        cs = datetime.datetime.fromisoformat(s_iso)
        ce = datetime.datetime.fromisoformat(e_iso)
    except (TypeError, ValueError):
        raise _MoSuggStale(_MO_STALE_GENERIC)
    now = _te_now()
    existing = _get_owned_task(user_id, tid) if tid else None
    if existing is None:
        raise _MoSuggStale(_MO_STALE_TASK_GONE)
    if existing.get("is_completed"):
        raise _MoSuggStale(_MO_STALE_TASK_DONE)
    title = existing.get("title") or "the task"

    cur_win = tec.task_window(existing)
    if cur_win is not None:
        if not fresh_claim and cur_win[0] == cs and cur_win[1] == ce:
            return ActionOutcome(status="SUCCESS", message=f"Blocked time for “{title}”."), tid
        raise _MoSuggStale(_MO_STALE_TASK_CHANGED)
    if cs <= now:
        raise _MoSuggStale(_MO_STALE_PAST_NOW)
    due_end = _te_due_end(existing)
    if due_end is not None and ce > due_end:
        raise _MoSuggStale(_MO_STALE_PAST_DUE)

    with db_layer.transaction() as conn:
        for bs, be in _te_busy_intervals(conn, user_id, tid):
            if sched.intervals_overlap(s_iso, e_iso, bs, be):
                raise _MoSuggStale(_MO_STALE_SLOT_TAKEN)
        # canonical B7 task-update path — planned_at + scheduled_end ONLY.
        fields = _task_update_columns(existing, {"planned_at": s_iso, "scheduled_end": e_iso})
        _exec_task_update(conn, user_id, tid, fields)
    return ActionOutcome(
        status="SUCCESS",
        message=f"Blocked {mo._fmt_duration(s_iso, e_iso) or 'time'} for “{title}”.",
    ), tid


@app.post("/api/process-input/suggested/{token}")
def prepare_suggested_action(token: str, current: dict = Depends(_rl("mo_proposal"))):
    """Turn an opaque, signed mutating suggestion into a reviewable B2 proposal.
    Owner-bound + expiry are enforced by the token; everything else is re-derived
    from live data here. NOTHING in the domain is mutated — only an mo_proposals
    row is written, and a repeat click collapses onto it via (user_id,
    client_token) uniqueness. A stale/no-longer-current condition is a clean 409
    whose body is the truthful "no longer current / nothing changed" message."""
    user_id = current["id"]
    payload = mo.verify_suggestion(token, secret=sec.SESSION_SECRET, now_iso=_mo_now())
    if payload is None or payload.get("u") != user_id:
        raise HTTPException(status_code=409, detail=_MO_STALE_GENERIC)

    kind = payload["k"]
    task = _get_owned_task(user_id, payload["t"])
    if task is None:
        raise HTTPException(status_code=409, detail=_MO_STALE_TASK_GONE)
    if task.get("is_completed"):
        raise HTTPException(status_code=409, detail=_MO_STALE_TASK_DONE)

    if kind == "complete_task":
        spec = _mo_spec_complete_task(task)
    elif kind == "schedule_task_block":
        if tec.task_window(task) is not None:
            raise HTTPException(status_code=409, detail=_MO_STALE_TASK_CHANGED)
        cand = _mo_block_candidate(user_id, task, payload.get("d", mo.TASK_BLOCK_DEFAULT_MINUTES),
                                   preferred_date=payload.get("td"))
        if cand is None:
            raise HTTPException(status_code=409, detail=_MO_STALE_PAST_DUE)
        spec = _mo_spec_schedule_block(task, cand)
    else:  # unreachable — verify_suggestion allow-lists kind
        raise HTTPException(status_code=409, detail=_MO_STALE_GENERIC)

    client_token = "sg_" + payload["n"]
    rhash = idem.request_fingerprint({"mo_suggested": payload["n"], "k": kind, "t": payload["t"]})
    return _mo_upsert_proposal(user_id, spec, client_token, rhash)


def _mo_stale_response(row: Optional[dict], message: Optional[str] = None) -> dict:
    """B8.1 — the single machine-readable shape for a STALE mutating-suggestion
    proposal. Returned by the first stale Confirm AND every repeated Confirm
    (HTTP 200, consistent `kind:"stale"`) so the client never falls through to
    the cancelled path and never treats it as a network error."""
    msg = (message or (row or {}).get("failure_reason") or mo.STALE_GENERIC_MESSAGE)
    return {
        "kind": "stale",
        "proposal": mo.public_proposal(row) if row else None,
        "message": msg,
        "retryable": False,
        "suggested_actions": [],
    }


def _mo_mark_stale(proposal_id: str, user_id: str, message: str) -> Optional[dict]:
    """Transition pending/executing/failed -> terminal `stale`, stamping
    confirmed_at as the 'became stale at' instant (drives the bounded
    STALE_VISIBLE_SECONDS pending window). Never touches an already-terminal
    row. Returns the fresh row."""
    with db_layer.transaction() as conn:
        conn.execute(
            "UPDATE mo_proposals SET status='stale', failure_reason=?, confirmed_at=? "
            "WHERE id=? AND user_id=? AND status IN ('pending','executing','failed')",
            (message[:200], _mo_now(), proposal_id, user_id),
        )
    return _mo_get_owned(proposal_id, user_id)


def _mo_execute(row: dict, user_id: str) -> dict:
    """Drive the canonical service for `row` EXACTLY ONCE. The real guarantee is
    the domain table's (user_id, client_token) uniqueness keyed by the proposal's
    stable `exec_token`; the proposal status is bookkeeping that self-heals from a
    mid-confirm crash (a re-confirm re-drives the service, which replays the
    already-committed row instead of creating a second one)."""
    action_type = row["action_type"]
    payload = json.loads(row["payload_json"])
    exec_token = row["exec_token"]
    fp = idem.request_fingerprint({"mo_exec": exec_token})

    # Claim the execution: pending/failed -> executing. If this updates 0 rows a
    # concurrent op moved the proposal — RE-READ and honour it. A proposal that
    # is now cancelled / expired must NEVER execute (rule 9); one already
    # executed returns its stored result (rule 12).
    with db_layer.transaction() as conn:
        claimed = conn.execute(
            "UPDATE mo_proposals SET status='executing' "
            "WHERE id=? AND user_id=? AND status IN ('pending','failed')",
            (row["id"], user_id),
        ).rowcount
    if not claimed:
        cur = _mo_get_owned(row["id"], user_id) or row
        if cur["status"] == "executed":
            return {"kind": "executed", "proposal": mo.public_proposal(cur),
                    "message": cur.get("exec_message") or f"Done — {cur['effect'][:1].lower()}{cur['effect'][1:]}",
                    "suggested_actions": mo.resolve_suggestions(
                        mo.default_suggestions("proposal_required", cur["action_type"]))}
        if cur["status"] == "stale":
            return _mo_stale_response(cur)
        if cur["status"] in ("cancelled", "expired"):
            raise HTTPException(status_code=409,
                                detail="This was already resolved — nothing was changed.")
        # status == 'executing': another confirm is in flight for the SAME
        # exec_token; fall through and re-drive the canonical service — its
        # (user_id, client_token) uniqueness still yields exactly one row.

    try:
        if action_type == "create_transaction":
            out = AppServices.create_transaction(user_id, payload,
                                                 client_token=exec_token, client_request_hash=fp)
            result_ref = ((out.data or {}).get("transaction") or {}).get("id")
        elif action_type == "create_task":
            tp = TaskParams(**{k: payload.get(k) for k in
                               ("title", "due_date", "reminder_at", "planned_at", "notes")})
            out = AppServices.create_task(user_id, tp, is_reminder=bool(payload.get("is_reminder")),
                                          client_token=exec_token, client_request_hash=fp)
            result_ref = (out.data or {}).get("id")
        elif action_type == "create_event":
            out = AppServices.create_event(user_id, payload,
                                           client_token=exec_token, client_request_hash=fp)
            result_ref = (out.data or {}).get("event_id")
        elif action_type == "schedule_task_block":
            out, result_ref = _mo_exec_schedule_block(user_id, payload, fresh_claim=bool(claimed))
        elif action_type == "complete_task":
            out, result_ref = _mo_exec_complete_task(user_id, payload, fresh_claim=bool(claimed))
        elif action_type == "update_task":
            out, result_ref = _mo_exec_update_task(user_id, payload, fresh_claim=bool(claimed))
        elif action_type == "delete_task":
            out, result_ref = _mo_exec_delete_task(user_id, payload, fresh_claim=bool(claimed))
        elif action_type == "update_event":
            out, result_ref = _mo_exec_update_event(user_id, payload, fresh_claim=bool(claimed))
        elif action_type == "delete_event":
            out, result_ref = _mo_exec_delete_event(user_id, payload, fresh_claim=bool(claimed))
        elif action_type == "batch_operations":
            out, result_ref = _mo_exec_batch(user_id, payload, fresh_claim=bool(claimed))
        else:
            out = ActionOutcome(status="INTERNAL_ERROR", message="Unknown action.")
            result_ref = None
    except idem.IdempotencyConflict:
        raise
    except _MoSuggStale as st:
        # B8.1 — terminal `stale`, NOT `cancelled`. The user never cancelled this.
        fresh = _mo_mark_stale(row["id"], user_id, str(st))
        return _mo_stale_response(fresh, str(st))
    except Exception as e:
        print(f"MO_EXEC_ERROR: {type(e).__name__}")
        out = ActionOutcome(status="DATABASE_ERROR", message="Couldn’t save that just now.")
        result_ref = None

    if out.status == "SUCCESS":
        with db_layer.transaction() as conn:
            conn.execute(
                "UPDATE mo_proposals SET status='executed', confirmed_at=?, result_ref=?, "
                "result_kind=?, exec_message=? WHERE id=? AND user_id=?",
                (_mo_now(), result_ref, mo.result_kind_for(action_type),
                 out.message[:280], row["id"], user_id),
            )
        fresh = _mo_get_owned(row["id"], user_id)
        return {"kind": "executed", "proposal": mo.public_proposal(fresh), "message": out.message,
                "suggested_actions": mo.resolve_suggestions(
                    mo.default_suggestions("proposal_required", action_type))}

    with db_layer.transaction() as conn:
        conn.execute(
            "UPDATE mo_proposals SET status='failed', failure_reason=? "
            "WHERE id=? AND user_id=? AND status <> 'executed'",
            ((out.message or "")[:200], row["id"], user_id),
        )
    fresh = _mo_get_owned(row["id"], user_id)
    return {"kind": "failed", "proposal": mo.public_proposal(fresh), "retryable": True,
            "message": "I couldn’t save that just now. Try Confirm again in a moment.",
            "suggested_actions": []}


@app.post("/api/process-input/{proposal_id}/confirm")
def confirm_proposal(proposal_id: str, current: dict = Depends(_rl("mo_proposal"))):
    user_id = current["id"]
    row = _mo_get_owned(proposal_id, user_id)
    if row is None:
        # unknown id OR another user's id -> the same 404 (no existence oracle).
        raise HTTPException(status_code=404, detail="That suggestion isn’t available.")
    row = _mo_expire_if_due(row)

    if row["status"] == "executed":
        return {"kind": "executed", "proposal": mo.public_proposal(row),
                "message": row.get("exec_message") or f"Done — {row['effect'][:1].lower()}{row['effect'][1:]}",
                "suggested_actions": mo.resolve_suggestions(
                    mo.default_suggestions("proposal_required", row["action_type"]))}
    # B8.1 — a stale proposal answers with kind:"stale" on EVERY repeated Confirm
    # (HTTP 200), never the cancelled message, never an execution.
    if row["status"] == "stale":
        return _mo_stale_response(row)
    if row["status"] == "cancelled":
        raise HTTPException(status_code=409, detail="You cancelled this, so nothing was changed.")
    if row["status"] == "expired":
        raise HTTPException(status_code=409, detail="This suggestion expired. Ask Mo again.")

    return _mo_execute(row, user_id)


@app.post("/api/process-input/{proposal_id}/cancel")
def cancel_proposal(proposal_id: str, current: dict = Depends(_rl("mo_proposal"))):
    user_id = current["id"]
    row = _mo_get_owned(proposal_id, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="That suggestion isn’t available.")
    row = _mo_expire_if_due(row)
    if row["status"] == "executed":
        raise HTTPException(status_code=409, detail="That’s already done — it can’t be cancelled here.")
    if row["status"] == "executing":
        raise HTTPException(status_code=409, detail="Mo is finishing this — try again in a moment.")
    # already-terminal (incl. B8.1 `stale`) -> acknowledge, mutate nothing, and
    # NEVER flip a truthful `stale` row to `cancelled`.
    if row["status"] in ("cancelled", "expired", "stale"):
        return {"kind": "cancelled", "proposal": mo.public_proposal(row)}
    with db_layer.transaction() as conn:
        conn.execute(
            "UPDATE mo_proposals SET status='cancelled' "
            "WHERE id=? AND user_id=? AND status IN ('pending','failed')",
            (proposal_id, user_id),
        )
    return {"kind": "cancelled", "proposal": mo.public_proposal(_mo_get_owned(proposal_id, user_id))}


@app.get("/api/process-input/pending")
def pending_proposal(current: dict = Depends(_rl("mo_proposal"))):
    """The caller's own ACTIONABLE proposal (pending / executing), or a bounded
    just-resolved acknowledgement: a just-executed result within
    mo.EXECUTED_VISIBLE_SECONDS, or (B8.1) a just-went-stale proposal within
    mo.STALE_VISIBLE_SECONDS so a refresh restores the truthful "no longer
    current / nothing changed" card — NEVER as an actionable proposal, and it
    disappears once the window closes. Cancelled / expired proposals, and stale
    ones past their window, are never returned. Never another user's row."""
    user_id = current["id"]
    conn = get_db()
    try:
        pendings = conn.execute(
            "SELECT id, expires_at FROM mo_proposals WHERE user_id=? AND status='pending'",
            (user_id,),
        ).fetchall()
        active = conn.execute(
            "SELECT * FROM mo_proposals WHERE user_id=? AND status IN ('pending','executing') "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        recent = conn.execute(
            "SELECT * FROM mo_proposals WHERE user_id=? AND status='executed' "
            "ORDER BY confirmed_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        recent_stale = conn.execute(
            "SELECT * FROM mo_proposals WHERE user_id=? AND status='stale' "
            "ORDER BY confirmed_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()

    expired_ids = {r["id"] for r in pendings
                   if mo.is_expired("pending", r["expires_at"], _mo_now())}
    if expired_ids:
        with db_layer.transaction() as c2:
            c2.executemany("UPDATE mo_proposals SET status='expired' WHERE id=? AND status='pending'",
                           [(i,) for i in expired_ids])

    if active is not None and active["id"] not in expired_ids:
        return {"proposal": mo.public_proposal(dict(active))}
    if recent is not None and mo.executed_still_visible(recent["confirmed_at"], _mo_now()):
        return {"proposal": mo.public_proposal(dict(recent))}
    if recent_stale is not None and mo.stale_still_visible(recent_stale["confirmed_at"], _mo_now()):
        return {"proposal": mo.public_proposal(dict(recent_stale))}
    return {"proposal": None}


# --------------------------------------------------------------------------- #
# Phase 5H-B5 — create-recovery status lookup.
#
# After a create whose HTTP response was lost, the official client may reload
# before retrying; its in-memory client_token is gone but a tiny opaque record
# (token + category + timestamp, NO request body) survives in localStorage. On
# reload the client asks THIS endpoint whether that token already committed, so
# it can reconcile the UI instead of the user re-submitting.
#
# Contract:
#   * authenticated; scoped to the session user (reuses the B3
#     (user_id, client_token) uniqueness — no parallel identity system).
#   * validates the token format; a malformed token -> 422.
#   * reveals ONLY whether one of THIS user's own creates used that token, and
#     its category. Never a row id, a payload, or the stored request hash.
#   * a token that is unknown, empty, or belongs to another user -> found:false
#     (not an enumeration oracle: tokens are 122-bit-random and the answer for
#     anything not yours is always the same).
#   * rate-limited (idem_status bucket).
# --------------------------------------------------------------------------- #
class IdemStatusRequest(BaseModel):
    client_token: Optional[str] = None


_IDEM_TABLE_KIND = {
    "transactions": "transaction",
    "tasks": "task",
    "schedule_events": "event",
}


@app.post("/api/idempotency/status")
def idempotency_status(req: IdemStatusRequest, current: dict = Depends(_rl("idem_status"))):
    try:
        token = idem.normalize_token(req.client_token)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if not token:
        return {"found": False}
    for table, kind in _IDEM_TABLE_KIND.items():
        if _prior_create(table, current["id"], token) is not None:
            return {"found": True, "kind": kind}
    return {"found": False}


# --- 5. FINANCE ENDPOINTS ---
_TX_SELECT = ("SELECT id, amount, amount_sen, type, category, description, date, "
              "created_at, updated_at FROM transactions")


@app.get("/api/finance/meta")
def get_finance_meta(current: dict = Depends(get_current_user)):
    """The canonical taxonomy the frontend, AI, and reporting all share."""
    return {"categories": fin.CANONICAL_CATEGORIES, "types": list(fin.TRANSACTION_TYPES)}


@app.get("/api/finance/history")
def get_transaction_history(
    current: dict = Depends(get_current_user),
    type: Optional[str] = None,
    category: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    """Every transaction for the authenticated user, newest first, with optional
    server-side filters. Real persisted data only."""
    sql = _TX_SELECT + " WHERE user_id = ?"
    args: list = [current["id"]]
    if type in fin.TRANSACTION_TYPES:
        sql += " AND type = ?"
        args.append(type)
    if category:
        sql += " AND category = ?"
        args.append(fin.normalize_category(category))
    if date_from:
        try:
            sql += " AND date >= ?"
            args.append(fin.normalize_iso_datetime(date_from, default_now=False))
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    if date_to:
        try:
            sql += " AND date <= ?"
            args.append(fin.normalize_iso_datetime(date_to, default_now=False))
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    sql += " ORDER BY date DESC, created_at DESC"

    conn = get_db()
    try:
        rows = conn.execute(sql, tuple(args)).fetchall()
        return {"transactions": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/api/finance")
def get_finance_data(current: dict = Depends(get_current_user)):
    user_id = current["id"]
    conn = get_db()
    try:
        transactions = conn.execute(
            _TX_SELECT + " WHERE user_id = ? ORDER BY date DESC, created_at DESC LIMIT 10",
            (user_id,),
        ).fetchall()
        recommendations = conn.execute(
            "SELECT id, module, type, message, status, created_at, updated_at "
            "FROM ai_recommendations WHERE user_id = ? AND module = 'FINANCE' AND status = 'PENDING'",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    return {
        "transactions": [dict(t) for t in transactions],
        "active_recommendations": [dict(r) for r in recommendations],
        "snapshot": get_finance_snapshot(user_id),
    }


@app.get("/api/finance-summary")
def get_finance_summary(current: dict = Depends(get_current_user)):
    return get_finance_snapshot(current["id"])


# --- transaction CRUD (Phase 3) ---------------------------------------------
@app.post("/api/transactions", status_code=status.HTTP_201_CREATED)
def create_transaction_endpoint(req: TransactionCreate, response: Response,
                                current: dict = Depends(get_current_user)):
    user_id = current["id"]
    try:
        token = idem.normalize_token(req.client_token)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    fields = _validate_transaction_fields(
        amount=req.amount, type_=req.type, category=req.category,
        description=req.description, date=req.date, require_amount=True,
    )
    # Fingerprint the NORMALIZED persistent intent only. The date is taken from
    # the CLIENT value (or None) — never the server 'now' default — so a retry
    # a few seconds later still matches.
    fp = idem.request_fingerprint({
        "amount_sen": fields["amount_sen"],
        "type": fields.get("type", "expense"),
        "category": fields.get("category", "Other"),
        "description": fields.get("description", ""),
        "date": fields["date"] if req.date else None,
    })

    def _replay(prior):
        prior_id, prior_hash = prior
        if prior_hash is not None and prior_hash != fp:
            raise idem.IdempotencyConflict()
        response.status_code = status.HTTP_200_OK
        return {"status": "SUCCESS", "idempotent": True,
                "transaction": _get_owned_transaction(user_id, prior_id),
                "snapshot": get_finance_snapshot(user_id)}

    if token:
        prior = _prior_create("transactions", user_id, token)
        if prior is not None:
            return _replay(prior)
    try:
        row = _insert_transaction(user_id, fields, client_token=token,
                                  client_request_hash=fp if token else None)
    except sqlite3.IntegrityError:
        prior = _prior_create("transactions", user_id, token) if token else None
        if prior is not None:
            return _replay(prior)
        raise HTTPException(status_code=500,
                            detail="Couldn't save that transaction. Please try again.")
    return {"status": "SUCCESS", "transaction": row, "snapshot": get_finance_snapshot(user_id)}


@app.patch("/api/transactions/{tx_id}")
def update_transaction_endpoint(tx_id: str, req: TransactionUpdate,
                                current: dict = Depends(get_current_user)):
    user_id = current["id"]
    if _get_owned_transaction(user_id, tx_id) is None:
        raise HTTPException(status_code=404, detail="Transaction not found.")

    fields = _validate_transaction_fields(
        amount=req.amount, type_=req.type, category=req.category,
        description=req.description, date=req.date, require_amount=False,
    )
    if not fields:
        raise HTTPException(status_code=422, detail="No fields to update.")

    sets, args = [], []
    for col in ("amount", "amount_sen", "type", "category", "description", "date"):
        if col in fields:
            sets.append(f"{col} = ?")
            args.append(fields[col])
    sets.append("updated_at = ?")
    args.append(get_iso_now())
    args += [tx_id, user_id]

    try:
        with db_layer.transaction() as conn:
            cur = conn.execute(
                f"UPDATE transactions SET {', '.join(sets)} WHERE id = ? AND user_id = ?",
                tuple(args),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Transaction not found.")
    except HTTPException:
        raise
    except Exception as e:
        print(f"DB_ERROR (update_transaction): {str(e)}")
        raise HTTPException(status_code=500, detail="Couldn't update that transaction. Please try again.")

    return {"status": "SUCCESS", "transaction": _get_owned_transaction(user_id, tx_id),
            "snapshot": get_finance_snapshot(user_id)}


@app.delete("/api/transactions/{tx_id}")
def delete_transaction_endpoint(tx_id: str, current: dict = Depends(get_current_user)):
    user_id = current["id"]
    try:
        with db_layer.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM transactions WHERE id = ? AND user_id = ?", (tx_id, user_id)
            )
            deleted = cur.rowcount
    except Exception as e:
        print(f"DB_ERROR (delete_transaction): {str(e)}")
        raise HTTPException(status_code=500, detail="Couldn't delete that transaction. Please try again.")
    if not deleted:
        raise HTTPException(status_code=404, detail="Transaction not found.")
    return {"status": "SUCCESS", "snapshot": get_finance_snapshot(user_id)}

@app.get("/api/finance/settings")
def get_finance_settings(current: dict = Depends(get_current_user)):
    """Returns only the fields the client needs: starting balance + the three
    budgets. The owning `user_id` is intentionally NOT returned (the caller is
    already the authenticated user)."""
    conn = get_db()
    try:
        s = fin.ensure_financial_settings(conn, current["id"])
        return {
            "starting_balance": fin.from_sen(s["starting_balance_sen"]),
            "weekly_budget": fin.from_sen(s["weekly_sen"]),
            "monthly_budget": fin.from_sen(s["monthly_sen"]),
            "daily_budget": fin.from_sen(s["daily_sen"]),
        }
    finally:
        conn.close()

@app.post("/api/finance/settings")
def save_finance_settings(req: FinanceSettingsUpdate, current: dict = Depends(get_current_user)):
    """Update starting balance and/or budgets. Only supplied fields change
    (omitted ones keep their stored value). user_financial_settings is the ONE
    store for these values in Phase 3. All money is normalized to 2dp via sen."""
    user_id = current["id"]

    def _norm(label, value, *, allow_zero=True):
        if value is None:
            return None
        try:
            sen = fin.to_sen(value)
        except fin.MoneyError as e:
            raise HTTPException(status_code=422, detail=f"{label}: {e}")
        if sen < 0:
            raise HTTPException(status_code=400, detail=f"{label} cannot be negative.")
        return fin.from_sen(sen)

    supplied = {
        "starting_balance": _norm("Starting balance", req.starting_balance),
        "weekly_budget": _norm("Weekly budget", req.weekly_budget),
        "monthly_budget": _norm("Monthly budget", req.monthly_budget),
        "daily_budget": _norm("Daily budget", req.daily_budget),
    }
    if all(v is None for v in supplied.values()):
        raise HTTPException(status_code=422, detail="No fields to update.")

    conn = get_db()
    try:
        existing = conn.execute(
            "SELECT starting_balance, weekly_budget, monthly_budget, daily_budget "
            "FROM user_financial_settings WHERE user_id = ?", (user_id,)
        ).fetchone()
        current_vals = {
            "starting_balance": float(existing["starting_balance"]) if existing else 0.0,
            "weekly_budget": float(existing["weekly_budget"]) if existing else 0.0,
            "monthly_budget": float(existing["monthly_budget"]) if existing else 0.0,
            "daily_budget": float(existing["daily_budget"]) if existing else 0.0,
        }
        merged = {k: (current_vals[k] if supplied[k] is None else supplied[k]) for k in current_vals}

        conn.execute(
            """
            INSERT INTO user_financial_settings
            (user_id, starting_balance, weekly_budget, monthly_budget, daily_budget, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                starting_balance = excluded.starting_balance,
                weekly_budget = excluded.weekly_budget,
                monthly_budget = excluded.monthly_budget,
                daily_budget = excluded.daily_budget,
                updated_at = excluded.updated_at
            """,
            (user_id, merged["starting_balance"], merged["weekly_budget"],
             merged["monthly_budget"], merged["daily_budget"], get_iso_now()),
        )
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        print(f"DB_ERROR (save_finance_settings): {str(e)}")
        raise HTTPException(status_code=500, detail="Couldn't save your financial settings. Please try again.")
    finally:
        conn.close()

    return {
        "status": "SUCCESS",
        "message": "Financial settings saved.",
        "data": dict(merged),
        "snapshot": get_finance_snapshot(user_id),
    }

# --- 6. ACTIVITY ENDPOINTS ---
def _get_activity_recommendations(user_id: str) -> list[dict]:
    """The ONE authoritative read of the user's pending Activity-module
    recommendations. Shared by /api/activity and the Today dashboard so
    there is exactly one query path, not two ad-hoc copies."""
    conn = get_db()
    try:
        recommendations = conn.execute(
            "SELECT * FROM ai_recommendations WHERE user_id = ? AND module = 'ACTIVITY' AND status = 'PENDING'",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in recommendations]


@app.get("/api/activity")
def get_activity_data(current: dict = Depends(get_current_user)):
    return {"active_recommendations": _get_activity_recommendations(current["id"])}

# --- 7. SETTINGS & MAPS ---
@app.get("/api/settings")
def get_settings(current: dict = Depends(get_current_user)):
    # Explicit columns only — never `SELECT *`. The owning `user_id` is not
    # returned (the caller is already the authenticated user; mirrors
    # /api/finance/settings), and a future column is not auto-exposed.
    conn = get_db()
    settings = conn.execute(
        "SELECT notifications, location_services, dark_mode, appearance_mode "
        "FROM user_preferences WHERE user_id = ?",
        (current["id"],),
    ).fetchone()
    conn.close()
    if not settings:
        return {}
    out = dict(settings)
    # `appearance_mode` NULL / unrecognised -> no account preference. The legacy
    # `dark_mode` boolean is intentionally NOT used as a fallback (it was inert);
    # the client then follows this device's saved choice, otherwise System.
    if out.get("appearance_mode") not in _APPEARANCE_MODES:
        out["appearance_mode"] = None
    return out

@app.post("/api/settings/update")
def update_settings(req: SettingsUpdate, current: dict = Depends(get_current_user)):
    mode = req.appearance_mode
    if mode is not None and mode not in _APPEARANCE_MODES:
        raise HTTPException(status_code=422, detail="appearance_mode must be system, light or dark.")

    conn = get_db()
    try:
        # `appearance_mode` is only written when supplied (COALESCE keeps any
        # existing value on a partial update); `dark_mode` is preserved untouched
        # on update and defaults to its historical 1 on first insert.
        conn.execute(
            """
            INSERT INTO user_preferences (user_id, notifications, location_services, dark_mode, appearance_mode)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
              notifications   = excluded.notifications,
              location_services = excluded.location_services,
              dark_mode = COALESCE(excluded.dark_mode, user_preferences.dark_mode),
              appearance_mode = COALESCE(excluded.appearance_mode, user_preferences.appearance_mode)
            """,
            (
                current["id"], int(req.notifications), int(req.location_services),
                (None if req.dark_mode is None else (1 if req.dark_mode else 0)),
                mode,
            ),
        )
        conn.commit()
        return {"message": "Preferences saved persistently"}
    except Exception as e:
        conn.rollback()
        print(f"DB_ERROR (update_settings): {str(e)}")
        raise HTTPException(status_code=500, detail="Couldn't save your preferences. Please try again.")
    finally:
        conn.close()

@app.post("/api/location/validate", response_model=LocationValidateResult)
async def validate_location(request: Request, current: dict = Depends(get_current_user)):
    """Phase 5B — authoritative server-side coordinate validation.

    Stateless: nothing is stored, nothing is logged (coordinates never touch a
    log line or an error message). The normalised coordinate is echoed back so a
    caller can trust it before a later place / weather query (Phase 5C+). Auth-
    gated for consistency with every other CoreOS endpoint; no location data is
    attached to the session.
    """
    try:
        raw = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Location body must be valid JSON.")
    return LocationValidateResult(**validate_location_payload(raw))


@app.post("/api/places/search", response_model=PlacesSearchResult)
async def places_search(request: Request, current: dict = Depends(_rl("places_search"))):
    """Phase 5C — real nearby-place search.

    Body: {activity, latitude, longitude, radius_m?, adaptive_radius?}.
    Coordinates go in the BODY (never a URL). Auth-gated. Server validates
    coordinates (Phase 5B), the activity (taxonomy) and the radius (<= 20 km),
    then calls the place-provider abstraction. Results are normalised,
    distance-sorted (straight line) and capped. With `adaptive_radius: true`
    (B10) the search runs the staged 8 -> 12 -> 20 km ladder with a strict
    per-rung within-radius gate (parity with /api/activities/search); it is
    mutually exclusive with a fixed `radius_m` (422). Nothing is persisted;
    coordinates are never logged; provider credentials never reach the browser.
    Provider failures come back as a normalised `status: "PROVIDER_ERROR"`
    outcome, not a raw upstream message.
    """
    try:
        raw = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Search body must be valid JSON.")
    return await _run_place_search(current["id"], raw)


@app.post("/api/places/resolve", response_model=PlaceResolveResult)
async def places_resolve(request: Request, current: dict = Depends(_rl("places_resolve"))):
    """Phase 5G — Schedule -> Map "View place": resolve ONE stored linked-place
    reference back to a focusable place.

    Body: {provider, provider_place_id, name, latitude, longitude, address?} —
    the event's persisted place snapshot. Auth-gated. For a Google reference the
    server does a live Place Details lookup so volatile facts (rating, open-now,
    hours) are FRESH; if the provider is unavailable / not configured / no longer
    knows the id, it returns the stored snapshot with `status: "SNAPSHOT"` and a
    truthful `reason` — it never freezes or invents live metadata. OSM references
    are snapshot-only (OSM publishes no volatile facts). Provider credentials
    never reach the browser; coordinates are never logged.
    """
    try:
        raw = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Resolve body must be valid JSON.")
    return await _run_place_resolve(raw)


@app.post("/api/geocode/search", response_model=GeoSearchResult)
async def geocode_search(request: Request, current: dict = Depends(_rl("geocode"))):
    """Phase 5D — resolve a typed area / place name to real coordinates so the
    user can set an EXPLICIT search origin on the Map.

    Body: {query}. Auth-gated. The provider API key stays server-side (sent only
    to the provider, never returned or logged). Nothing is persisted; result
    coordinates are never logged; the raw provider status is normalised to a
    PROVIDER_* kind. The frontend must let the user pick a result — it is never
    auto-selected server-side.
    """
    try:
        raw = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Search body must be valid JSON.")
    return await _run_geocode(raw)


@app.post("/api/activities/search", response_model=ActivitySearchResult)
async def activities_search(request: Request, current: dict = Depends(_rl("activities"))):
    """Phase 5E — Activity Engine: turn an activity intent into ranked, truthfully
    classified real local candidates.

    Body: {activity_id, origin:{latitude, longitude, source, accuracy_m?}}.
    Coordinates go in the BODY (never a URL). Auth-gated. Server validates the
    activity id, the coordinates (Phase 5B) and the origin source. The
    deterministic activity_core engine — never an LLM — owns requirements,
    evidence evaluation, relevance/confidence and ordering. Provider keys stay
    server-side; nothing is persisted; coordinates are never logged (only the
    provider name, activity id and error KIND on failure).
    """
    try:
        raw = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Search body must be valid JSON.")
    return await _run_activity_search(raw)


@app.post("/api/weather/context", response_model=WeatherContextResult)
async def weather_context(request: Request, current: dict = Depends(_rl("weather"))):
    """Phase 5F — Weather Intelligence Foundation.

    Body: {origin:{latitude, longitude}, start_time?, end_time?}. Coordinates go
    in the BODY (never a URL). Auth-gated. Returns the provider's CURRENT
    conditions plus, when a tz-aware start/end window is supplied, a deterministic
    aggregated FORECAST for that window (representative/min/max temperature,
    max precipitation *probability* — never converted to certainty — plus
    normalised WMO conditions and factual context primitives). Timezone is the
    TARGET LOCATION's local time (Open-Meteo timezone=auto). Provider-neutral
    downstream. Stateless: nothing is persisted, coordinates are never logged
    (only provider name + error KIND), and no AI is involved.
    """
    try:
        raw = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Weather body must be valid JSON.")
    return await _run_weather_context(raw)


@app.get("/api/map-locations")
def get_map_locations(
    current: dict = Depends(get_current_user),
    activity: Optional[str] = None,
):
    # DEPRECATED (superseded by POST /api/places/search in Phase 5C). Kept only so
    # the Phase 2 auth / IDOR test-suite still has an endpoint to probe. The
    # `map_locations` table is unused (0 rows) and the real Map never calls this
    # route (proven by tests/test_map_experience.py). No lat/lng params — the
    # Map surface never puts coordinates in a URL.
    user_id = current["id"]
    conn = get_db()
    try:
        if activity:
            rows = conn.execute(
                "SELECT * FROM map_locations WHERE user_id = ? AND (activity = ? OR activity IS NULL) ORDER BY id",
                (user_id, activity)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM map_locations WHERE user_id = ? ORDER BY id",
                (user_id,)
            ).fetchall()

        locations = [dict(r) for r in rows]
        return {
            "locations": locations,
            "message": None if locations
            else "Nearby place search isn't available yet. It's coming in a later update.",
        }
    finally:
        conn.close()


if __name__ == "__main__":
    # Allows `python main.py`; honours COREOS_HOST / COREOS_PORT from the env.
    # Uvicorn's default proxy-header handling (trusted: 127.0.0.1) is correct
    # for the Caddy front door and is left in place. HOST is loopback by
    # default (see above). Server-header suppression, if wanted, belongs at the
    # Caddy / deployment edge, not here.
    import uvicorn
    print(f"SYSTEM: API docs {'exposed at /api/docs' if sec.EXPOSE_DOCS else 'disabled'}; "
          f"bind {HOST}:{PORT}.")
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False)