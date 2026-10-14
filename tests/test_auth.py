"""Phase 2 — authentication tests. Temp DB, no real OpenAI calls."""
import db as db_layer
import auth as auth_core
from conftest import new_authed_client


# ---- REGISTER -------------------------------------------------------------
def test_register_valid(client):
    r = client.post("/api/auth/register", json={"email": "new@example.com", "password": "password123", "name": "New"})
    assert r.status_code == 201
    body = r.json()
    assert body["user"]["email"] == "new@example.com" and body["user"]["id"].startswith("usr_")
    assert "password" not in str(body) and "hash" not in str(body).lower()
    assert client.cookies.get("coreos_session")


def test_register_duplicate_email(client):
    client.post("/api/auth/register", json={"email": "dup@example.com", "password": "password123"})
    r = client.post("/api/auth/register", json={"email": "DUP@example.com", "password": "password123"})
    assert r.status_code == 409


def test_register_malformed_email(client):
    r = client.post("/api/auth/register", json={"email": "not-an-email", "password": "password123"})
    assert r.status_code == 422


def test_register_weak_password(client):
    r = client.post("/api/auth/register", json={"email": "weak@example.com", "password": "short"})
    assert r.status_code == 422


# ---- PASSWORD SECURITY --------------------------------------------------
def test_password_is_hashed_not_plaintext(client):
    client.post("/api/auth/register", json={"email": "hash@example.com", "password": "sup3rsecret!!"})
    c = db_layer.get_connection()
    stored = c.execute("SELECT password_hash FROM users WHERE email_normalized='hash@example.com'").fetchone()[0]
    c.close()
    assert stored and stored != "sup3rsecret!!"
    assert stored.startswith("$argon2")
    assert auth_core.verify_password(stored, "sup3rsecret!!") is True
    assert auth_core.verify_password(stored, "wrong") is False


# ---- LOGIN ------------------------------------------------------------------
def test_login_correct_credentials(client):
    client.post("/api/auth/register", json={"email": "li@example.com", "password": "password123"})
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={"email": "li@example.com", "password": "password123"})
    assert r.status_code == 200
    assert client.cookies.get("coreos_session")
    assert client.get("/api/auth/me").json()["user"]["email"] == "li@example.com"


def test_login_wrong_password(client):
    client.post("/api/auth/register", json={"email": "wp@example.com", "password": "password123"})
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={"email": "wp@example.com", "password": "WRONGWRONG"})
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid email or password."


def test_login_nonexistent_account_is_generic(client):
    r = client.post("/api/auth/login", json={"email": "ghost@example.com", "password": "whatever12"})
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid email or password."


def test_login_account_without_password_is_generic(client):
    # simulate a pre-Phase-2 account: user row, no password_hash
    c = db_layer.get_connection()
    c.execute("INSERT INTO users (id,name,email,email_normalized,is_active,created_at,updated_at) "
              "VALUES ('legacy1','L','legacy@example.com','legacy@example.com',1,'x','x')")
    c.commit(); c.close()
    r = client.post("/api/auth/login", json={"email": "legacy@example.com", "password": "anything123"})
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid email or password."


# ---- SESSION -------------------------------------------------------------
def test_unauthenticated_requests_are_401(client):
    for path in ["/api/auth/me", "/api/finance", "/api/finance/history", "/api/schedule",
                 "/api/activity", "/api/settings", "/api/dashboard-data", "/api/map-locations"]:
        assert client.get(path).status_code == 401, path
    assert client.post("/api/settings/update", json={"notifications": True, "location_services": True,
                                                     "dark_mode": True}).status_code == 401
    assert client.post("/api/process-input", json={"user_input": "hi"}).status_code == 401


def test_authenticated_request_succeeds(alice):
    tc, _ = alice
    assert tc.get("/api/auth/me").status_code == 200
    assert tc.get("/api/finance").status_code == 200


def test_session_survives_multiple_requests(alice):
    tc, _ = alice
    for _ in range(4):
        assert tc.get("/api/dashboard-data").status_code == 200


def test_logout_invalidates_session(alice):
    tc, _ = alice
    assert tc.post("/api/auth/logout").status_code == 200
    assert tc.get("/api/auth/me").status_code == 401
    assert tc.get("/api/finance").status_code == 401


def test_invalid_or_expired_session_rejected(client):
    client.cookies.set("coreos_session", "totally-made-up-token")
    assert client.get("/api/auth/me").status_code == 401

    # expired: register, then age the session row past expiry
    tc, user = new_authed_client("exp@example.com", "password123")
    c = db_layer.get_connection()
    c.execute("UPDATE sessions SET expires_at = '2000-01-01T00:00:00+00:00' WHERE user_id = ?", (user["id"],))
    c.commit(); c.close()
    assert tc.get("/api/auth/me").status_code == 401
    # the expired row is cleaned up lazily
    c = db_layer.get_connection()
    assert c.execute("SELECT COUNT(*) FROM sessions WHERE user_id=?", (user["id"],)).fetchone()[0] == 0
    c.close()


def test_logout_deletes_server_side_session_row(alice):
    tc, user = alice
    c = db_layer.get_connection()
    assert c.execute("SELECT COUNT(*) FROM sessions WHERE user_id=?", (user["id"],)).fetchone()[0] == 1
    c.close()
    tc.post("/api/auth/logout")
    c = db_layer.get_connection()
    assert c.execute("SELECT COUNT(*) FROM sessions WHERE user_id=?", (user["id"],)).fetchone()[0] == 0
    c.close()


def test_rate_limit_on_login(client):
    client.post("/api/auth/register", json={"email": "rl@example.com", "password": "password123"})
    client.post("/api/auth/logout")
    codes = [client.post("/api/auth/login", json={"email": "rl@example.com", "password": "bad"}).status_code
             for _ in range(12)]
    assert 429 in codes  # limiter kicks in within the window
