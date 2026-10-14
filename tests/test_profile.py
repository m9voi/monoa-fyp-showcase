"""Batch 01 — Profile / Personal Details: the authenticated user may edit
their own display name only. Temp DB, no real OpenAI calls.

The frontend source-string checks below follow the same pattern already used
by tests/test_dashboard_today.py: this stack has no DOM/React renderer for
`node --test`, so page WIRING (which endpoint is called, what happens on
success vs. failure) is verified by reading the real .tsx source, the same
way test_dashboard_today.py verifies Today's wiring. The pure validation
logic itself (validateName/isDirty) has its own real unit tests at
app/profile/__tests__/personalDetails.test.ts (run via `npm run test:node`).
"""
import os

import db as db_layer
from conftest import new_authed_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), "r", encoding="utf-8") as fh:
        return fh.read()


_PROFILE = _read("app", "profile", "page.tsx")
_PERSONAL_DETAILS = _read("app", "profile", "personal-details", "page.tsx")
_HELPERS = _read("app", "profile", "personalDetails.ts")


def _stored_name(user_id: str) -> str:
    c = db_layer.get_connection()
    try:
        row = c.execute("SELECT name FROM users WHERE id = ?", (user_id,)).fetchone()
        return row["name"]
    finally:
        c.close()


def test_unauthenticated_request_rejected(client):
    r = client.patch("/api/profile", json={"name": "Nobody"})
    assert r.status_code == 401


