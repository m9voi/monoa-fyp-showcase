"""Phase 5H-B3 — create-once idempotency (finding M2).

A user-intended CREATE (transaction / task / generic schedule event) is applied
AT MOST ONCE when the official client retries the same logical submission with
the same `client_token`. A token reused with a materially different payload is a
truthful HTTP 409. The DATABASE (nullable client_token column + partial unique
index) is the authority.

Temp DB; no external calls.
"""
import sqlite3

import pytest

import db as db_layer
import idempotency as idem
import main
from conftest import new_authed_client

TOK = "11111111-2222-4333-8444-555555555555"      # crypto.randomUUID() shape


def _iso(h=0):
    import datetime
    MYT = datetime.timezone(datetime.timedelta(hours=8))
    return (datetime.datetime.now(MYT).replace(microsecond=0)
            + datetime.timedelta(hours=h)).isoformat()


def _tx_body(**over):
    b = {"amount": 20.0, "type": "expense", "category": "Food",
         "description": "lunch", "date": _iso(0)}
    b.update(over)
    return b


def _task_body(**over):
    b = {"title": "Finish slides", "notes": "for FYP"}
    b.update(over)
    return b


def _event_body(**over):
    b = {"title": "Study block", "start": _iso(24), "end": _iso(25)}
    b.update(over)
    return b


def _count(user_email_client, table):
    conn = db_layer.get_connection()
    try:
        # count for whichever single user exists in this fresh test DB
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


# ============================ idempotency.py unit ============================
@pytest.mark.parametrize("raw,expect", [
    (None, None), ("", None), ("   ", None),
    (TOK, TOK),
    ("a", "a"), ("shared", "shared"), ("tok-abc-123", "tok-abc-123"),
    ("ct-1725360000000-abc123def4", "ct-1725360000000-abc123def4"),
])
def test_normalize_token_accepts(raw, expect):
    assert idem.normalize_token(raw) == expect


@pytest.mark.parametrize("raw", ["a b", "x" * 65, "ba\nd", "ba\td", "emoji😀",
                                 "semi;colon", "slash/x", 123, {"x": 1}])
def test_normalize_token_rejects(raw):
    with pytest.raises(ValueError):
        idem.normalize_token(raw)


def test_request_fingerprint_is_stable_and_order_independent():
    a = idem.request_fingerprint({"amount_sen": 2000, "type": "expense", "category": "Food"})
    b = idem.request_fingerprint({"category": "Food", "type": "expense", "amount_sen": 2000})
    assert a == b and len(a) == 32
    assert a != idem.request_fingerprint({"amount_sen": 2001, "type": "expense", "category": "Food"})


# ============================ transactions ============================
def test_tx_new_token_creates_one_row():
    tc, _ = new_authed_client("idtx1@example.com")
    r = tc.post("/api/transactions", json=_tx_body(client_token=TOK))
    assert r.status_code == 201
    assert _count(tc, "transactions") == 1


def test_tx_same_token_retry_returns_same_row_once():
    tc, _ = new_authed_client("idtx2@example.com")
    # A real retry re-sends the IDENTICAL submission — build the body once so the
    # (now()-based) `date` field can't differ by a second between the two calls.
    body = _tx_body(client_token=TOK)
    r1 = tc.post("/api/transactions", json=body)
    r2 = tc.post("/api/transactions", json=body)
    assert r1.status_code == 201 and r2.status_code == 200
    assert r2.json().get("idempotent") is True
    assert r1.json()["transaction"]["id"] == r2.json()["transaction"]["id"]
    assert _count(tc, "transactions") == 1


def test_tx_different_token_creates_second_row():
    tc, _ = new_authed_client("idtx3@example.com")
    tc.post("/api/transactions", json=_tx_body(client_token="tok-a"))
    tc.post("/api/transactions", json=_tx_body(client_token="tok-b"))
    assert _count(tc, "transactions") == 2


