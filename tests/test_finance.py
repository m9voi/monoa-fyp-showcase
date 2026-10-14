"""Phase 3 — Finance system tests. Temp DB only, AI boundary mocked."""
import datetime
import sqlite3

import pytest

import main
import db as db_layer
import finance_core as fin
from conftest import new_authed_client


def _iso(offset_hours=0):
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=offset_hours)).astimezone().isoformat()


def _create(tc, **kw):
    body = {"amount": kw.get("amount", 10), "type": kw.get("type", "expense"),
            "category": kw.get("category", "Food"), "description": kw.get("description", "x")}
    if "date" in kw:
        body["date"] = kw["date"]
    return tc.post("/api/transactions", json=body)


# ============================ MONEY PRECISION ============================
@pytest.mark.parametrize("val,sen", [
    ("0.01", 1), ("0.10", 10), ("1.99", 199), ("19.90", 1990), ("999.99", 99999),
    (0.1, 10), (0.2, 20), ("2.675", 268), ("2.005", 201), ("100000000.00", 10_000_000_000),
])
def test_to_sen_exact(val, sen):
    assert fin.to_sen(val) == sen


def test_no_float_accumulation():
    assert fin.to_sen(0.1) + fin.to_sen(0.2) == 30
    assert fin.from_sen(30) == 0.3


def test_from_sen_never_negative_zero():
    assert repr(fin.from_sen(0)) == "0.0"
    assert fin.from_sen(-0) == 0.0


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), "abc", None, True, "1e400", "100000000.01"])
def test_bad_amounts_rejected(bad):
    with pytest.raises(fin.MoneyError):
        fin.to_sen(bad)


def test_create_rejects_bad_amount(alice):
    tc, _ = alice
    for bad in [0, -5, "abc", None]:
        r = tc.post("/api/transactions", json={"amount": bad, "type": "expense", "category": "Food"})
        assert r.status_code == 422, bad


def test_money_stored_as_sen_and_major(alice):
    tc, _ = alice
    r = _create(tc, amount="19.99")
    assert r.status_code == 201
    txid = r.json()["transaction"]["id"]
    c = db_layer.get_connection()
    row = c.execute("SELECT amount, amount_sen FROM transactions WHERE id=?", (txid,)).fetchone()
    c.close()
    assert row["amount_sen"] == 1999
    assert row["amount"] == 19.99


# ============================ CALCULATION SPEC ============================
def test_available_balance_income_and_expense(alice):
    tc, _ = alice
    tc.post("/api/finance/settings", json={"starting_balance": 1000})
    _create(tc, amount="100.00", type="expense")
    _create(tc, amount="30.50", type="income")
    _create(tc, amount="19.50", type="expense")
    snap = tc.get("/api/finance-summary").json()
    assert snap["starting_balance"] == 1000.0
    assert snap["total_income"] == 30.5
    assert snap["total_expenses"] == 119.5
    assert snap["available_balance"] == 911.0   # 1000 + 30.50 - 119.50


def test_category_totals_sum_to_month_spending(alice):
    tc, _ = alice
    _create(tc, amount="10.00", category="Food")
    _create(tc, amount="5.55", category="Transport")
    _create(tc, amount="4.45", category="food")   # normalizes to Food
    _create(tc, amount="7.00", type="income", category="Other")  # income excluded
    snap = tc.get("/api/finance-summary").json()
    assert snap["month_spending"] == 20.0
    total = round(sum(s["amount"] for s in snap["category_breakdown"]), 2)
    assert total == snap["month_spending"]
    cats = {s["category"]: s["amount"] for s in snap["category_breakdown"]}
    assert cats["Food"] == 14.45 and cats["Transport"] == 5.55
    assert round(sum(s["pct"] for s in snap["category_breakdown"]), 0) == 100


def test_period_boundaries_are_myt(alice):
    tc, _ = alice
    now = fin.myt_now()
    # yesterday 23:00 MYT -> must NOT count as "today" (the UTC-vs-MYT bug)
    y = (now - datetime.timedelta(days=1)).replace(hour=23, minute=0, second=0, microsecond=0)
    _create(tc, amount="50.00", date=y.isoformat())
    # today 00:30 MYT and today 12:00 MYT -> both count as "today"
    _create(tc, amount="12.00", date=now.replace(hour=0, minute=30, second=0, microsecond=0).isoformat())
    _create(tc, amount="8.00", date=now.replace(hour=12, minute=0, second=0, microsecond=0).isoformat())
    snap = tc.get("/api/finance-summary").json()
    assert snap["today_spending"] == 20.0          # 12 + 8, NOT 70
    assert snap["week_spending"] >= 20.0
    assert snap["total_expenses"] == 70.0          # all three counted in the all-time total