def test_authenticated_user_can_update_own_name(alice):
    tc, user = alice
    r = tc.patch("/api/profile", json={"name": "Alice Wonderland"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["name"] == "Alice Wonderland"
    assert body["user"]["id"] == user["id"]


def test_update_persists_in_db(alice):
    tc, user = alice
    tc.patch("/api/profile", json={"name": "Persisted Alice"})
    assert _stored_name(user["id"]) == "Persisted Alice"
    # A fresh read (not just the mutation response) reflects it too.
    r = tc.get("/api/auth/me")
    assert r.json()["user"]["name"] == "Persisted Alice"


def test_response_returns_canonical_trimmed_value(alice):
    tc, _ = alice
    r = tc.patch("/api/profile", json={"name": "   Spacey Name   "})
    assert r.status_code == 200
    assert r.json()["user"]["name"] == "Spacey Name"


def test_whitespace_only_name_rejected(alice):
    tc, user = alice
    r = tc.patch("/api/profile", json={"name": "   "})
    assert r.status_code == 422
    # nothing changed
    assert _stored_name(user["id"]) == "Alice"


def test_empty_name_rejected(alice):
    tc, user = alice
    r = tc.patch("/api/profile", json={"name": ""})
    assert r.status_code == 422
    assert _stored_name(user["id"]) == "Alice"


def test_over_limit_name_rejected(alice):
    tc, user = alice
    too_long = "A" * 121
    r = tc.patch("/api/profile", json={"name": too_long})
    assert r.status_code == 422
    assert _stored_name(user["id"]) == "Alice"


def test_name_at_exact_limit_is_accepted(alice):
    tc, _ = alice
    exactly_120 = "B" * 120
    r = tc.patch("/api/profile", json={"name": exactly_120})
    assert r.status_code == 200
    assert r.json()["user"]["name"] == exactly_120


def test_unicode_name_preserved(alice):
    tc, _ = alice
    r = tc.patch("/api/profile", json={"name": "Müsab Ünïcödé 名前"})
    assert r.status_code == 200
    assert r.json()["user"]["name"] == "Müsab Ünïcödé 名前"


def test_one_users_update_cannot_affect_another_user(alice, bob):
    ac, alice_user = alice
    bc, bob_user = bob
    ac.patch("/api/profile", json={"name": "Alice Changed"})
    assert _stored_name(alice_user["id"]) == "Alice Changed"
    assert _stored_name(bob_user["id"]) == "Bob"  # untouched
    bc.patch("/api/profile", json={"name": "Bob Changed"})
    assert _stored_name(bob_user["id"]) == "Bob Changed"
    assert _stored_name(alice_user["id"]) == "Alice Changed"  # still untouched


def test_email_is_unchanged_by_a_name_update(alice):
    tc, user = alice
    before_email = user["email"]
    r = tc.patch("/api/profile", json={"name": "New Name Only"})
    assert r.json()["user"]["email"] == before_email
    c = db_layer.get_connection()
    try:
        row = c.execute("SELECT email FROM users WHERE id = ?", (user["id"],)).fetchone()
        assert row["email"] == before_email
    finally:
        c.close()


def test_existing_session_remains_valid_after_name_update(alice):
    tc, _ = alice
    tc.patch("/api/profile", json={"name": "Still Signed In"})
    # the SAME session cookie still authenticates further requests
    r = tc.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json()["user"]["name"] == "Still Signed In"
    r2 = tc.get("/api/finance")
    assert r2.status_code == 200


# --------------------------------------------------------------------------- #
# Frontend wiring (source-string checks — see module docstring for why)       #
# --------------------------------------------------------------------------- #
def test_profile_page_renders_the_real_authenticated_identity():
    # No hardcoded name/email anywhere — both come from useAuth().user.
    assert "user?.name" in _PROFILE
    assert "user?.email" in _PROFILE
    assert "MONOA user" in _PROFILE  # the one honest fallback, not a fake name


def test_profile_page_links_to_personal_details():
    assert 'href="/profile/personal-details"' in _PROFILE
    assert os.path.isdir(os.path.join(ROOT, "app", "profile", "personal-details"))


def test_profile_no_longer_shows_a_fake_photo_upload_control():
    lowered = _PROFILE.lower() + _PERSONAL_DETAILS.lower()
    for banned in ("change photo", "upload photo", "edit photo", "add photo"):
        assert banned not in lowered, banned


def test_personal_details_form_initializes_from_the_real_user():
    assert "useState(user?.name" in _PERSONAL_DETAILS
    assert "user?.email" in _PERSONAL_DETAILS


def test_personal_details_email_field_is_read_only():
    assert "readOnly" in _PERSONAL_DETAILS
    assert "Can&apos;t be changed here" in _PERSONAL_DETAILS
    assert "/settings/account-security" in _PERSONAL_DETAILS


def test_personal_details_uses_the_real_validation_helper_not_inline_rules():
    assert "validateName(name)" in _PERSONAL_DETAILS
    assert "from \"../personalDetails\"" in _PERSONAL_DETAILS


def test_personal_details_never_truncates_silently():
    assert "NAME_MAX_LENGTH" in _HELPERS
    assert "trimmed.length > NAME_MAX_LENGTH" in _HELPERS
    # rejects (returns an error string), never slices the value down
    assert ".slice(" not in _HELPERS and ".substring(" not in _HELPERS


def test_personal_details_success_path_refetches_the_canonical_user():
    # The just-typed value is never trusted as "saved" — a successful PATCH
    # is followed by refresh() (re-fetching /api/auth/me) BEFORE setSaved(true).
    save_fn = _PERSONAL_DETAILS[_PERSONAL_DETAILS.index("const save ="):]
    try_block = save_fn[save_fn.index("try {"):save_fn.index("} catch")]
    assert "await refresh()" in try_block
    assert try_block.index("await refresh()") < try_block.index("setSaved(true)")


def test_personal_details_failure_path_never_sets_saved():
    save_fn = _PERSONAL_DETAILS[_PERSONAL_DETAILS.index("const save ="):]
    catch_block = save_fn[save_fn.index("} catch"):save_fn.index("} finally")]
    assert "setSaved(true)" not in catch_block
    assert "setError(" in catch_block


def test_personal_details_disables_submit_while_busy_or_unchanged():
    assert "disabled={busy || !dirty}" in _PERSONAL_DETAILS