def test_tx_no_token_is_not_deduplicated():
    tc, _ = new_authed_client("idtx4@example.com")
    tc.post("/api/transactions", json=_tx_body())
    tc.post("/api/transactions", json=_tx_body())
    assert _count(tc, "transactions") == 2


def test_tx_token_is_per_user():
    tc_a, _ = new_authed_client("idtxA@example.com")
    tc_b, _ = new_authed_client("idtxB@example.com")
    ra = tc_a.post("/api/transactions", json=_tx_body(client_token="shared-tok"))
    rb = tc_b.post("/api/transactions", json=_tx_body(client_token="shared-tok"))
    assert ra.status_code == 201 and rb.status_code == 201     # not each other's
    assert ra.json()["transaction"]["id"] != rb.json()["transaction"]["id"]
    assert _count(tc_a, "transactions") == 2                   # one per user, total 2


def test_tx_same_token_different_payload_is_409():
    tc, _ = new_authed_client("idtx5@example.com")
    tc.post("/api/transactions", json=_tx_body(amount=20.0, category="Food", client_token=TOK))
    r = tc.post("/api/transactions", json=_tx_body(amount=500.0, category="Shopping", client_token=TOK))
    assert r.status_code == 409
    body = r.json()
    assert body == {"detail": "This form was already submitted with different details. "
                              "Refresh the page and try again."}
    # no existing-row detail leaked, no second row
    for bad in ("20", "500", "Food", "Shopping", "amount", TOK):
        assert bad not in body["detail"]
    assert _count(tc, "transactions") == 1


def test_tx_retry_does_not_double_count_finance():
    tc, _ = new_authed_client("idtx6@example.com")
    body = _tx_body(amount=30.0, type="expense", client_token=TOK)   # identical retry
    r1 = tc.post("/api/transactions", json=body)
    snap1 = r1.json()["snapshot"]
    r2 = tc.post("/api/transactions", json=body)
    snap2 = r2.json()["snapshot"]
    assert snap1["month_spending"] == snap2["month_spending"]         # counted once
    fin = tc.get("/api/finance").json()
    assert len(fin["transactions"]) == 1
    assert fin["snapshot"]["month_spending"] == snap1["month_spending"]
    # refresh preserves it
    assert tc.get("/api/finance-summary").json()["month_spending"] == snap1["month_spending"]


def test_tx_retry_after_clock_moves_still_matches(monkeypatch):
    # date defaulted server-side must NOT enter the fingerprint: a retry seconds
    # later (different server 'now') is still a replay, not a 409.
    tc, _ = new_authed_client("idtx7@example.com")
    body = {"amount": 12.0, "type": "expense", "category": "Food",
            "description": "no explicit date", "client_token": TOK}   # <-- no `date`
    r1 = tc.post("/api/transactions", json=body)
    seq = ["2026-09-03T10:00:00+08:00", "2026-09-03T10:00:05+08:00"]
    monkeypatch.setattr(main.fin, "myt_now",
                        lambda: __import__("datetime").datetime.fromisoformat(seq.pop(0)))
    r2 = tc.post("/api/transactions", json=body)
    assert r1.status_code == 201 and r2.status_code == 200
    assert r2.json().get("idempotent") is True
    assert _count(tc, "transactions") == 1


# ============================ tasks ============================
def test_task_same_token_retry_one_row():
    tc, _ = new_authed_client("idtk1@example.com")
    r1 = tc.post("/api/tasks", json=_task_body(client_token=TOK))
    r2 = tc.post("/api/tasks", json=_task_body(client_token=TOK))
    assert r1.status_code == 201 and r2.status_code == 200 and r2.json()["idempotent"] is True
    assert r1.json()["task"]["id"] == r2.json()["task"]["id"]
    sched = tc.get("/api/schedule").json()
    assert len([t for t in sched["tasks"]]) == 1


