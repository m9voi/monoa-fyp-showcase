"""Pytest fixtures for CoreOS (Phase 2).

Every test runs against a TEMPORARY sqlite database. The real coreos.db is
never opened by the test-suite. The AI boundary is never called for real —
tests that exercise /api/process-input use the `mock_ai` fixture.
"""

import os
import sys
import tempfile
import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --- configure BEFORE importing main -----------------------------------------
_TMP_DIR = tempfile.mkdtemp(prefix="coreos_test_")
_SESSION_DB = os.path.join(_TMP_DIR, "coreos_test.db")
os.environ["COREOS_DB_PATH"] = _SESSION_DB
os.environ["COREOS_SCHEDULER_ENABLED"] = "0"
os.environ["COREOS_ENV"] = "development"
os.environ["COREOS_SESSION_SECRET"] = "test-secret-not-a-real-one-0123456789abcdef"
# The suite drives the app over a simulated HTTP TestClient, so the session
# cookie must NOT be Secure here — pin it independently of the developer's local
# .env (which sets COREOS_COOKIE_SECURE=1 for the HTTPS reverse proxy). Phase 5A.
os.environ["COREOS_COOKIE_SECURE"] = "0"
# The suite must NEVER reach a live external place provider (a real
# GOOGLE_PLACES_API_KEY may exist in the developer's local .env). Pin the
# provider to OSM with no key; provider-adapter tests inject their own mocked
# HTTP transport, and endpoint tests monkeypatch get_provider(). Phase 5C.
os.environ["GOOGLE_PLACES_API_KEY"] = ""
os.environ["COREOS_PLACES_PROVIDER"] = "osm"
# Same guard for the Phase 5D geocoding ("Change location") provider: never let
# the suite reach live Google Geocoding or Nominatim. Adapter tests inject a
# mocked HTTP transport; endpoint tests monkeypatch get_geo_provider().
os.environ["GOOGLE_GEOCODING_API_KEY"] = ""
os.environ["COREOS_GEOCODE_PROVIDER"] = "osm"
# Phase 5F: the suite must NEVER reach live Open-Meteo. Provider-adapter tests
# inject a mocked HTTP transport; endpoint tests monkeypatch get_weather_provider().
os.environ["COREOS_WEATHER_PROVIDER"] = "open-meteo"
os.environ.setdefault("OPENAI_API_KEY", "")

# Direct-DB tests still use this id for the legacy-style account.
ADMIN_ID = "mussab_admin_1"


def apply_migrations(db_path: str) -> None:
    from alembic.config import Config
    from alembic import command

    prev = os.environ.get("COREOS_DB_PATH")
    os.environ["COREOS_DB_PATH"] = db_path
    try:
        cfg = Config(os.path.join(ROOT, "alembic.ini"))
        cfg.set_main_option("script_location", os.path.join(ROOT, "alembic"))
        command.upgrade(cfg, "head")
    finally:
        if prev is not None:
            os.environ["COREOS_DB_PATH"] = prev


# Import main once -> runs init_db() -> alembic upgrade head on the temp DB.
import main  # noqa: E402
import db as db_layer  # noqa: E402
import auth as auth_core  # noqa: E402


def _now():
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()


