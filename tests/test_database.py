"""Phase 1/2 database & persistence layer tests. Temp DB only."""
import sqlite3
import datetime

import pytest

import main
import db as db_layer
import scheduler
from conftest import apply_migrations, seed_direct_user, ADMIN_ID

HEAD_REVISION = "0020_user_interests"


def _iso(offset_hours=0):
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=offset_hours)).astimezone().isoformat()


def _schema_snapshot(path):
    c = sqlite3.connect(path)
    rows = c.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    c.close()
    return rows


# 1 -------------------------------------------------------------------------
def test_database_initialization(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for expected in ["users", "sessions", "transactions", "tasks", "schedule_events",
                     "ai_recommendations", "user_preferences",
                     "user_financial_settings", "map_locations", "alembic_version"]:
        assert expected in names
    # legacy / superseded tables must be gone (user_budgets dropped in 0008)
    for legacy in ["finances", "schedule", "user_memory", "user_budgets"]:
        assert legacy not in names


# 2 -------------------------------------------------------------------------
def test_migration_execution(fresh_db_path):
    c = sqlite3.connect(fresh_db_path)
    assert c.execute("SELECT version_num FROM alembic_version").fetchone()[0] == HEAD_REVISION
    # auth artefacts present
    ucols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
    assert {"email_normalized", "password_hash", "is_active"} <= ucols
    assert c.execute("SELECT 1 FROM sqlite_master WHERE name='sessions'").fetchone()
    assert any(row[2] == "users" for row in c.execute("PRAGMA foreign_key_list(sessions)"))
    c.close()


# 3 -------------------------------------------------------------------------
def test_migration_idempotency(fresh_db_path):
    before = _schema_snapshot(fresh_db_path)
    apply_migrations(fresh_db_path)
    apply_migrations(fresh_db_path)
    assert before == _schema_snapshot(fresh_db_path)


# 3b — downgrade/re-upgrade sanity for the auth migration
def test_auth_migration_downgrade_reupgrade(fresh_db_path, tmp_path):
    import subprocess, sys, os
    env = dict(os.environ, COREOS_DB_PATH=fresh_db_path)
    r1 = subprocess.run([sys.executable, "-m", "alembic", "downgrade", "0005_foreign_keys"],
                        cwd=os.path.dirname(os.path.dirname(__file__)), env=env, capture_output=True)
    assert r1.returncode == 0, r1.stderr
    c = sqlite3.connect(fresh_db_path)
    assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='sessions'").fetchone()
    assert {r[1] for r in c.execute("PRAGMA table_info(users)")} == {"id", "name", "email"}
    c.close()
    r2 = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                        cwd=os.path.dirname(os.path.dirname(__file__)), env=env, capture_output=True)
    assert r2.returncode == 0, r2.stderr
    c = sqlite3.connect(fresh_db_path)
    assert c.execute("SELECT version_num FROM alembic_version").fetchone()[0] == HEAD_REVISION
    c.close()


# 4 -------------------------------------------------------------------------
def test_foreign_key_enforcement(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO transactions (id, user_id, amount, category, date, type) "
            "VALUES ('bad', 'no_such_user', 1, 'Food', ?, 'expense')", (_iso(),)
        )
    conn.execute(
        "INSERT INTO transactions (id, user_id, amount, category, date, type) "
        "VALUES ('ok', ?, 1, 'Food', ?, 'expense')", (ADMIN_ID, _iso())
    )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE id='ok'").fetchone()[0] == 1


def test_foreign_key_cascade_delete(conn):
    conn.execute("INSERT INTO users (id, name, email) VALUES ('tmp_u', 'x', 'x1@x')")
    conn.execute("INSERT INTO transactions (id, user_id, amount, category, date, type) "
                 "VALUES ('t_casc', 'tmp_u', 5, 'Food', ?, 'expense')", (_iso(),))
    conn.execute("INSERT INTO sessions (id, user_id, token_hash, created_at, expires_at) "
                 "VALUES ('s_casc', 'tmp_u', 'h', ?, ?)", (_iso(), _iso(24)))
    conn.commit()
    conn.execute("DELETE FROM users WHERE id='tmp_u'")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE id='t_casc'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM sessions WHERE id='s_casc'").fetchone()[0] == 0