def test_task_different_token_two_rows():
    tc, _ = new_authed_client("idtk2@example.com")
    tc.post("/api/tasks", json=_task_body(client_token="a"))
    tc.post("/api/tasks", json=_task_body(client_token="b"))
    assert _count(tc, "tasks") == 2


def test_task_same_token_different_payload_409():
    tc, _ = new_authed_client("idtk3@example.com")
    tc.post("/api/tasks", json=_task_body(title="Task one", client_token=TOK))
    r = tc.post("/api/tasks", json=_task_body(title="Totally different task", client_token=TOK))
    assert r.status_code == 409
    assert _count(tc, "tasks") == 1


def test_task_unrelated_mutations_unchanged():
    tc, _ = new_authed_client("idtk4@example.com")
    tid = tc.post("/api/tasks", json=_task_body(client_token=TOK)).json()["task"]["id"]
    assert tc.patch(f"/api/tasks/{tid}", json={"title": "renamed"}).status_code == 200
    assert tc.post(f"/api/tasks/{tid}/complete").status_code == 200
    assert tc.post(f"/api/tasks/{tid}/uncomplete").status_code == 200
    assert tc.delete(f"/api/tasks/{tid}").status_code == 200
    assert _count(tc, "tasks") == 0


# ============================ generic events ============================
def test_event_generic_retry_one_row():
    tc, _ = new_authed_client("idev1@example.com")
    body = _event_body(client_token=TOK)                             # identical retry
    r1 = tc.post("/api/events", json=body)
    r2 = tc.post("/api/events", json=body)
    assert r1.status_code == 201 and r2.status_code == 200 and r2.json()["idempotent"] is True
    assert len(tc.get("/api/schedule").json()["all_events"]) == 1


def test_event_same_token_different_payload_409():
    tc, _ = new_authed_client("idev2@example.com")
    tc.post("/api/events", json=_event_body(title="Study block", client_token=TOK))
    r = tc.post("/api/events", json=_event_body(title="Gym session", client_token=TOK))
    assert r.status_code == 409
    assert len(tc.get("/api/schedule").json()["all_events"]) == 1


def test_event_5g_map_bridge_idempotency_still_holds():
    # a linked-place create + retry (the Phase 5G contract) is unchanged
    tc, _ = new_authed_client("idev3@example.com")
    body = {"title": "Cycling", "start": _iso(24), "end": _iso(25),
            "client_token": TOK,
            "place": {"provider": "google", "provider_place_id": "ChIJ_velodrom_01",
                      "name": "Velodrom Nasional", "latitude": 2.93, "longitude": 101.68}}
    r1 = tc.post("/api/events", json=body)
    r2 = tc.post("/api/events", json=body)
    assert r1.status_code == 201 and r2.status_code == 200 and r2.json()["idempotent"] is True
    ev = r2.json()["event"]
    assert ev["place"]["provider_place_id"] == "ChIJ_velodrom_01"
    assert len(tc.get("/api/schedule").json()["all_events"]) == 1


def test_malformed_token_is_422_no_row():
    tc, _ = new_authed_client("idev4@example.com")
    r = tc.post("/api/events", json=_event_body(client_token="bad token with spaces"))
    assert r.status_code == 422
    assert len(tc.get("/api/schedule").json()["all_events"]) == 0


# ============================ concurrency / lost-response ============================
def test_prior_committed_by_race_is_replayed_not_duplicated():
    # simulate: a concurrent identical request already committed the row before
    # this one runs -> this endpoint's pre-check finds it and replays.
    # (explicit date so the two bodies are byte-identical even across a second
    #  tick — the fingerprint must match for a replay, not a 409).
    tc, _ = new_authed_client("idrace1@example.com")
    body = _tx_body(client_token=TOK, date="2026-09-03T12:00:00+08:00")
    r1 = tc.post("/api/transactions", json=body)
    first_id = r1.json()["transaction"]["id"]
    # "the client never saw r1's response" -> it retries with the same token
    r2 = tc.post("/api/transactions", json=body)
    assert r2.status_code == 200 and r2.json()["transaction"]["id"] == first_id
    assert _count(tc, "transactions") == 1