def _reset_session_db():
    """Wipe ALL rows (keep schema). No app data is seeded — users register."""
    conn = db_layer.get_connection()
    conn.execute("PRAGMA foreign_keys = OFF")
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' AND name <> 'alembic_version'"
    ).fetchall():
        conn.execute(f"DELETE FROM {name}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()
    conn.close()
    auth_core._attempts.clear()  # reset rate-limiter between tests


@pytest.fixture(autouse=True)
def _clean_db():
    _reset_session_db()
    yield


def seed_direct_user(user_id: str = ADMIN_ID, email: str = "legacy@coreos.local",
                     password: str | None = None):
    """Insert a user row straight into the DB (for direct-DB tests / legacy sim)."""
    conn = db_layer.get_connection()
    pw = auth_core.hash_password(password) if password else None
    conn.execute(
        "INSERT OR IGNORE INTO users (id, name, email, email_normalized, password_hash, "
        "is_active, created_at, updated_at) VALUES (?, 'Legacy', ?, ?, ?, 1, ?, ?)",
        (user_id, email, email.lower(), pw, _now(), _now()),
    )
    conn.commit()
    conn.close()
    return user_id


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    return TestClient(main.app)


def new_authed_client(email: str, password: str = "password123", name: str | None = None):
    """A fresh TestClient registered + logged in as `email`. Returns (client, user)."""
    from fastapi.testclient import TestClient
    tc = TestClient(main.app)
    r = tc.post("/api/auth/register", json={"email": email, "password": password, "name": name})
    assert r.status_code == 201, r.text
    return tc, r.json()["user"]


@pytest.fixture()
def alice():
    return new_authed_client("alice@example.com", "alicepass123", "Alice")


@pytest.fixture()
def bob():
    return new_authed_client("bob@example.com", "bobpass123", "Bob")


@pytest.fixture()
def conn():
    """Direct DB connection with a minimal seeded user (id = ADMIN_ID)."""
    seed_direct_user()
    c = db_layer.get_connection()
    yield c
    c.close()


_MO_READ_INTENTS = ("GET_SPENDING_SUMMARY", "GET_SCHEDULE_SUMMARY", "GET_TODAY_SUMMARY",
                    "GENERAL_UTILITY", "SUGGEST_FREE_TIME")


@pytest.fixture()
def mock_ai(monkeypatch):
    """Patch AIService.interpret (the single-OpenAI-call boundary) so no real
    model call is ever made. Returns a setter: mock_ai(intent,
    parameters=..., clarification=...) — UNCHANGED call signature from before
    the Mo multi-operation closure, translated internally into the new
    MoInterpretation(mode, operations=[...]) envelope so every pre-existing
    single-intent test keeps working without modification."""
    import main as m

    def _set(intent, parameters=None, clarification=None):
        def fake_interpret(_user_input, context_date=None, context_entity=None, user_interests=None):
            if intent == "CLARIFICATION_REQUIRED":
                return m.MoInterpretation(mode="clarification", operations=[],
                                          clarification_question=clarification)
            if intent == "ACTION_NOT_SUPPORTED":
                return m.MoInterpretation(mode="unsupported", operations=[])
            mode = "read" if intent in _MO_READ_INTENTS else "single_operation"
            return m.MoInterpretation(
                mode=mode,
                operations=[m.MoOperation(intent=m.IntentEnum(intent), parameters=parameters)],
            )
        monkeypatch.setattr(m.AIService, "interpret", staticmethod(fake_interpret))

    return _set


@pytest.fixture()
def mock_ai_interp(monkeypatch):
    """Patch AIService.interpret with an arbitrary, hand-built
    MoInterpretation — for multi_operation/plan_day tests that need more than
    one operations[] entry. Returns a setter: mock_ai_interp(mode=...,
    operations=[{"intent": ..., "parameters": ..., "constraint": ...,
    "condition": ...}, ...], unsupported_items=[...], narrative=...,
    clarification_question=...)."""
    import main as m

    def _set(mode, operations=None, unsupported_items=None, narrative=None,
             clarification_question=None):
        ops = [m.MoOperation(**o) for o in (operations or [])]
        interp = m.MoInterpretation(mode=mode, operations=ops,
                                    unsupported_items=unsupported_items or [],
                                    narrative=narrative,
                                    clarification_question=clarification_question)

        def fake_interpret(_user_input, context_date=None, context_entity=None, user_interests=None):
            return interp
        monkeypatch.setattr(m.AIService, "interpret", staticmethod(fake_interpret))

    return _set


@pytest.fixture()
def fresh_db_path(tmp_path):
    p = str(tmp_path / "fresh.db")
    apply_migrations(p)
    return p


# --- B2: Mo human-in-the-loop helpers --------------------------------------- #
# POST /api/process-input no longer mutates. A test whose intent is "the Mo path
# ultimately persists X exactly once" now proposes AND confirms.

def mo_propose(tc, text: str, token: str | None = None):
    """POST /api/process-input -> the raw response (status_code kept on .last)."""
    body = {"user_input": text}
    if token is not None:
        body["client_token"] = token
    return tc.post("/api/process-input", json=body)


def mo_confirm(tc, proposal_id: str):
    return tc.post(f"/api/process-input/{proposal_id}/confirm")


def mo_cancel(tc, proposal_id: str):
    return tc.post(f"/api/process-input/{proposal_id}/cancel")


def mo_pending(tc):
    return tc.get("/api/process-input/pending")


def mo_run(tc, text: str, token: str | None = None):
    """Propose then confirm. Returns (propose_json, confirm_json). Asserts the
    propose step produced a proposal (raises AssertionError otherwise)."""
    pr = mo_propose(tc, text, token)
    assert pr.status_code == 200, pr.text
    pj = pr.json()
    assert pj.get("kind") == "proposal_required", pj
    cj = mo_confirm(tc, pj["proposal"]["proposal_id"])
    return pj, cj