def test_budget_usage_and_over_budget(alice):
    tc, _ = alice
    tc.post("/api/finance/settings", json={"daily_budget": 20, "weekly_budget": 100, "monthly_budget": 500})
    _create(tc, amount="25.00")   # over daily
    snap = tc.get("/api/finance-summary").json()
    assert snap["daily_over_budget"] is True
    assert snap["daily_remaining"] == -5.0
    assert snap["daily_used_pct"] == 125.0
    assert snap["weekly_over_budget"] is False


def test_zero_budget_is_neutral(alice):
    tc, _ = alice
    _create(tc, amount="10.00")
    snap = tc.get("/api/finance-summary").json()
    assert snap["daily_budget"] == 0.0
    assert snap["daily_used_pct"] == 0.0
    assert snap["daily_over_budget"] is False


# ============================ CREATE ============================
def test_create_valid(alice):
    tc, _ = alice
    r = _create(tc, amount="12.34", category="Transport", description="  grab ride  ")
    assert r.status_code == 201
    tx = r.json()["transaction"]
    assert tx["amount"] == 12.34 and tx["category"] == "Transport"
    assert tx["description"] == "grab ride"        # trimmed
    assert "user_id" not in tx and "merchant" not in tx
    assert r.json()["snapshot"]["total_expenses"] == 12.34


def test_create_bad_category_falls_back_to_other(alice):
    tc, _ = alice
    r = _create(tc, amount="9.00", category="totally-made-up")
    assert r.status_code == 201 and r.json()["transaction"]["category"] == "Other"


def test_create_bad_type_falls_back_to_expense(alice):
    tc, _ = alice
    r = _create(tc, amount="9.00", type="banana")
    assert r.status_code == 201 and r.json()["transaction"]["type"] == "expense"


def test_create_bad_date_rejected(alice):
    tc, _ = alice
    r = _create(tc, amount="9.00", date="not-a-date")
    assert r.status_code == 422


def test_create_defaults_date_to_now(alice):
    tc, _ = alice
    r = _create(tc, amount="9.00")
    assert r.status_code == 201
    assert r.json()["transaction"]["date"].endswith("+08:00")


def test_create_long_description_rejected(alice):
    tc, _ = alice
    r = tc.post("/api/transactions", json={"amount": 5, "type": "expense", "category": "Food",
                                           "description": "x" * 200})
    assert r.status_code == 422


# ============================ EDIT ============================
def test_edit_valid_recalculates(alice):
    tc, _ = alice
    txid = _create(tc, amount="10.00", category="Food").json()["transaction"]["id"]
    r = tc.patch(f"/api/transactions/{txid}", json={"amount": "25.00", "category": "Bills"})
    assert r.status_code == 200
    assert r.json()["transaction"]["amount"] == 25.0
    assert r.json()["transaction"]["category"] == "Bills"
    assert r.json()["snapshot"]["total_expenses"] == 25.0
    cats = {s["category"]: s["amount"] for s in r.json()["snapshot"]["category_breakdown"]}
    assert cats == {"Bills": 25.0}


def test_edit_nonexistent_404(alice):
    tc, _ = alice
    assert tc.patch("/api/transactions/nope", json={"amount": 5}).status_code == 404


def test_edit_unauthorized_404(alice, bob):
    ac, _ = alice
    bc, _ = bob
    btx = _create(bc, amount="10.00").json()["transaction"]["id"]
    r = ac.patch(f"/api/transactions/{btx}", json={"amount": "999.00"})
    assert r.status_code == 404
    # bob's transaction unchanged
    assert bc.get("/api/finance-summary").json()["total_expenses"] == 10.0


def test_edit_empty_payload_422(alice):
    tc, _ = alice
    txid = _create(tc, amount="10.00").json()["transaction"]["id"]
    assert tc.patch(f"/api/transactions/{txid}", json={}).status_code == 422


# ============================ DELETE ============================
def test_delete_valid_recalculates(alice):
    tc, _ = alice
    txid = _create(tc, amount="40.00").json()["transaction"]["id"]
    _create(tc, amount="10.00")
    r = tc.delete(f"/api/transactions/{txid}")
    assert r.status_code == 200
    assert r.json()["snapshot"]["total_expenses"] == 10.0
    assert all(t["id"] != txid for t in tc.get("/api/finance/history").json()["transactions"])


def test_delete_nonexistent_404(alice):
    tc, _ = alice
    assert tc.delete("/api/transactions/nope").status_code == 404


def test_delete_unauthorized_404(alice, bob):
    ac, _ = alice
    bc, _ = bob
    btx = _create(bc, amount="10.00").json()["transaction"]["id"]
    assert ac.delete(f"/api/transactions/{btx}").status_code == 404
    assert bc.get("/api/finance-summary").json()["total_expenses"] == 10.0