def test_integrityerror_branch_replays(monkeypatch):
    # force the pre-check to miss so the INSERT hits the unique index; the
    # except-IntegrityError branch must resolve to the existing row, one row.
    tc, _ = new_authed_client("idrace2@example.com")
    tc.post("/api/tasks", json=_task_body(client_token=TOK))          # row now exists

    real = main._prior_create
    calls = {"n": 0}

    def flaky(table, user_id, token):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(table, user_id, token)   # miss once

    monkeypatch.setattr(main, "_prior_create", flaky)
    r = tc.post("/api/tasks", json=_task_body(client_token=TOK))
    assert r.status_code == 200 and r.json().get("idempotent") is True
    assert calls["n"] >= 2                                            # pre-check + post-IntegrityError
    assert _count(tc, "tasks") == 1


def test_legacy_row_without_hash_replays_not_conflicts():
    # a row created before 0011 has client_request_hash NULL; a retry with the
    # same token must replay (return it), never 409.
    tc, u = new_authed_client("idlegacy@example.com")
    conn = db_layer.get_connection()
    try:
        conn.execute(
            "INSERT INTO tasks (id, user_id, title, is_completed, reminder_status, "
            "created_at, updated_at, client_token, client_request_hash) "
            "VALUES ('legacy1', ?, 'Legacy task', 0, 'NONE', ?, ?, ?, NULL)",
            (u["id"], _iso(0), _iso(0), TOK),
        )
        conn.commit()
    finally:
        conn.close()
    r = tc.post("/api/tasks", json=_task_body(title="anything", client_token=TOK))
    assert r.status_code == 200 and r.json()["task"]["id"] == "legacy1"
    assert _count(tc, "tasks") == 1


# ============================ DB constraint / migration ============================
def test_unique_indexes_exist_on_all_three_tables():
    conn = db_layer.get_connection()
    try:
        def idxs(t):
            return {r[1] for r in conn.execute(f"PRAGMA index_list({t})")}
        assert "idx_transactions_user_client_token" in idxs("transactions")
        assert "idx_tasks_user_client_token" in idxs("tasks")
        assert "idx_schedule_events_user_client_token" in idxs("schedule_events")
    finally:
        conn.close()


def test_db_rejects_a_raw_duplicate_token_per_user():
    tc, u = new_authed_client("iddb1@example.com")
    conn = db_layer.get_connection()
    try:
        conn.execute("INSERT INTO tasks (id, user_id, title, is_completed, reminder_status, "
                     "created_at, updated_at, client_token) VALUES ('a1', ?, 't', 0, 'NONE', ?, ?, 'dup')",
                     (u["id"], _iso(0), _iso(0)))
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO tasks (id, user_id, title, is_completed, reminder_status, "
                         "created_at, updated_at, client_token) VALUES ('a2', ?, 't', 0, 'NONE', ?, ?, 'dup')",
                         (u["id"], _iso(0), _iso(0)))
            conn.commit()
    finally:
        conn.close()


def test_head_is_single_and_current():
    import subprocess, sys, os
    root = os.path.dirname(os.path.dirname(__file__))
    heads = subprocess.run([sys.executable, "-m", "alembic", "heads"],
                           cwd=root, capture_output=True, text=True).stdout
    assert heads.count("(head)") == 1
    assert "0020_user_interests" in heads


# ====== Mo human-in-the-loop (/api/process-input) exactly-once (B2) ======
# process-input now UNDERSTANDS only; a supported mutating request returns a
# server-owned PENDING proposal and creates NO domain row. .../{id}/confirm
# executes it exactly once through the canonical service; repeated confirm /
# retry / a mid-confirm DB blip still leaves exactly one row.
from conftest import mo_propose, mo_confirm, mo_run  # noqa: E402

