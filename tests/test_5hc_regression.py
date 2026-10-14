"""Phase 5H-C — adversarial cross-component regression.

These tests deliberately chain B1-B6 mechanisms into realistic multi-step
sequences (unknown create + session expiry + reconciliation, account switch +
reconciliation IDOR, CSRF + mutation side effects, expired session still trying
to mutate). Single-mechanism happy paths live in the per-phase suites; this file
only exercises the interactions.
"""
import datetime

import pytest

import main
import db as db_layer


def _count(table: str, user_id: str) -> int:
    conn = db_layer.get_connection()
    try:
        return conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def _expire_all_sessions() -> None:
    """Force every live session to be past its expiry (server-side)."""
    past = (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=1)).astimezone().isoformat()
    conn = db_layer.get_connection()
    try:
        conn.execute("UPDATE sessions SET expires_at = ?", (past,))
        conn.commit()
    finally:
        conn.close()


_TX = {"amount": 20.0, "type": "expense", "category": "Food", "description": "lunch"}


# ------------------------------------------------------------------------- #
# SCENARIO A — unknown create -> session expiry -> reconcile 401 ->
# same-user re-auth -> found:true -> exactly one row, truthful recovery.
# ------------------------------------------------------------------------- #
def test_scenario_a_unknown_create_survives_session_expiry_and_reauth(alice, monkeypatch):
    tc, user = alice
    uid = user["id"]
    tok = "tok-5hc-A-1"

    # 1-2. the create commits, then the response is lost (raise AFTER the insert).
    real_snapshot = main.get_finance_snapshot
    seen = {"n": 0}

    def boom(user_id):
        seen["n"] += 1
        if seen["n"] == 1:
            raise RuntimeError("simulated: response lost after commit")
        return real_snapshot(user_id)

    monkeypatch.setattr(main, "get_finance_snapshot", boom)
    with pytest.raises(RuntimeError):
        tc.post("/api/transactions", json={**_TX, "client_token": tok})
    monkeypatch.setattr(main, "get_finance_snapshot", real_snapshot)
    assert _count("transactions", uid) == 1

    # 3-4. the session expires; reconciliation now gets a 401.
    _expire_all_sessions()
    r401 = tc.post("/api/idempotency/status", json={"client_token": tok})
    assert r401.status_code == 401
    # the committed row is untouched by the failed reconcile
    assert _count("transactions", uid) == 1

    # 5. the SAME user re-authenticates (new session, same user_id).
    assert tc.post("/api/auth/login",
                   json={"email": "alice@example.com", "password": "alicepass123"}
                   ).status_code == 200

    # 6. reconciliation resumes and truthfully recovers the committed create.
    s = tc.post("/api/idempotency/status", json={"client_token": tok})
    assert s.status_code == 200
    assert s.json() == {"found": True, "kind": "transaction"}

    # 7. a retry with the same token + payload replays; still exactly one row.
    replay = tc.post("/api/transactions", json={**_TX, "client_token": tok})
    assert replay.status_code == 200 and replay.json().get("idempotent") is True
    assert _count("transactions", uid) == 1


# ------------------------------------------------------------------------- #
# SCENARIO B — unknown create + account switch: B cannot see A's data or
# reconcile A's token; cross-user client_token reuse stays isolated.
# ------------------------------------------------------------------------- #
def test_scenario_b_account_switch_cannot_observe_or_reconcile_other_users_create(alice, bob):
    tc_a, a = alice
    tc_b, b = bob
    tok = "tok-5hc-B-shared"

    # A makes a real create carrying the token.
    assert tc_a.post("/api/transactions", json={**_TX, "client_token": tok}).status_code == 201
    assert _count("transactions", a["id"]) == 1
    assert _count("transactions", b["id"]) == 0

    # B (a different session) asks about A's token -> nothing. Not an oracle.
    rb = tc_b.post("/api/idempotency/status", json={"client_token": tok})
    assert rb.status_code == 200 and rb.json() == {"found": False}

    # B's own finance view never contains A's row.
    lst = tc_b.get("/api/finance")
    assert lst.status_code == 200
    assert all(row.get("description") != "lunch"
               for row in lst.json().get("transactions", []))

    # B reuses the SAME token string for B's own create -> B gets B's own row
    # (partial unique index is per (user_id, client_token)); A is untouched.
    assert tc_b.post("/api/transactions",
                     json={"amount": 5.0, "type": "expense", "category": "Other",
                           "description": "bob-thing", "client_token": tok}).status_code == 201
    assert _count("transactions", a["id"]) == 1
    assert _count("transactions", b["id"]) == 1

    # each side still reconciles only its own.
    assert tc_a.post("/api/idempotency/status", json={"client_token": tok}).json() == {
        "found": True, "kind": "transaction"}
    assert tc_b.post("/api/idempotency/status", json={"client_token": tok}).json() == {
        "found": True, "kind": "transaction"}