# 5 -------------------------------------------------------------------------
def test_transaction_persistence():
    seed_direct_user()
    fields = main._validate_transaction_fields(
        amount=12.5, type_="expense", category="Food", description="unit test coffee", date=_iso())
    out = main.AppServices.create_transaction(ADMIN_ID, fields)
    assert out.status == "SUCCESS"
    tx_id = out.data["transaction"]["id"]
    c = db_layer.get_connection()
    row = c.execute("SELECT amount, amount_sen, description, created_at, updated_at FROM transactions WHERE id=?",
                    (tx_id,)).fetchone()
    c.close()
    assert row["amount"] == 12.5 and row["amount_sen"] == 1250
    assert row["description"] == "unit test coffee"
    assert row["created_at"] and row["updated_at"]


# 6 -------------------------------------------------------------------------
def test_task_persistence(conn):
    out = main.AppServices.create_task(ADMIN_ID, main.TaskParams(title="unit test task"))
    assert out.status == "SUCCESS"
    row = db_layer.get_connection().execute(
        "SELECT title, reminder_status, created_at FROM tasks WHERE id=?", (out.data["id"],)).fetchone()
    assert row["title"] == "unit test task" and row["reminder_status"] == "NONE" and row["created_at"]


# 7 -------------------------------------------------------------------------
def test_schedule_persistence(alice):
    tc, user = alice
    c = db_layer.get_connection()
    c.execute("INSERT INTO schedule_events (id, user_id, title, location, start_time, end_time, created_at, updated_at) "
              "VALUES ('ev1', ?, 'Exam', 'Hall A', ?, ?, ?, ?)",
              (user["id"], _iso(1), _iso(2), _iso(), _iso()))
    c.commit()
    c.close()
    r = tc.get("/api/schedule")
    assert r.status_code == 200
    assert "Exam" in [e["title"] for e in r.json()["events"]]


# 8 -------------------------------------------------------------------------
def test_recommendation_persistence(alice):
    tc, user = alice
    c = db_layer.get_connection()
    c.execute("INSERT INTO ai_recommendations (id, user_id, module, type, message, status) "
              "VALUES ('rec_x', ?, 'FINANCE', 'INSIGHT', 'hi', 'PENDING')", (user["id"],))
    c.commit(); c.close()

    r = tc.post("/api/recommendations/update", json={"recommendation_id": "rec_x", "action": "DISMISSED"})
    assert r.status_code == 200
    c = db_layer.get_connection()
    row = c.execute("SELECT status, updated_at FROM ai_recommendations WHERE id='rec_x'").fetchone()
    c.close()
    assert row["status"] == "DISMISSED" and row["updated_at"]
    fr = tc.get("/api/finance")
    assert all(rec["id"] != "rec_x" for rec in fr.json()["active_recommendations"])


# 9 -------------------------------------------------------------------------
def test_settings_persistence(alice):
    tc, _ = alice
    r = tc.post("/api/settings/update", json={
        "notifications": False, "location_services": True, "dark_mode": False})
    assert r.status_code == 200
    got = tc.get("/api/settings").json()
    assert got["notifications"] == 0 and got["dark_mode"] == 0 and got["location_services"] == 1


# 10 ------------------------------------------------------------------------
def test_starting_balance_does_not_overwrite_budgets(alice):
    tc, _ = alice
    assert tc.post("/api/finance/settings", json={"weekly_budget": 500, "monthly_budget": 2000,
                                                  "daily_budget": 70}).status_code == 200
    before = tc.get("/api/finance/settings").json()
    assert tc.post("/api/finance/settings", json={"starting_balance": 4321}).status_code == 200
    after = tc.get("/api/finance/settings").json()
    assert after["starting_balance"] == 4321
    assert after["weekly_budget"] == before["weekly_budget"] == 500
    assert after["monthly_budget"] == before["monthly_budget"] == 2000
    assert after["daily_budget"] == before["daily_budget"] == 70