_TX_PARAMS = {"amount": 20, "type": "expense", "category": "Food",
              "description": "lunch", "date": "2026-09-03T12:00:00+08:00"}


def test_ai_proposal_creates_no_row_confirm_creates_exactly_one(alice, mock_ai):
    tc, _ = alice
    mock_ai("CREATE_TRANSACTION", parameters=_TX_PARAMS)
    pr = mo_propose(tc, "I spent RM20 on food", TOK)
    assert pr.json()["kind"] == "proposal_required"
    assert _count(tc, "transactions") == 0                       # nothing yet
    pid = pr.json()["proposal"]["proposal_id"]
    c1 = mo_confirm(tc, pid)
    assert c1.json()["kind"] == "executed"
    assert _count(tc, "transactions") == 1
    # the response to confirm was "lost" -> the client confirms again
    c2 = mo_confirm(tc, pid)
    assert c2.json()["kind"] == "executed"
    assert _count(tc, "transactions") == 1                       # still one
    assert len(tc.get("/api/finance").json()["transactions"]) == 1


def test_ai_triple_confirm_same_tick_is_one_row(alice, mock_ai):
    tc, _ = alice
    mock_ai("CREATE_TRANSACTION", parameters=_TX_PARAMS)
    pid = mo_propose(tc, "spent rm20 food", TOK).json()["proposal"]["proposal_id"]
    for _ in range(3):
        assert mo_confirm(tc, pid).json()["kind"] == "executed"
    assert _count(tc, "transactions") == 1


def test_ai_two_separate_proposals_without_token_are_not_merged(alice, mock_ai):
    tc, _ = alice
    mock_ai("CREATE_TRANSACTION", parameters=_TX_PARAMS)
    _p1, c1 = mo_run(tc, "I spent RM20 on food")
    _p2, c2 = mo_run(tc, "I spent RM20 on food")
    assert c1.json()["kind"] == "executed" and c2.json()["kind"] == "executed"
    assert _count(tc, "transactions") == 2      # two deliberate confirmations = two rows


def test_ai_same_token_different_text_is_409_no_mutation(alice, mock_ai):
    tc, _ = alice
    mock_ai("CREATE_TRANSACTION", parameters=_TX_PARAMS)
    mo_propose(tc, "spent rm20 food", TOK)
    r = mo_propose(tc, "a completely different command", TOK)
    assert r.status_code == 409
    assert r.json()["detail"].startswith("This form was already submitted")
    assert _count(tc, "transactions") == 0      # nothing executed, one pending proposal


def test_ai_reminder_confirm_once(alice, mock_ai):
    tc, _ = alice
    mock_ai("CREATE_REMINDER", parameters={"title": "Submit my assignment",
            "due_date": None, "reminder_at": "2026-09-04T10:00:00+08:00"})
    pid = mo_propose(tc, "Remind me to submit my assignment tomorrow", TOK).json()["proposal"]["proposal_id"]
    assert _count(tc, "tasks") == 0
    mo_confirm(tc, pid); mo_confirm(tc, pid)
    assert _count(tc, "tasks") == 1


def test_ai_task_confirm_once(alice, mock_ai):
    tc, _ = alice
    mock_ai("CREATE_TASK", parameters={"title": "Work on FYP", "due_date": None, "reminder_at": None})
    pid = mo_propose(tc, "I need to work on my FYP", TOK).json()["proposal"]["proposal_id"]
    assert _count(tc, "tasks") == 0
    mo_confirm(tc, pid); mo_confirm(tc, pid)
    assert _count(tc, "tasks") == 1


