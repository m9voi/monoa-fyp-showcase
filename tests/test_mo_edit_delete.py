"""Mo capability closure — EDIT / DELETE task+event, GET_TODAY_SUMMARY, and the
updated unsupported fallback.

Same discipline as test_mo_hitl.py: temp DB via conftest, the AI boundary is
the `mock_ai` fixture (no real model call). UPDATE_TASK / DELETE_TASK /
UPDATE_EVENT / DELETE_EVENT never receive an id from the model — Mo resolves
the real target itself, server-side, from a free-text "target_description"
against the CALLER's OWN rows only (see _mo_resolve_task_target /
_mo_resolve_event_target in main.py). Nothing mutates before Confirm; the
executed mutation reuses the exact canonical column-update/delete path the
manual Plan UI uses.
"""
import datetime as dt

import db as db_layer
from conftest import new_authed_client, mo_propose, mo_confirm, mo_pending

MYT = dt.timezone(dt.timedelta(hours=8))


def _now():
    return dt.datetime.now(MYT).replace(minute=0, second=0, microsecond=0)


def _mk_task(tc, title, **extra):
    body = {"title": title, **extra}
    r = tc.post("/api/tasks", json=body)
    assert r.status_code == 201, r.text
    return r.json()["task"]


def _mk_event(tc, title, start, end):
    r = tc.post("/api/events", json={"title": title, "start": start.isoformat(), "end": end.isoformat()})
    assert r.status_code == 201, r.text
    return r.json()["event"]


def _db_task(task_id):
    c = db_layer.get_connection()
    try:
        r = c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


