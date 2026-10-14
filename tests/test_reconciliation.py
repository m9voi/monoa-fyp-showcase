"""Phase 5H-B5 — create-recovery status lookup (POST /api/idempotency/status).

The endpoint lets a client that lost a create's HTTP response ask, after a
reload, whether the create with a given opaque client_token actually committed —
without re-sending the request body.
"""
import sqlite3

import pytest

import main
import db as db_layer


def _count_tx(user_id: str) -> int:
    conn = db_layer.get_connection()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# auth + input contract
# --------------------------------------------------------------------------- #
def test_status_requires_auth(client):
    r = client.post("/api/idempotency/status", json={"client_token": "abc"})
    assert r.status_code == 401


def test_status_malformed_token_is_422(alice):
    tc, _ = alice
    r = tc.post("/api/idempotency/status", json={"client_token": "bad token!!"})
    assert r.status_code == 422


def test_status_empty_token_is_not_found(alice):
    tc, _ = alice
    for body in ({}, {"client_token": None}, {"client_token": ""}, {"client_token": "   "}):
        r = tc.post("/api/idempotency/status", json=body)
        assert r.status_code == 200
        assert r.json() == {"found": False}


def test_status_unknown_token_is_not_found(alice):
    tc, _ = alice
    r = tc.post("/api/idempotency/status",
                json={"client_token": "11111111-2222-3333-4444-555555555555"})
    assert r.status_code == 200
    assert r.json() == {"found": False}


# --------------------------------------------------------------------------- #
# found + category, per idempotent create kind
# --------------------------------------------------------------------------- #
def test_status_found_after_transaction_create(alice):
    tc, _ = alice
    tok = "tok-tx-recover-1"
    r = tc.post("/api/transactions",
                json={"amount": 12.5, "description": "x", "client_token": tok})
    assert r.status_code == 201
    s = tc.post("/api/idempotency/status", json={"client_token": tok})
    assert s.status_code == 200
    assert s.json() == {"found": True, "kind": "transaction"}


def test_status_found_after_task_create(alice):
    tc, _ = alice
    tok = "tok-task-recover-1"
    assert tc.post("/api/tasks", json={"title": "T", "client_token": tok}).status_code == 201
    assert tc.post("/api/idempotency/status", json={"client_token": tok}).json() == {
        "found": True, "kind": "task",
    }


def test_status_found_after_event_create(alice):
    tc, _ = alice
    tok = "tok-ev-recover-1"
    r = tc.post("/api/events", json={
        "title": "E", "start": "2026-09-10T10:00:00+08:00", "client_token": tok,
    })
    assert r.status_code == 201
    assert tc.post("/api/idempotency/status", json={"client_token": tok}).json() == {
        "found": True, "kind": "event",
    }


# --------------------------------------------------------------------------- #
# cross-user isolation — a token reveals only the caller's own row
# --------------------------------------------------------------------------- #
def test_status_is_scoped_to_the_session_user(alice, bob):
    tc_a, _ = alice
    tc_b, _ = bob
    tok = "tok-shared-across-users"
    assert tc_a.post("/api/transactions",
                     json={"amount": 5, "client_token": tok}).status_code == 201
    # bob asks about alice's token -> nothing
    assert tc_b.post("/api/idempotency/status", json={"client_token": tok}).json() == {
        "found": False,
    }
    # alice still sees her own
    assert tc_a.post("/api/idempotency/status", json={"client_token": tok}).json()["found"] is True