def test_ai_event_confirm_once(alice, mock_ai):
    tc, _ = alice
    mock_ai("CREATE_EVENT", parameters={"title": "Dentist", "start": "2026-09-04T15:00:00+08:00",
            "end": None, "location": None, "all_day": False})
    pid = mo_propose(tc, "Dentist appointment tomorrow 3pm", TOK).json()["proposal"]["proposal_id"]
    assert len(tc.get("/api/schedule").json()["all_events"]) == 0
    mo_confirm(tc, pid); mo_confirm(tc, pid)
    assert len(tc.get("/api/schedule").json()["all_events"]) == 1


def test_ai_malformed_token_is_422_no_proposal_no_mutation(alice, mock_ai):
    tc, _ = alice
    mock_ai("CREATE_TRANSACTION", parameters=_TX_PARAMS)
    r = mo_propose(tc, "x", "bad token here")
    assert r.status_code == 422
    assert _count(tc, "transactions") == 0
    assert _count(tc, "mo_proposals") == 0


def test_ai_confirm_replays_through_canonical_prior_create(alice, mock_ai, monkeypatch):
    # A mid-confirm DB blip that makes the canonical service's first _prior_create
    # look up empty must still resolve to exactly one row (its replay path).
    tc, _ = alice
    mock_ai("CREATE_TRANSACTION", parameters=_TX_PARAMS)
    pid = mo_propose(tc, "spent rm20 food", TOK).json()["proposal"]["proposal_id"]
    mo_confirm(tc, pid)                                   # row now exists
    assert _count(tc, "transactions") == 1

    real = main._prior_create
    calls = {"n": 0}

    def flaky(table, user_id, tok):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(table, user_id, tok)

    monkeypatch.setattr(main, "_prior_create", flaky)
    c = mo_confirm(tc, pid)
    assert c.json()["kind"] == "executed"
    assert _count(tc, "transactions") == 1                # never a second row


def test_ai_clarification_creates_no_proposal_and_frees_the_token(alice, mock_ai):
    tc, _ = alice
    mock_ai("CLARIFICATION_REQUIRED", clarification="What did you spend it on?")
    r1 = mo_propose(tc, "I spent RM20", TOK)
    assert r1.json()["kind"] == "clarification_required"
    assert _count(tc, "transactions") == 0
    assert _count(tc, "mo_proposals") == 0                # no proposal row was written
    # the same token now carries a real command -> a proposal, then one row
    mock_ai("CREATE_TRANSACTION", parameters=_TX_PARAMS)
    _p, c2 = mo_run(tc, "I spent RM20 on food", TOK)
    assert c2.json()["kind"] == "executed"
    assert _count(tc, "transactions") == 1


# ============ legacy pre-5H-B3 tokened schedule_events (NULL hash) ============
def _seed_legacy_event(user_id, token, *, title="KLCC Park"):
    start, end = "2026-09-04T15:00:00+08:00", "2026-09-04T16:00:00+08:00"
    conn = db_layer.get_connection()
    try:
        conn.execute(
            "INSERT INTO schedule_events "
            "(id, user_id, title, location, start_time, end_time, all_day, status, "
            " source_type, recurrence, created_at, updated_at, "
            " place_provider, place_provider_id, place_lat, place_lng, place_address, "
            " client_token, client_request_hash) "
            "VALUES ('legev1', ?, ?, 'KLCC Park', ?, ?, 0, 'confirmed', 'coreos', 'none', ?, ?, "
            "'google', 'ChIJ_klcc', 3.1553, 101.7137, 'KLCC addr', ?, NULL)",
            (user_id, title, start, end, _iso(0), _iso(0), token),
        )
        conn.commit()
    finally:
        conn.close()
    return start, end


_LEG_PLACE = {"provider": "google", "provider_place_id": "ChIJ_klcc", "name": "KLCC Park",
              "address": "KLCC addr", "latitude": 3.1553, "longitude": 101.7137}


