"""Account & security: Change Password, Change Email, Delete Account.

All three require re-proving the current password even though the caller is
already authenticated via session cookie — tested explicitly below. Delete
Account must also remove rows in the tables that have NO DB-declared foreign
key back to users(id) (mo_proposals, mo_context, the conflict/decision/
override tables) — SQLite's ON DELETE CASCADE only covers the tables that
DO declare one, so this is the one part of deletion that isn't automatic and
is worth its own regression test.

Disposable test accounts only (alice/bob fixtures, real registration) — the
real demo account is never touched here.
"""
import db as db_layer
from conftest import mo_propose


def _user_row(email: str):
    c = db_layer.get_connection()
    try:
        return c.execute(
            "SELECT id, email, email_normalized, password_hash FROM users WHERE email = ?",
            (email,),
        ).fetchone()
    finally:
        c.close()


def _count(table: str, user_id: str) -> int:
    c = db_layer.get_connection()
    try:
        return c.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (user_id,)).fetchone()[0]
    finally:
        c.close()


# --------------------------------------------------------------------------- #
# Change Password
# --------------------------------------------------------------------------- #
def test_change_password_wrong_current_password_rejected(alice):
    tc, _ = alice
    r = tc.post("/api/auth/change-password",
                json={"current_password": "wrong", "new_password": "NewPass123!"})
    assert r.status_code == 401, r.text


def test_change_password_too_short_rejected(alice):
    tc, _ = alice
    r = tc.post("/api/auth/change-password",
                json={"current_password": "alicepass123", "new_password": "short"})
    assert r.status_code == 422, r.text


def test_change_password_success_logs_in_with_new_and_not_old(alice):
    tc, user = alice
    r = tc.post("/api/auth/change-password",
                json={"current_password": "alicepass123", "new_password": "BrandNewPass456!"})
    assert r.status_code == 200, r.text

    # old password no longer works
    from fastapi.testclient import TestClient
    import main
    fresh = TestClient(main.app)
    bad = fresh.post("/api/auth/login", json={"email": user["email"], "password": "alicepass123"})
    assert bad.status_code == 401, bad.text

    good = fresh.post("/api/auth/login", json={"email": user["email"], "password": "BrandNewPass456!"})
    assert good.status_code == 200, good.text


def test_change_password_revokes_other_sessions(alice):
    tc, user = alice
    from fastapi.testclient import TestClient
    import main
    # a second "device" logged in as the same user, before the change
    other_device = TestClient(main.app)
    login = other_device.post("/api/auth/login", json={"email": user["email"], "password": "alicepass123"})
    assert login.status_code == 200, login.text
    assert other_device.get("/api/auth/me").status_code == 200

    tc.post("/api/auth/change-password",
            json={"current_password": "alicepass123", "new_password": "AnotherNewPass789!"})

    # the OTHER device's old session must now be dead
    assert other_device.get("/api/auth/me").status_code == 401
    # but the device that made the change is still signed in (fresh cookie issued)
    assert tc.get("/api/auth/me").status_code == 200


# --------------------------------------------------------------------------- #
# Change Email
# --------------------------------------------------------------------------- #
def test_change_email_wrong_password_rejected(alice):
    tc, _ = alice
    r = tc.post("/api/auth/change-email",
                json={"new_email": "alice2@example.com", "current_password": "wrong"})
    assert r.status_code == 401, r.text


def test_change_email_invalid_format_rejected(alice):
    tc, _ = alice
    r = tc.post("/api/auth/change-email",
                json={"new_email": "not-an-email", "current_password": "alicepass123"})
    assert r.status_code == 422, r.text


def test_change_email_duplicate_rejected(alice, bob):
    tc, _ = alice
    _, bob_user = bob
    r = tc.post("/api/auth/change-email",
                json={"new_email": bob_user["email"], "current_password": "alicepass123"})
    assert r.status_code == 409, r.text


def test_change_email_success_updates_identity_and_login(alice):
    tc, user = alice
    r = tc.post("/api/auth/change-email",
                json={"new_email": "alice.new@example.com", "current_password": "alicepass123"})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["email"] == "alice.new@example.com"

    me = tc.get("/api/auth/me").json()
    assert me["user"]["email"] == "alice.new@example.com"

    from fastapi.testclient import TestClient
    import main
    fresh = TestClient(main.app)
    old = fresh.post("/api/auth/login", json={"email": user["email"], "password": "alicepass123"})
    assert old.status_code == 401
    new = fresh.post("/api/auth/login", json={"email": "alice.new@example.com", "password": "alicepass123"})
    assert new.status_code == 200


# --------------------------------------------------------------------------- #
# Delete Account
# --------------------------------------------------------------------------- #
def test_delete_account_wrong_password_rejected(alice):
    tc, _ = alice
    r = tc.post("/api/auth/delete-account", json={"current_password": "wrong"})
    assert r.status_code == 401, r.text
    # account must still exist
    assert tc.get("/api/auth/me").status_code == 200


def test_delete_account_success_removes_row_and_signs_out(alice):
    tc, user = alice
    r = tc.post("/api/auth/delete-account", json={"current_password": "alicepass123"})
    assert r.status_code == 200, r.text

    assert _user_row(user["email"]) is None
    assert tc.get("/api/auth/me").status_code == 401

    from fastapi.testclient import TestClient
    import main
    fresh = TestClient(main.app)
    login = fresh.post("/api/auth/login", json={"email": user["email"], "password": "alicepass123"})
    assert login.status_code == 401


def test_delete_account_removes_orphan_table_rows(alice, mock_ai):
    """mo_proposals has no DB-declared FK -> ON DELETE CASCADE never touches
    it. Account deletion must clean it up explicitly or the row is orphaned
    forever."""
    tc, user = alice
    mock_ai("CREATE_TRANSACTION", parameters={
        "amount": 15, "type": "expense", "category": "Food",
        "description": "snack", "date": "2026-09-05T09:00:00+08:00",
    })
    pr = mo_propose(tc, "I spent RM15 on a snack", "tok-orphan-1")
    assert pr.status_code == 200, pr.text
    user_id = user["id"]
    assert _count("mo_proposals", user_id) == 1   # pending proposal exists, left unconfirmed

    r = tc.post("/api/auth/delete-account", json={"current_password": "alicepass123"})
    assert r.status_code == 200, r.text

    assert _count("mo_proposals", user_id) == 0


def test_delete_account_does_not_affect_other_users(alice, bob):
    tc_a, user_a = alice
    tc_b, user_b = bob
    tc_a.post("/api/auth/delete-account", json={"current_password": "alicepass123"})

    assert _user_row(user_a["email"]) is None
    assert _user_row(user_b["email"]) is not None
    assert tc_b.get("/api/auth/me").status_code == 200