# ============================ HISTORY ============================
def test_history_ordering_and_ownership(alice, bob):
    ac, _ = alice
    bc, _ = bob
    _create(bc, amount="99.00", description="bob")
    _create(ac, amount="1.00", date=_iso(-48), description="old")
    _create(ac, amount="2.00", date=_iso(-1), description="mid")
    _create(ac, amount="3.00", date=_iso(0), description="new")
    rows = ac.get("/api/finance/history").json()["transactions"]
    assert [r["description"] for r in rows] == ["new", "mid", "old"]
    assert all(r["description"] != "bob" for r in rows)


def test_history_empty(alice):
    tc, _ = alice
    assert tc.get("/api/finance/history").json()["transactions"] == []


def test_history_type_filter(alice):
    tc, _ = alice
    _create(tc, amount="10.00", type="expense")
    _create(tc, amount="5.00", type="income")
    exp = tc.get("/api/finance/history?type=expense").json()["transactions"]
    inc = tc.get("/api/finance/history?type=income").json()["transactions"]
    assert len(exp) == 1 and exp[0]["type"] == "expense"
    assert len(inc) == 1 and inc[0]["type"] == "income"


def test_history_category_filter(alice):
    tc, _ = alice
    _create(tc, amount="10.00", category="Food")
    _create(tc, amount="5.00", category="Transport")
    r = tc.get("/api/finance/history?category=Food").json()["transactions"]
    assert len(r) == 1 and r[0]["category"] == "Food"
    # normalization: lower-case filter still works
    r2 = tc.get("/api/finance/history?category=transport").json()["transactions"]
    assert len(r2) == 1 and r2[0]["category"] == "Transport"


# ============================ BUDGETS ============================
def test_budgets_persist_and_do_not_touch_balance_or_tx(alice):
    tc, _ = alice
    tc.post("/api/finance/settings", json={"starting_balance": 500})
    _create(tc, amount="20.00")
    tc.post("/api/finance/settings", json={"daily_budget": 30, "weekly_budget": 150, "monthly_budget": 600})
    s = tc.get("/api/finance/settings").json()
    assert (s["daily_budget"], s["weekly_budget"], s["monthly_budget"]) == (30.0, 150.0, 600.0)
    assert s["starting_balance"] == 500.0
    assert len(tc.get("/api/finance/history").json()["transactions"]) == 1


def test_settings_negative_rejected(alice):
    tc, _ = alice
    for k in ("starting_balance", "daily_budget", "weekly_budget", "monthly_budget"):
        assert tc.post("/api/finance/settings", json={k: -1}).status_code == 400


def test_settings_nan_rejected(alice):
    tc, _ = alice
    assert tc.post("/api/finance/settings", json={"starting_balance": "not-money"}).status_code == 422


# ============================ AUTHORIZATION / IDOR ============================
def test_transaction_endpoints_require_auth(client):
    assert client.post("/api/transactions", json={"amount": 5}).status_code == 401
    assert client.patch("/api/transactions/x", json={"amount": 5}).status_code == 401
    assert client.delete("/api/transactions/x").status_code == 401
    assert client.get("/api/finance/meta").status_code == 401


def test_body_user_id_ignored_on_create(alice, bob):
    ac, a = alice
    _, b = bob
    r = ac.post("/api/transactions", json={"amount": 7, "type": "expense", "category": "Food",
                                           "user_id": b["id"], "owner": b["id"]})
    assert r.status_code == 201
    c = db_layer.get_connection()
    owner = c.execute("SELECT user_id FROM transactions WHERE id=?", (r.json()["transaction"]["id"],)).fetchone()[0]
    c.close()
    assert owner == a["id"]


# ==================== Mo (proposal -> confirm) ====================
from conftest import mo_propose, mo_confirm, mo_run  # noqa: E402


def test_ai_proposal_then_confirm_creates_transaction_for_caller_only(alice, bob, mock_ai):
    ac, a = alice
    _, b = bob
    # the model even "returns" an injected owner — it must be ignored on BOTH
    # the proposal and the confirmed row; ownership is the session user.
    mock_ai("CREATE_TRANSACTION", parameters={
        "amount": 42.50, "type": "expense", "description": "ai lunch",
        "category": "Food", "date": _iso(), "user_id": b["id"], "owner": b["id"],
    })
    _p, conf = mo_run(ac, "spent 42.50 on lunch")
    assert conf.status_code == 200 and conf.json()["kind"] == "executed"
    c = db_layer.get_connection()
    row = c.execute("SELECT user_id, amount_sen FROM transactions WHERE description='ai lunch'").fetchone()
    bcount = c.execute("SELECT COUNT(*) FROM transactions WHERE user_id=?", (b["id"],)).fetchone()[0]
    c.close()
    assert row["user_id"] == a["id"] and row["amount_sen"] == 4250
    assert bcount == 0
    assert ac.get("/api/finance-summary").json()["total_expenses"] == 42.5