def _db_event(event_id):
    c = db_layer.get_connection()
    try:
        r = c.execute("SELECT * FROM schedule_events WHERE id = ?", (event_id,)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


# --------------------------------------------------------------------------- #
# EDIT — task
# --------------------------------------------------------------------------- #
def test_update_task_proposal_confirm_and_persistence(alice, mock_ai):
    tc, _ = alice
    task = _mk_task(tc, "FYP writeup")
    mock_ai("UPDATE_TASK", parameters={"target_description": "FYP", "new_title": "FYP final writeup"})

    pr = mo_propose(tc, "rename my FYP task to FYP final writeup", "tok-upd-1")
    j = pr.json()
    assert j["kind"] == "proposal_required"
    assert j["proposal"]["display"]["new_title"] == "FYP final writeup"
    # nothing mutates before Confirm
    assert _db_task(task["id"])["title"] == "FYP writeup"

    c1 = mo_confirm(tc, j["proposal"]["proposal_id"]).json()
    assert c1["kind"] == "executed"
    assert _db_task(task["id"])["title"] == "FYP final writeup"


def test_update_task_nonexistent_target(alice, mock_ai):
    tc, _ = alice
    mock_ai("UPDATE_TASK", parameters={"target_description": "a task that does not exist", "new_title": "x"})
    pr = mo_propose(tc, "rename my nonexistent task", "tok-upd-404").json()
    assert pr["kind"] == "error"
    assert "couldn" in pr["message"].lower() or "find" in pr["message"].lower()


def test_update_task_ambiguous_target_asks_for_clarification(alice, mock_ai):
    tc, _ = alice
    _mk_task(tc, "Team meeting notes")
    _mk_task(tc, "Client meeting notes")
    mock_ai("UPDATE_TASK", parameters={"target_description": "meeting", "new_title": "renamed"})
    pr = mo_propose(tc, "rename my meeting task", "tok-upd-ambig").json()
    assert pr["kind"] == "clarification_required"


def test_update_task_cross_user_cannot_reference_another_users_task(alice, bob, mock_ai):
    ac, _a = alice
    bc, _b = bob
    _mk_task(ac, "Alice Secret Task")
    mock_ai("UPDATE_TASK", parameters={"target_description": "Secret", "new_title": "hijacked"})
    pr = mo_propose(bc, "rename my secret task", "tok-upd-cross").json()
    assert pr["kind"] == "error"  # bob's own search never sees alice's row at all


# --------------------------------------------------------------------------- #
# DELETE — task
# --------------------------------------------------------------------------- #
def test_delete_task_proposal_confirm_deletion_and_idempotency(alice, mock_ai):
    tc, _ = alice
    task = _mk_task(tc, "Buy groceries")
    mock_ai("DELETE_TASK", parameters={"target_description": "groceries"})

    pr = mo_propose(tc, "delete my groceries task", "tok-del-1").json()
    assert pr["kind"] == "proposal_required"
    pid = pr["proposal"]["proposal_id"]
    assert _db_task(task["id"]) is not None          # nothing mutates before Confirm

    c1 = mo_confirm(tc, pid).json()
    assert c1["kind"] == "executed"
    assert _db_task(task["id"]) is None

    # repeated confirm -> same executed result, never a second delete attempt
    for _ in range(3):
        c_again = mo_confirm(tc, pid).json()
        assert c_again["kind"] == "executed"


def test_delete_task_nonexistent_target(alice, mock_ai):
    tc, _ = alice
    mock_ai("DELETE_TASK", parameters={"target_description": "nothing like this exists"})
    pr = mo_propose(tc, "delete my nonexistent task", "tok-del-404").json()
    assert pr["kind"] == "error"


def test_delete_task_cross_user_protection(alice, bob, mock_ai):
    ac, _a = alice
    bc, _b = bob
    task = _mk_task(ac, "Alice Private Task")
    mock_ai("DELETE_TASK", parameters={"target_description": "Private"})
    pr = mo_propose(bc, "delete my private task", "tok-del-cross").json()
    assert pr["kind"] == "error"
    assert _db_task(task["id"]) is not None           # alice's task is untouched


# --------------------------------------------------------------------------- #
# EDIT / DELETE — event
# --------------------------------------------------------------------------- #
def test_update_event_proposal_confirm_and_persistence(alice, mock_ai):
    tc, _ = alice
    start = _now() + dt.timedelta(days=1)
    ev = _mk_event(tc, "Dentist appointment", start, start + dt.timedelta(hours=1))
    new_start = start.replace(hour=17)
    mock_ai("UPDATE_EVENT", parameters={"target_description": "Dentist", "new_start": new_start.isoformat()})

    pr = mo_propose(tc, "move my dentist appointment to 5pm", "tok-evu-1").json()
    assert pr["kind"] == "proposal_required"
    assert _db_event(ev["id"])["start_time"] == start.isoformat()   # unchanged before Confirm

    c1 = mo_confirm(tc, pr["proposal"]["proposal_id"]).json()
    assert c1["kind"] == "executed"
    assert _db_event(ev["id"])["start_time"] == new_start.isoformat()


def test_update_event_close_misspelling_offers_candidate_not_guess(alice, mock_ai):
    """"FYB Presentation" typed/misheard for the real "FYP Presentation" —
    a plain substring search finds nothing. Must offer that ONE close
    candidate as a clarification (never silently act on it, never a flat
    dead-end "couldn't find" with no path forward)."""
    tc, _ = alice
    start = _now() + dt.timedelta(days=1)
    ev = _mk_event(tc, "FYP Presentation", start, start + dt.timedelta(hours=1))
    mock_ai("UPDATE_EVENT", parameters={"target_description": "FYB Presentation", "new_start_time_only": "11:30"})

    pr = mo_propose(tc, "move my FYB presentation to 11:30 am", "tok-evu-typo-1").json()
    assert pr["kind"] == "clarification_required", pr
    assert "FYP Presentation" in pr["message"]
    assert _db_event(ev["id"])["start_time"] == start.isoformat()  # untouched — never guessed


def test_update_event_no_close_candidate_still_flat_error(alice, mock_ai):
    """A target with nothing even close must stay a plain "couldn't find" —
    the candidate-review path must not fire on an unrelated name."""
    tc, _ = alice
    start = _now() + dt.timedelta(days=1)
    _mk_event(tc, "FYP Presentation", start, start + dt.timedelta(hours=1))
    mock_ai("UPDATE_EVENT", parameters={"target_description": "Dentist appointment", "new_start_time_only": "11:30"})

    pr = mo_propose(tc, "move my dentist appointment to 11:30 am", "tok-evu-noguess-1").json()
    assert pr["kind"] == "error", pr
    assert "Did you mean" not in pr["message"]


def test_delete_event_proposal_confirm_and_deletion(alice, mock_ai):
    tc, _ = alice
    start = _now() + dt.timedelta(days=2)
    ev = _mk_event(tc, "Dentist appointment", start, start + dt.timedelta(hours=1))
    mock_ai("DELETE_EVENT", parameters={"target_description": "Dentist"})

    pr = mo_propose(tc, "delete my dentist appointment", "tok-evd-1").json()
    assert pr["kind"] == "proposal_required"
    c1 = mo_confirm(tc, pr["proposal"]["proposal_id"]).json()
    assert c1["kind"] == "executed"
    assert _db_event(ev["id"]) is None


def test_delete_event_nonexistent_target(alice, mock_ai):
    tc, _ = alice
    mock_ai("DELETE_EVENT", parameters={"target_description": "no such event"})
    pr = mo_propose(tc, "delete my nonexistent event", "tok-evd-404").json()
    assert pr["kind"] == "error"


def test_update_event_cross_user_protection(alice, bob, mock_ai):
    ac, _a = alice
    bc, _b = bob
    start = _now() + dt.timedelta(days=1)
    ev = _mk_event(ac, "Alice Only Meeting", start, start + dt.timedelta(hours=1))
    mock_ai("UPDATE_EVENT", parameters={"target_description": "Alice Only", "new_title": "hijacked"})
    pr = mo_propose(bc, "rename alice's meeting", "tok-evu-cross").json()
    assert pr["kind"] == "error"
    assert _db_event(ev["id"])["title"] == "Alice Only Meeting"


# --------------------------------------------------------------------------- #
# GET_TODAY_SUMMARY
# --------------------------------------------------------------------------- #
def test_today_summary_aggregates_real_data(alice, mock_ai):
    tc, _ = alice
    _mk_task(tc, "Open task one")
    start = _now()
    _mk_event(tc, "Team sync", start, start + dt.timedelta(hours=1))
    tc.post("/api/transactions", json={
        "amount": 12, "type": "expense", "category": "Food",
        "description": "lunch", "date": _now().isoformat(),
    })
    mock_ai("GET_TODAY_SUMMARY")
    r = mo_propose(tc, "what do I have today", "tok-today-1").json()
    assert r["kind"] == "answer"
    assert "Team sync" in r["message"]
    assert "RM 12.00" in r["message"] or "RM 12" in r["message"]


def test_today_summary_empty_state(alice, mock_ai):
    tc, _ = alice
    mock_ai("GET_TODAY_SUMMARY")
    r = mo_propose(tc, "what do I have today", "tok-today-empty").json()
    assert r["kind"] == "answer"
    assert "clear schedule" in r["message"]
    assert "RM 0.00" in r["message"] or "RM 0" in r["message"]


# --------------------------------------------------------------------------- #
# SAFETY
# --------------------------------------------------------------------------- #
def test_unsupported_action_remains_blocked_with_accurate_capability_copy(alice, mock_ai):
    tc, _ = alice
    mock_ai("ACTION_NOT_SUPPORTED")
    r = mo_propose(tc, "book me a flight to Tokyo", "tok-unsup-1").json()
    assert r["kind"] == "unsupported"
    # the fallback must name real, current capabilities, not the old
    # navigation-only "I can open Schedule/Activity/Wallet" copy.
    assert "task" in r["message"].lower()
    assert "event" in r["message"].lower()


def test_update_and_delete_never_mutate_before_confirm(alice, mock_ai):
    tc, _ = alice
    task = _mk_task(tc, "Untouched until confirm")
    mock_ai("DELETE_TASK", parameters={"target_description": "Untouched"})
    mo_propose(tc, "delete my untouched task", "tok-safety-1")
    assert _db_task(task["id"]) is not None


def test_repeated_confirm_on_update_task_does_not_reapply_twice(alice, mock_ai):
    tc, _ = alice
    task = _mk_task(tc, "Rename me once")
    mock_ai("UPDATE_TASK", parameters={"target_description": "Rename me", "new_title": "Renamed once"})
    pr = mo_propose(tc, "rename my task", "tok-safety-2").json()
    pid = pr["proposal"]["proposal_id"]
    for _ in range(3):
        c = mo_confirm(tc, pid).json()
        assert c["kind"] == "executed"
    assert _db_task(task["id"])["title"] == "Renamed once"