def test_legacy_null_hash_same_payload_replays():
    tc, u = new_authed_client("idleg1@example.com")
    start, end = _seed_legacy_event(u["id"], TOK)
    r = tc.post("/api/events", json={"title": "KLCC Park", "start": start, "end": end,
                                     "client_token": TOK, "place": _LEG_PLACE})
    assert r.status_code == 200 and r.json()["event"]["id"] == "legev1"
    assert len(tc.get("/api/schedule").json()["all_events"]) == 1


def test_legacy_null_hash_different_payload_is_409():
    # closes the gap: a legacy tokened row must NOT be returned as though a
    # materially different payload succeeded.
    tc, u = new_authed_client("idleg2@example.com")
    start, end = _seed_legacy_event(u["id"], TOK)
    r = tc.post("/api/events", json={"title": "SOMETHING ELSE ENTIRELY", "start": start, "end": end,
                                     "client_token": TOK, "place": _LEG_PLACE})
    assert r.status_code == 409
    assert len(tc.get("/api/schedule").json()["all_events"]) == 1


# ============ REAL concurrency (two independent execution contexts) ============
def test_concurrent_identical_transaction_creates_exactly_one_row():
    """Two genuinely concurrent POST /api/transactions with the same user, same
    client_token and identical payload -> exactly one persisted row, one caller
    201 and the other 200-replay, no sqlite lock error, no double Finance effect.

    Real overlap: two independent TestClient instances (separate httpx clients +
    ASGI portals) sharing one session cookie, run from two threads released by a
    Barrier. The shared contention point is the real SQLite file + the partial
    unique index. NOT monkeypatched.
    """
    import threading
    from fastapi.testclient import TestClient
    from conftest import new_authed_client as _mk

    tc, _ = _mk("idconc@example.com")
    cookie = tc.cookies.get(main.sec.COOKIE_NAME)
    assert cookie

    def a_client():
        c = TestClient(main.app)
        c.cookies.set(main.sec.COOKIE_NAME, cookie)
        return c

    ROUNDS = 6
    for i in range(ROUNDS):
        tok = f"race-tok-{i}"
        body = {"amount": 20.0, "type": "expense", "category": "Food",
                "description": f"race{i}", "date": "2026-09-03T12:00:00+08:00",
                "client_token": tok}
        c1, c2 = a_client(), a_client()
        out, errs = {}, {}
        barrier = threading.Barrier(2)

        def go(name, client):
            try:
                barrier.wait(timeout=5)
                r = client.post("/api/transactions", json=body)
                out[name] = (r.status_code, r.json())
            except Exception as e:                       # noqa: BLE001 - surface to main thread
                errs[name] = repr(e)

        t1 = threading.Thread(target=go, args=("A", c1))
        t2 = threading.Thread(target=go, args=("B", c2))
        t1.start(); t2.start(); t1.join(10); t2.join(10)

        assert not errs, f"round {i}: worker exception(s): {errs}"
        codes = sorted(v[0] for v in out.values())
        # one INSERT wins (201); the other loses the race and resolves through
        # the pre-check OR the IntegrityError branch (200 replay). Never 500,
        # never OperationalError, never two 201s.
        assert codes == [200, 201], f"round {i}: unexpected codes {codes}: {out}"
        ids = {out["A"][1]["transaction"]["id"], out["B"][1]["transaction"]["id"]}
        assert len(ids) == 1, f"round {i}: callers see different rows {ids}"
        for v in out.values():
            assert v[1]["status"] == "SUCCESS"

        conn = db_layer.get_connection()
        try:
            n = conn.execute("SELECT COUNT(*) FROM transactions WHERE client_token = ?",
                             (tok,)).fetchone()[0]
        finally:
            conn.close()
        assert n == 1, f"round {i}: {n} rows for one token"

    # no duplicate Finance effect: ROUNDS tokens -> ROUNDS transactions, not 2x
    assert _count(tc, "transactions") == ROUNDS
    fin = tc.get("/api/finance").json()
    seen = [t["id"] for t in fin["transactions"]]
    assert len(seen) == len(set(seen))                   # list has no dupes