# ------------------------------------------------------------------------- #
# SCENARIO D — CSRF: a disallowed Origin on an authenticated mutation is a
# generic 403 BEFORE any side effect, on every idempotent create route, and
# never echoes the attacker origin.
# ------------------------------------------------------------------------- #
_EVIL = "https://evil.attacker.example"


@pytest.mark.parametrize("path,body", [
    ("/api/transactions", {**_TX}),
    ("/api/tasks", {"title": "csrf-task"}),
    ("/api/events", {"title": "csrf-ev", "start": "2026-09-10T10:00:00+08:00"}),
])
def test_scenario_d_disallowed_origin_mutation_is_generic_403_with_no_side_effect(alice, path, body):
    tc, user = alice
    uid = user["id"]
    table = {"/api/transactions": "transactions", "/api/tasks": "tasks",
             "/api/events": "schedule_events"}[path]

    r = tc.post(path, json=body, headers={"Origin": _EVIL})
    assert r.status_code == 403
    assert r.json() == {"detail": "Request origin is not allowed."}
    assert "evil" not in r.text.lower() and "attacker" not in r.text.lower()
    assert _count(table, uid) == 0  # nothing was written


def test_scenario_d_allowed_origin_still_writes(alice):
    tc, user = alice
    # a same-site dev origin from the allowlist
    r = tc.post("/api/transactions", json={**_TX},
                headers={"Origin": "http://localhost:3000"})
    assert r.status_code == 201
    assert _count("transactions", user["id"]) == 1


# ------------------------------------------------------------------------- #
# Expired session cannot continue mutating (network-loss != auth-loss, but a
# genuinely expired session must be refused).
# ------------------------------------------------------------------------- #
def test_expired_session_cannot_mutate_or_read(alice):
    tc, user = alice
    _expire_all_sessions()
    assert tc.post("/api/transactions", json={**_TX}).status_code == 401
    assert tc.get("/api/finance").status_code == 401
    assert tc.post("/api/idempotency/status", json={"client_token": "x"}).status_code == 401
    assert _count("transactions", user["id"]) == 0


# ------------------------------------------------------------------------- #
# Repeated reconciliation passes for a committed token are stable and have no
# side effects (idempotent read).
# ------------------------------------------------------------------------- #
def test_repeated_reconciliation_is_stable_and_side_effect_free(alice):
    tc, user = alice
    tok = "tok-5hc-O-1"
    assert tc.post("/api/transactions", json={**_TX, "client_token": tok}).status_code == 201
    for _ in range(5):
        assert tc.post("/api/idempotency/status", json={"client_token": tok}).json() == {
            "found": True, "kind": "transaction"}
    assert _count("transactions", user["id"]) == 1


# ------------------------------------------------------------------------- #
# SCENARIO F — a provider search with real coordinates persists nothing.
# ------------------------------------------------------------------------- #
def test_scenario_f_provider_search_persists_nothing(alice, monkeypatch):
    tc, user = alice

    class _Stub:
        name = "osm"
        attribution = "© OpenStreetMap contributors"

        async def search_nearby(self, **_kw):
            return []

    monkeypatch.setattr(main.places_provider, "get_provider", lambda *a, **k: _Stub())

    def _rowcount() -> int:
        conn = db_layer.get_connection()
        try:
            total = 0
            for t in ("transactions", "tasks", "schedule_events", "map_locations",
                      "user_preferences", "ai_recommendations"):
                total += conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            return total
        finally:
            conn.close()

    before = _rowcount()
    r = tc.post("/api/places/search",
                json={"activity": "futsal", "latitude": 3.1578, "longitude": 101.7117})
    assert r.status_code == 200
    assert _rowcount() == before  # coordinates went in the body; nothing was stored