def test_ai_unknown_category_normalized_to_other_on_confirm(alice, mock_ai):
    tc, _ = alice
    mock_ai("CREATE_TRANSACTION", parameters={
        "amount": 8, "type": "expense", "description": "weird", "category": "Crypto-NFT", "date": _iso()})
    pr = mo_propose(tc, "spent 8 on weird")
    # the proposal already shows the normalised category — no surprise on confirm
    assert pr.json()["proposal"]["display"]["category"] == "Other"
    mo_confirm(tc, pr.json()["proposal"]["proposal_id"])
    c = db_layer.get_connection()
    cat = c.execute("SELECT category FROM transactions WHERE description='weird'").fetchone()[0]
    c.close()
    assert cat == "Other"


def test_ai_malformed_amount_makes_no_proposal_and_no_mutation(alice, mock_ai):
    tc, _ = alice
    mock_ai("CREATE_TRANSACTION", parameters={
        "amount": "abc", "type": "expense", "description": "bad", "category": "Food", "date": _iso()})
    r = tc.post("/api/process-input", json={"user_input": "spent abc"})
    assert r.status_code == 200 and r.json()["kind"] == "error"
    c = db_layer.get_connection()
    n = c.execute("SELECT COUNT(*) FROM mo_proposals").fetchone()[0]
    c.close()
    assert n == 0
    assert tc.get("/api/finance-summary").json()["total_expenses"] == 0.0


def test_ai_spending_summary_is_an_answer_from_the_authoritative_snapshot(alice, mock_ai):
    tc, _ = alice
    _create(tc, amount="15.00")
    mock_ai("GET_SPENDING_SUMMARY")
    r = tc.post("/api/process-input", json={"user_input": "how much did I spend"})
    j = r.json()
    assert j["kind"] == "answer" and "15.00" in j["message"]
    # an answer is read-only — it never produces a proposal
    c = db_layer.get_connection()
    assert c.execute("SELECT COUNT(*) FROM mo_proposals").fetchone()[0] == 0
    c.close()


# ============================ PERSISTENCE ROUND-TRIPS ============================
def test_create_edit_delete_persist_across_refetch(alice):
    tc, _ = alice
    txid = _create(tc, amount="10.00", category="Food").json()["transaction"]["id"]
    assert tc.get("/api/finance-summary").json()["total_expenses"] == 10.0

    tc.patch(f"/api/transactions/{txid}", json={"amount": "22.22"})
    assert tc.get("/api/finance-summary").json()["total_expenses"] == 22.22
    hist = tc.get("/api/finance/history").json()["transactions"]
    assert hist[0]["amount"] == 22.22

    tc.delete(f"/api/transactions/{txid}")
    assert tc.get("/api/finance-summary").json()["total_expenses"] == 0.0
    assert tc.get("/api/finance/history").json()["transactions"] == []


def test_finance_db_integrity_after_finance_ops(alice):
    tc, _ = alice
    tc.post("/api/finance/settings", json={"starting_balance": 100, "daily_budget": 10})
    txid = _create(tc, amount="3.33").json()["transaction"]["id"]
    tc.patch(f"/api/transactions/{txid}", json={"category": "Bills"})
    c = db_layer.get_connection()
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    c.close()


# ============================ MIGRATION 0007 ============================
def test_migration_0007_amount_sen_backfill(fresh_db_path):
    # fresh_db_path is already migrated to head; seed a legacy-style row w/o amount_sen
    c = sqlite3.connect(fresh_db_path)
    c.execute("INSERT INTO users (id,name,email,email_normalized,is_active,created_at,updated_at) "
              "VALUES ('u','u','u@e.com','u@e.com',1,'x','x')")
    c.execute("UPDATE transactions SET amount_sen = NULL")  # simulate pre-0007
    c.execute("INSERT INTO transactions (id,user_id,amount,category,date,type,created_at,updated_at) "
              "VALUES ('t1','u',12.34,'Food','2026-09-01T10:00:00+08:00','expense','x','x')")
    c.commit(); c.close()

    import subprocess, sys, os
    root = os.path.dirname(os.path.dirname(__file__))
    env = dict(os.environ, COREOS_DB_PATH=fresh_db_path)
    # re-run head (0007 backfill is guarded/idempotent)
    subprocess.run([sys.executable, "-m", "alembic", "stamp", "0006_authentication"],
                   cwd=root, env=env, capture_output=True)
    r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                       cwd=root, env=env, capture_output=True)
    assert r.returncode == 0, r.stderr

    c = sqlite3.connect(fresh_db_path)
    got = c.execute("SELECT amount_sen FROM transactions WHERE id='t1'").fetchone()[0]
    nulls = c.execute("SELECT COUNT(*) FROM transactions WHERE amount_sen IS NULL").fetchone()[0]
    assert got == 1234 and nulls == 0
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    c.close()
