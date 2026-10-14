"""B2 — Mo human-in-the-loop: propose -> confirm/cancel -> execute exactly once.

Temp DB; the AI boundary is the `mock_ai` fixture (no real model call). These
lock in the security / exactly-once / ownership / expiry / recovery properties
of the new /api/process-input contract.
"""
import datetime

import db as db_layer
import main
import mo_proposals as mo
from conftest import new_authed_client, mo_propose, mo_confirm, mo_cancel, mo_pending, mo_run

TX = {"amount": 20, "type": "expense", "category": "Personal Care",
      "description": "shaving", "date": "2026-09-05T09:00:00+08:00"}


def _n(table):
    c = db_layer.get_connection()
    try:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        c.close()


# --------------------------------------------------------------------------- #
# A. Transaction proposal
# --------------------------------------------------------------------------- #
def test_A_transaction_proposal_confirm_refresh_and_exactly_once(alice, mock_ai):
    tc, _ = alice
    mock_ai("CREATE_TRANSACTION", parameters=TX)

    pr = mo_propose(tc, "I spent RM20 on shaving", "tok-A")
    j = pr.json()
    assert j["kind"] == "proposal_required"
    pid = j["proposal"]["proposal_id"]
    assert j["proposal"]["status"] == "pending"
    assert j["proposal"]["display"]["amount"] == "RM 20.00"
    assert j["proposal"]["display"]["category"] == "Personal Care"
    assert _n("transactions") == 0                       # (2) nothing before confirm

    # (3) refresh while pending -> the SAME proposal comes back, still pending
    p = mo_pending(tc).json()["proposal"]
    assert p and p["proposal_id"] == pid and p["status"] == "pending"

    # (4) confirm -> exactly one RM20 expense with truthful details
    c1 = mo_confirm(tc, pid).json()
    assert c1["kind"] == "executed"
    row = _one_tx()
    assert row["amount_sen"] == 2000 and row["category"] == "Personal Care" and row["description"] == "shaving"

    # (5) triple confirm / retry still one row
    for _ in range(3):
        assert mo_confirm(tc, pid).json()["kind"] == "executed"
    assert _n("transactions") == 1

    # (6) refresh shows executed truthfully
    p2 = mo_pending(tc).json()["proposal"]
    assert p2["status"] == "executed" and p2["result_kind"] == "transaction"

    # (7) Wallet shows the same transaction
    fin = tc.get("/api/finance").json()
    assert len(fin["transactions"]) == 1 and fin["transactions"][0]["amount_sen"] == 2000


def test_A8_logout_login_preserves_the_result(mock_ai):
    from fastapi.testclient import TestClient
    tc, _ = new_authed_client("hitlA8@example.com")
    mock_ai("CREATE_TRANSACTION", parameters=TX)
    _p, conf = mo_run(tc, "I spent RM20 on shaving", "tok-A8")
    assert conf.json()["kind"] == "executed"
    tc.post("/api/auth/logout")

    tc2 = TestClient(main.app)
    assert tc2.post("/api/auth/login",
                    json={"email": "hitlA8@example.com", "password": "password123"}).status_code == 200
    fin = tc2.get("/api/finance").json()
    assert len(fin["transactions"]) == 1 and fin["transactions"][0]["amount_sen"] == 2000


def _one_tx():
    c = db_layer.get_connection()
    try:
        return c.execute("SELECT amount_sen, category, description FROM transactions").fetchone()
    finally:
        c.close()


# --------------------------------------------------------------------------- #
# B. Cancellation
# --------------------------------------------------------------------------- #
def test_B_cancel_creates_nothing_and_cannot_be_confirmed(alice, mock_ai):
    tc, _ = alice
    mock_ai("CREATE_TRANSACTION", parameters=TX)
    pid = mo_propose(tc, "I spent RM20 on shaving", "tok-B").json()["proposal"]["proposal_id"]

    assert mo_cancel(tc, pid).json()["kind"] == "cancelled"
    assert _n("transactions") == 0

    # refresh does not resurrect it as pending
    assert mo_pending(tc).json()["proposal"] is None

    # confirming a cancelled proposal fails safely, still no row
    r = mo_confirm(tc, pid)
    assert r.status_code == 409
    assert _n("transactions") == 0

    # cancel is idempotent
    assert mo_cancel(tc, pid).json()["kind"] == "cancelled"


