"""B4 — appearance preference: server persistence, validation, per-account
isolation, backward compatibility with the inert `dark_mode` flag, and the
guarantee that a theme choice never mutates domain data. Temp DB, no paid calls.
"""
import pytest

import db as db_layer
from conftest import new_authed_client


def _counts(user_id):
    c = db_layer.get_connection()
    try:
        return {
            t: c.execute(f"SELECT COUNT(*) FROM {t} WHERE user_id=?", (user_id,)).fetchone()[0]
            for t in ("transactions", "tasks", "schedule_events", "mo_proposals",
                      "schedule_conflict_decisions")
        }
    finally:
        c.close()


@pytest.mark.parametrize("mode", ["system", "light", "dark"])
def test_valid_modes_persist_and_round_trip(mode):
    tc, _ = new_authed_client(f"ap_{mode}@example.com")
    r = tc.post("/api/settings/update", json={
        "notifications": True, "location_services": True, "appearance_mode": mode,
    })
    assert r.status_code == 200
    assert tc.get("/api/settings").json()["appearance_mode"] == mode


@pytest.mark.parametrize("bad", ["System", "DARK", "auto", "oled", "", "  ", "light ", "1"])
def test_invalid_mode_is_422_and_changes_nothing(bad):
    tc, user = new_authed_client("ap_bad@example.com")
    tc.post("/api/settings/update", json={
        "notifications": True, "location_services": True, "appearance_mode": "dark",
    })
    r = tc.post("/api/settings/update", json={
        "notifications": True, "location_services": True, "appearance_mode": bad,
    })
    assert r.status_code == 422
    assert tc.get("/api/settings").json()["appearance_mode"] == "dark"   # unchanged
    assert _counts(user["id"]) == {k: 0 for k in _counts(user["id"])}


def test_omitting_appearance_mode_keeps_the_stored_value():
    tc, _ = new_authed_client("ap_keep@example.com")
    tc.post("/api/settings/update", json={
        "notifications": True, "location_services": True, "appearance_mode": "light",
    })
    # an update that only flips a toggle must not wipe the saved appearance
    r = tc.post("/api/settings/update", json={"notifications": False, "location_services": True})
    assert r.status_code == 200
    got = tc.get("/api/settings").json()
    assert got["appearance_mode"] == "light" and got["notifications"] == 0


def test_no_saved_appearance_reads_back_as_none_regardless_of_legacy_dark_mode():
    tc, _ = new_authed_client("ap_legacy@example.com")
    # a pre-B4 client that only knows notifications/location_services/dark_mode
    r = tc.post("/api/settings/update", json={
        "notifications": True, "location_services": True, "dark_mode": True,
    })
    assert r.status_code == 200
    got = tc.get("/api/settings").json()
    assert got["dark_mode"] == 1            # legacy flag preserved, not deleted
    assert got["appearance_mode"] is None   # ...but it does NOT drive appearance


def test_each_account_keeps_its_own_appearance():
    ac, a = new_authed_client("ap_a@example.com")
    bc, b = new_authed_client("ap_b@example.com")
    ac.post("/api/settings/update", json={
        "notifications": True, "location_services": True, "appearance_mode": "light"})
    bc.post("/api/settings/update", json={
        "notifications": True, "location_services": True, "appearance_mode": "dark"})
    assert ac.get("/api/settings").json()["appearance_mode"] == "light"
    assert bc.get("/api/settings").json()["appearance_mode"] == "dark"
    # switching back (a second client for A) still resolves A's own saved mode
    a2 = new_authed_client  # noqa: F841 — re-login below via a fresh client
    from fastapi.testclient import TestClient
    import main
    tc = TestClient(main.app)
    assert tc.post("/api/auth/login",
                   json={"email": "ap_a@example.com", "password": "password123"}).status_code == 200
    assert tc.get("/api/settings").json()["appearance_mode"] == "light"


def test_get_settings_never_returns_another_users_appearance():
    ac, _ = new_authed_client("ap_iso_a@example.com")
    bc, _ = new_authed_client("ap_iso_b@example.com")
    ac.post("/api/settings/update", json={
        "notifications": True, "location_services": True, "appearance_mode": "dark"})
    # B has never saved one -> B sees None, never A's "dark"
    assert bc.get("/api/settings").json().get("appearance_mode") is None


def test_saving_appearance_does_not_touch_domain_data(alice):
    tc, user = alice
    tc.post("/api/transactions", json={"amount": 12, "type": "expense",
                                       "category": "Food", "description": "x"})
    before = _counts(user["id"])
    for m in ("dark", "light", "system", "dark"):
        assert tc.post("/api/settings/update", json={
            "notifications": True, "location_services": True, "appearance_mode": m,
        }).status_code == 200
    assert _counts(user["id"]) == before


def test_settings_update_requires_auth():
    from fastapi.testclient import TestClient
    import main
    r = TestClient(main.app).post("/api/settings/update", json={
        "notifications": True, "location_services": True, "appearance_mode": "dark"})
    assert r.status_code in (401, 403)