# --------------------------------------------------------------------------- #
# the real "commit succeeded, HTTP response was lost" scenario
# --------------------------------------------------------------------------- #
def test_committed_then_response_lost_is_recoverable_and_not_duplicated(alice, monkeypatch):
    tc, user = alice
    uid = user["id"]
    tok = "tok-lost-response-1"
    payload = {"amount": 20.0, "type": "expense", "category": "Food",
               "description": "lunch", "client_token": tok}

    # Make the endpoint blow up AFTER _insert_transaction has committed the row
    # (this is a genuine commit-then-lost-response, not a throw-before-commit).
    real_snapshot = main.get_finance_snapshot
    calls = {"n": 0}

    def boom(user_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated: response lost after commit")
        return real_snapshot(user_id)

    monkeypatch.setattr(main, "get_finance_snapshot", boom)

    # The client never receives a success — from its side this is exactly a
    # "lost response after the server did its work" outcome.
    with pytest.raises(RuntimeError):
        tc.post("/api/transactions", json=payload)
    monkeypatch.setattr(main, "get_finance_snapshot", real_snapshot)

    # DB truth: the transaction DID commit, exactly once.
    assert _count_tx(uid) == 1

    # Recovery lookup confirms it.
    s = tc.post("/api/idempotency/status", json={"client_token": tok})
    assert s.json() == {"found": True, "kind": "transaction"}

    # And a retry with the SAME token + payload replays, never duplicates.
    r2 = tc.post("/api/transactions", json=payload)
    assert r2.status_code == 200
    assert r2.json().get("idempotent") is True
    assert _count_tx(uid) == 1           # still one — no double Finance effect


# --------------------------------------------------------------------------- #
# the client gave up early (its deadline expired) but the backend keeps running
# and commits afterwards -> the pending token must STILL be recoverable.
# A found:false at reconcile time does not mean "never committed".
# --------------------------------------------------------------------------- #
def test_client_gave_up_early_then_backend_commits_is_still_recoverable(alice):
    tc, user = alice
    uid = user["id"]
    tok = "tok-late-commit-1"

    # T0: the client's request is in flight; a reconcile at this instant sees
    # nothing (the row is not written yet). The client must NOT drop the token.
    assert tc.post("/api/idempotency/status", json={"client_token": tok}).json() == {
        "found": False,
    }

    # T1: the backend work completes and commits (response was lost to the
    # client). Modelled by the real create landing after that first check.
    assert tc.post("/api/transactions",
                   json={"amount": 7.0, "client_token": tok}).status_code == 201

    # T2: a later reconcile (client reloaded) now recovers the real result.
    assert tc.post("/api/idempotency/status", json={"client_token": tok}).json() == {
        "found": True, "kind": "transaction",
    }
    assert _count_tx(uid) == 1


# --------------------------------------------------------------------------- #
# Scenario 1: commit -> response lost -> session becomes invalid -> the SAME
# user re-authenticates -> the committed create is STILL reconcilable, because
# _prior_create keys on user_id, not on the session.
# --------------------------------------------------------------------------- #
def test_same_user_reauth_after_session_loss_can_still_reconcile(alice, monkeypatch):
    tc, user = alice
    uid = user["id"]
    tok = "tok-reauth-recover-1"

    real_snapshot = main.get_finance_snapshot
    seen = {"n": 0}

    def boom(user_id):
        seen["n"] += 1
        if seen["n"] == 1:
            raise RuntimeError("simulated: response lost after commit")
        return real_snapshot(user_id)

    monkeypatch.setattr(main, "get_finance_snapshot", boom)
    with pytest.raises(RuntimeError):
        tc.post("/api/transactions",
                json={"amount": 15.0, "description": "x", "client_token": tok})
    monkeypatch.setattr(main, "get_finance_snapshot", real_snapshot)
    assert _count_tx(uid) == 1

    # session becomes invalid
    tc.post("/api/auth/logout")
    assert tc.post("/api/idempotency/status",
                   json={"client_token": tok}).status_code == 401

    # the SAME user signs back in -> new session, same user_id
    r = tc.post("/api/auth/login",
                json={"email": "alice@example.com", "password": "alicepass123"})
    assert r.status_code == 200

    # the committed create is recoverable again
    assert tc.post("/api/idempotency/status", json={"client_token": tok}).json() == {
        "found": True, "kind": "transaction",
    }
    assert _count_tx(uid) == 1


# --------------------------------------------------------------------------- #
# HTTP 409: the token already committed a create with a DIFFERENT payload.
# That is positive evidence the earlier attempt landed -> recovery state for it
# must survive the 409 (the frontend does NOT clear the pending record here).
# --------------------------------------------------------------------------- #
def test_409_on_same_token_different_payload_still_reveals_the_committed_row(alice):
    tc, user = alice
    uid = user["id"]
    tok = "tok-conflict-recover-1"

    # first attempt commits (payload A)
    assert tc.post("/api/transactions", json={
        "amount": 10.0, "description": "A", "client_token": tok,
    }).status_code == 201

    # retry reuses the token but the user edited the form (payload B) -> 409
    r = tc.post("/api/transactions", json={
        "amount": 99.0, "description": "B", "client_token": tok,
    })
    assert r.status_code == 409

    # the committed A is STILL findable by the token -> the client can reconcile
    assert tc.post("/api/idempotency/status", json={"client_token": tok}).json() == {
        "found": True, "kind": "transaction",
    }
    assert _count_tx(uid) == 1  # B never wrote anything


def test_409_on_mo_path_same_token_different_text(alice, mock_ai):
    from conftest import mo_propose, mo_confirm
    tc, _ = alice
    tok = "tok-omni-conflict-1"
    params = {"amount": 5.0, "type": "expense", "category": "Food",
              "description": "A", "date": "2026-09-03T12:00:00+08:00"}
    mock_ai("CREATE_TRANSACTION", parameters=params)
    p1 = mo_propose(tc, "spent 5 on food", tok)
    assert p1.status_code == 200 and p1.json()["kind"] == "proposal_required"
    c1 = mo_confirm(tc, p1.json()["proposal"]["proposal_id"])
    assert c1.json()["kind"] == "executed"

    # same token, materially different submission text -> the fingerprint (the
    # input text) differs -> a truthful 409, no second proposal, no mutation.
    mock_ai("CREATE_TRANSACTION", parameters={**params, "amount": 9.0, "description": "B"})
    r2 = mo_propose(tc, "spent 9 on food", tok)
    assert r2.status_code == 409

    # the confirmed create is still recoverable via the token (it was written
    # with the token as its idempotency key).
    assert tc.post("/api/idempotency/status", json={"client_token": tok}).json()["found"] is True


# --------------------------------------------------------------------------- #
# rate limit is a 429, distinct from any timeout
# --------------------------------------------------------------------------- #
def test_status_is_rate_limited(alice, monkeypatch):
    import security_config as sec
    import auth as auth_core
    monkeypatch.setattr(sec, "RL_IDEM_STATUS_MAX", 3)
    auth_core._attempts.clear()
    tc, _ = alice
    codes = [
        tc.post("/api/idempotency/status", json={"client_token": "x-abc"}).status_code
        for _ in range(6)
    ]
    assert 429 in codes
    # the 429 carries Retry-After (a real throttle, not a network condition)
    last = tc.post("/api/idempotency/status", json={"client_token": "x-abc"})
    if last.status_code == 429:
        assert "retry-after" in {k.lower() for k in last.headers}