def test_single_budget_update_touches_only_that_field(alice):
    tc, _ = alice
    tc.post("/api/finance/settings", json={"starting_balance": 100, "weekly_budget": 5,
                                           "monthly_budget": 6, "daily_budget": 7})
    tc.post("/api/finance/settings", json={"daily_budget": 99})
    after = tc.get("/api/finance/settings").json()
    assert after["daily_budget"] == 99
    assert after["starting_balance"] == 100 and after["weekly_budget"] == 5 and after["monthly_budget"] == 6


# 11 ------------------------------------------------------------------------
@pytest.mark.parametrize("payload", [
    {"starting_balance": -1}, {"weekly_budget": -5}, {"monthly_budget": -0.01}, {"daily_budget": -100},
])
def test_invalid_financial_values_rejected(alice, payload):
    tc, _ = alice
    assert tc.post("/api/finance/settings", json=payload).status_code == 400


# 12 ------------------------------------------------------------------------
def test_reminder_persistence(conn):
    out = main.AppServices.create_task(ADMIN_ID, main.TaskParams(title="call bank", reminder_at=_iso(5)),
                                       is_reminder=True)
    assert out.status == "SUCCESS"
    c2 = db_layer.get_connection()
    row = c2.execute("SELECT reminder_at, reminder_status FROM tasks WHERE id=?", (out.data["id"],)).fetchone()
    c2.close()
    assert row["reminder_at"] and row["reminder_status"] == "PENDING"


# 13 ------------------------------------------------------------------------
def test_due_reminder_processing(conn):
    past, future = _iso(-3), _iso(+3)
    conn.execute("INSERT INTO tasks (id,user_id,title,is_completed,reminder_at,reminder_status,created_at,updated_at) "
                 "VALUES ('r_past', ?, 'past', 0, ?, 'PENDING', ?, ?)", (ADMIN_ID, past, past, past))
    conn.execute("INSERT INTO tasks (id,user_id,title,is_completed,reminder_at,reminder_status,created_at,updated_at) "
                 "VALUES ('r_future', ?, 'future', 0, ?, 'PENDING', ?, ?)", (ADMIN_ID, future, future, future))
    conn.execute("INSERT INTO tasks (id,user_id,title,is_completed,reminder_at,reminder_status,created_at,updated_at) "
                 "VALUES ('r_done', ?, 'done', 1, ?, 'PENDING', ?, ?)", (ADMIN_ID, past, past, past))
    conn.commit()

    assert scheduler.scan_due_reminders_once() == 1
    c = db_layer.get_connection()
    st = dict(c.execute("SELECT id, reminder_status FROM tasks WHERE id IN ('r_past','r_future','r_done')").fetchall())
    c.close()
    assert st["r_past"] == "DUE" and st["r_future"] == "PENDING" and st["r_done"] == "PENDING"
    assert scheduler.scan_due_reminders_once() == 0


# 14 ------------------------------------------------------------------------
def test_rollback_after_failed_operation():
    with pytest.raises(sqlite3.IntegrityError):
        with db_layer.transaction() as c:
            c.execute("INSERT INTO users (id, name, email) VALUES ('rb_user', 'a', 'a@a')")
            c.execute("INSERT INTO users (id, name, email) VALUES ('rb_user', 'b', 'b@b')")
    c = db_layer.get_connection()
    n = c.execute("SELECT COUNT(*) FROM users WHERE id='rb_user'").fetchone()[0]
    c.close()
    assert n == 0


# integrity ---------------------------------------------------------------
def test_integrity_and_fk_check(alice):
    tc, _ = alice
    tc.post("/api/finance/settings", json={"starting_balance": 10})
    c = db_layer.get_connection()
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    c.close()