# --------------------------------------------------------------------------- #
# C. Task / event proposal
# --------------------------------------------------------------------------- #
def test_C_event_proposal_confirm_shows_in_plan_no_duplicate(alice, mock_ai):
    tc, _ = alice
    start = "2026-09-06T15:00:00+08:00"
    mock_ai("CREATE_EVENT", parameters={"title": "Gym", "start": start, "end": None,
                                        "location": None, "all_day": False})
    pid = mo_propose(tc, "add gym on saturday 3pm", "tok-C").json()["proposal"]["proposal_id"]
    assert len(tc.get("/api/schedule").json()["all_events"]) == 0

    assert mo_confirm(tc, pid).json()["kind"] == "executed"
    assert mo_confirm(tc, pid).json()["kind"] == "executed"   # duplicate confirm
    evs = tc.get("/api/schedule").json()["all_events"]
    assert len(evs) == 1 and evs[0]["title"] == "Gym"


def test_C_task_proposal_confirm(alice, mock_ai):
    tc, _ = alice
    mock_ai("CREATE_TASK", parameters={"title": "Email Dr Tan", "due_date": None, "reminder_at": None})
    pid = mo_propose(tc, "remind me to email dr tan", "tok-Ct").json()["proposal"]["proposal_id"]
    assert _n("tasks") == 0
    mo_confirm(tc, pid)
    assert _n("tasks") == 1


# --------------------------------------------------------------------------- #
# D. Ownership / IDOR
# --------------------------------------------------------------------------- #
def test_D_user_B_cannot_touch_user_A_proposal(alice, bob, mock_ai):
    ac, _a = alice
    bc, _b = bob
    mock_ai("CREATE_TRANSACTION", parameters=TX)
    pid = mo_propose(ac, "I spent RM20 on shaving", "tok-D").json()["proposal"]["proposal_id"]

    # B: read (there is no read endpoint, but /pending must never show A's)
    assert mo_pending(bc).json()["proposal"] is None
    # B: confirm / cancel -> the SAME 404 as a nonexistent id (no existence oracle)
    assert mo_confirm(bc, pid).status_code == 404
    assert mo_cancel(bc, pid).status_code == 404
    assert mo_confirm(bc, "prop_deadbeef" * 4).status_code == 404

    # A's proposal is untouched and still confirmable
    assert mo_confirm(ac, pid).json()["kind"] == "executed"
    assert _n("transactions") == 1


# --------------------------------------------------------------------------- #
# E. Failure injection
# --------------------------------------------------------------------------- #
def test_E_ai_parse_failure_makes_no_proposal_no_row(alice, monkeypatch):
    tc, _ = alice

    def boom(*_a, **_k):
        raise ValueError("MALFORMED_AI_OUTPUT")

    monkeypatch.setattr(main.AIService, "interpret", staticmethod(boom))
    r = tc.post("/api/process-input", json={"user_input": "spent something on stuff"})
    assert r.status_code == 200 and r.json()["kind"] == "error"
    assert _n("mo_proposals") == 0 and _n("transactions") == 0


def test_E_domain_insert_failure_does_not_mark_executed(alice, mock_ai, monkeypatch):
    tc, _ = alice
    mock_ai("CREATE_TRANSACTION", parameters=TX)
    pid = mo_propose(tc, "I spent RM20 on shaving", "tok-E").json()["proposal"]["proposal_id"]

    def fail_insert(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(main, "_insert_transaction", fail_insert)
    r = mo_confirm(tc, pid).json()
    assert r["kind"] == "failed" and r.get("retryable") is True
    assert _n("transactions") == 0
    row = main._mo_get_owned(pid, _uid(tc))
    assert row["status"] == "failed"

    # recovery: with the insert working again, Confirm succeeds -> one row
    monkeypatch.undo()
    assert mo_confirm(tc, pid).json()["kind"] == "executed"
    assert _n("transactions") == 1


def test_E_post_commit_response_loss_reconciles_not_re_executes(alice, mock_ai):
    tc, _ = alice
    mock_ai("CREATE_TRANSACTION", parameters=TX)
    pid = mo_propose(tc, "I spent RM20 on shaving", "tok-E2").json()["proposal"]["proposal_id"]
    mo_confirm(tc, pid)                          # committed; "response lost"
    assert _n("transactions") == 1
    # the client retries confirm AND separately reconciles via /pending
    assert mo_confirm(tc, pid).json()["kind"] == "executed"
    assert mo_pending(tc).json()["proposal"]["status"] == "executed"
    assert _n("transactions") == 1


def test_E_token_reused_with_different_input_is_safe_conflict(alice, mock_ai):
    tc, _ = alice
    mock_ai("CREATE_TRANSACTION", parameters=TX)
    mo_propose(tc, "I spent RM20 on shaving", "tok-E3")
    r = mo_propose(tc, "I spent RM99 on something else", "tok-E3")
    assert r.status_code == 409
    assert _n("mo_proposals") == 1 and _n("transactions") == 0


# --------------------------------------------------------------------------- #
# expiry
# --------------------------------------------------------------------------- #
def test_expired_proposal_cannot_execute(alice, mock_ai, monkeypatch):
    tc, _ = alice
    mock_ai("CREATE_TRANSACTION", parameters=TX)
    pid = mo_propose(tc, "I spent RM20 on shaving", "tok-exp").json()["proposal"]["proposal_id"]

    # fast-forward past the TTL by rewriting expires_at in the past
    c = db_layer.get_connection()
    past = (datetime.datetime.now().astimezone() - datetime.timedelta(hours=1)).isoformat()
    c.execute("UPDATE mo_proposals SET expires_at = ? WHERE id = ?", (past, pid))
    c.commit()
    c.close()

    r = mo_confirm(tc, pid)
    assert r.status_code == 409
    assert _n("transactions") == 0
    # /pending no longer offers it
    assert mo_pending(tc).json()["proposal"] is None
    # and its stored status is now 'expired'
    assert main._mo_get_owned(pid, _uid(tc))["status"] == "expired"


# --------------------------------------------------------------------------- #
# bounded executed-visibility policy
# --------------------------------------------------------------------------- #
def test_old_executed_result_stops_dominating_pending(alice, mock_ai):
    tc, _ = alice
    mock_ai("CREATE_TRANSACTION", parameters=TX)
    _p, conf = mo_run(tc, "I spent RM20 on shaving", "tok-vis")
    assert conf.json()["kind"] == "executed"
    pid = conf.json()["proposal"]["proposal_id"]

    assert mo_pending(tc).json()["proposal"]["status"] == "executed"   # visible now
    # age the confirmation past the window
    c = db_layer.get_connection()
    old = (datetime.datetime.now().astimezone()
           - datetime.timedelta(seconds=mo.EXECUTED_VISIBLE_SECONDS + 60)).isoformat()
    c.execute("UPDATE mo_proposals SET confirmed_at = ? WHERE id = ?", (old, pid))
    c.commit()
    c.close()
    assert mo_pending(tc).json()["proposal"] is None                   # no longer surfaced
    # the transaction itself is of course still there
    assert _n("transactions") == 1


# --------------------------------------------------------------------------- #
# F. shared surface — one contract for Today and /mo
# --------------------------------------------------------------------------- #
def test_F_answer_kind_is_read_only_everywhere(alice, mock_ai):
    tc, _ = alice
    mock_ai("GET_SPENDING_SUMMARY")
    j = tc.post("/api/process-input", json={"user_input": "how much have I spent"}).json()
    assert j["kind"] == "answer"
    assert all(s["route"] for s in j["suggested_actions"])            # every suggestion has a real route
    assert _n("mo_proposals") == 0


def test_F_suggested_actions_are_all_from_the_registry(alice, mock_ai):
    tc, _ = alice
    mock_ai("ACTION_NOT_SUPPORTED")
    j = tc.post("/api/process-input", json={"user_input": "book me a flight"}).json()
    assert j["kind"] == "unsupported"
    routes = {"/", "/schedule", "/finance", "/activity"}
    assert j["suggested_actions"] and all(s["route"] in routes for s in j["suggested_actions"])
    assert all(s["key"] in mo.SUGGESTION_REGISTRY for s in j["suggested_actions"])


def _uid(tc):
    return tc.get("/api/auth/me").json()["user"]["id"]
