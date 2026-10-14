"""P0 CORRECTNESS FIXES — PROPOSAL FIDELITY + CONTEXT FIDELITY.

Two confirmed correctness problems fixed together:

  1. PROPOSAL FIDELITY. UPDATE_EVENT (a "move X to <time>" request) had NO
     conflict detection at all — a move into an already-occupied slot was
     silently proposed AND persisted with no warning. Fixed: the exact
     requested time is checked BEFORE the proposal is built (a conflict is
     surfaced, never silently substituted), re-validated again at Confirm
     (the schedule may have changed since the proposal was built — Confirm
     can only answer "is this EXACT change still safe?", never pick a
     different value), and the persisted row is compared against the
     confirmed fields after writing (never claim success on a silent
     mismatch).

  2. CONTEXT FIDELITY. A contextual mutating suggestion ("Block 45 min for
     X") generated from a schedule-summary answer about a SPECIFIC day (e.g.
     "tomorrow") silently recomputed its candidate slot anchored to "now" the
     moment it was actually clicked, losing the day it was generated for.
     Fixed: the resolved day travels INSIDE the suggestion's own signed token
     and is reused at click time, never re-derived from ambient "now".

Temp DB via conftest; the AI boundary is `mock_ai`/`mock_ai_interp` (no real
model call) — same pattern as tests/test_mo_multi_operation.py.
"""
import datetime as dt

import db as db_layer
import mo_proposals as mo
import security_config as sec
from conftest import mo_propose, mo_confirm, mo_pending

MYT = dt.timezone(dt.timedelta(hours=8))


def _now():
    return dt.datetime.now(MYT).replace(minute=0, second=0, microsecond=0)


def _mk_event(tc, title, start, end):
    r = tc.post("/api/events", json={"title": title, "start": start.isoformat(), "end": end.isoformat()})
    assert r.status_code == 201, r.text
    return r.json()["event"]


def _mk_task(tc, title, **extra):
    body = {"title": title, **extra}
    r = tc.post("/api/tasks", json=body)
    assert r.status_code == 201, r.text
    return r.json()["task"]


def _db_event(event_id):
    c = db_layer.get_connection()
    try:
        r = c.execute("SELECT * FROM schedule_events WHERE id = ?", (event_id,)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


def _task_row(uid, tid):
    c = db_layer.get_connection()
    try:
        r = c.execute("SELECT * FROM tasks WHERE id=? AND user_id=?", (tid, uid)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


def _uid(tc):
    return tc.get("/api/auth/me").json()["user"]["id"]


def _op(intent, **params):
    return {"intent": intent, "parameters": params}


def _sug(answer, kind):
    for s in answer.get("mutating_suggestions", []):
        if s["kind"] == kind:
            return s
    return None


def _ask_schedule_for(tc, mock_ai_interp, target_date):
    mock_ai_interp("read", operations=[_op("GET_SCHEDULE_SUMMARY", target_date=target_date)])
    r = tc.post("/api/process-input", json={"user_input": "what does that day look like"})
    assert r.status_code == 200, r.text
    return r.json()


def _prepare(tc, token):
    return tc.post(f"/api/process-input/suggested/{token}")


# =========================================================================== #
# BUG #1 — PROPOSAL FIDELITY: moving an event into a real conflict
# =========================================================================== #
def test_move_into_a_conflict_is_surfaced_never_silently_persisted(alice, mock_ai_interp):
    tc, _ = alice
    d = _now()
    gym = _mk_event(tc, "Gym", d.replace(hour=20), d.replace(hour=21))
    _mk_event(tc, "Car oil change", d.replace(hour=18), d.replace(hour=19))

    mock_ai_interp("single_operation", operations=[
        _op("UPDATE_EVENT", target_description="Gym", new_start=d.replace(hour=18, minute=30).isoformat()),
    ])
    r = mo_propose(tc, "move gym to 6:30", "tok-p0-conflict-1").json()
    assert r["kind"] == "error"                          # never a proposal for a conflicting move
    assert "Car oil change" in r["message"]
    assert "6:30" in r["message"]

    assert _db_event(gym["id"])["start_time"] == d.replace(hour=20).isoformat()   # untouched


def test_move_into_a_conflict_message_names_a_safe_alternative_without_applying_it(alice, mock_ai_interp):
    tc, _ = alice
    d = _now()
    gym = _mk_event(tc, "Gym", d.replace(hour=20), d.replace(hour=21))
    _mk_event(tc, "Car oil change", d.replace(hour=18), d.replace(hour=19))

    mock_ai_interp("single_operation", operations=[
        _op("UPDATE_EVENT", target_description="Gym", new_start=d.replace(hour=18, minute=30).isoformat()),
    ])
    r = mo_propose(tc, "move gym to 6:30", "tok-p0-conflict-2").json()
    assert r["kind"] == "error"
    assert "7:00" in r["message"]                          # a safe alternative is NAMED...
    assert _db_event(gym["id"])["start_time"] == d.replace(hour=20).isoformat()  # ...never auto-applied


def test_user_can_explicitly_choose_the_alternative_and_it_persists_exactly(alice, mock_ai_interp):
    tc, _ = alice
    d = _now()
    gym = _mk_event(tc, "Gym", d.replace(hour=20), d.replace(hour=21))
    _mk_event(tc, "Car oil change", d.replace(hour=18), d.replace(hour=19))

    mock_ai_interp("single_operation", operations=[
        _op("UPDATE_EVENT", target_description="Gym", new_start=d.replace(hour=19).isoformat()),
    ])
    r = mo_propose(tc, "move gym to 7", "tok-p0-alt-choice").json()
    assert r["kind"] == "proposal_required"
    items = r["proposal"]["display"]["items"]
    assert items[0]["when"].endswith("07:00 PM")

    c1 = mo_confirm(tc, r["proposal"]["proposal_id"]).json()
    assert c1["kind"] == "executed"
    assert _db_event(gym["id"])["start_time"] == d.replace(hour=19).isoformat()   # EXACT chosen value


def test_user_can_instead_ask_to_move_the_conflicting_event(alice, mock_ai_interp):
    """Section 8 Option B is not auto-built as a dual-choice proposal — but the
    workaround it points to (ask to move the OTHER item) still works as an
    ordinary, independent request."""
    tc, _ = alice
    d = _now()
    _mk_event(tc, "Gym", d.replace(hour=20), d.replace(hour=21))
    oil = _mk_event(tc, "Car oil change", d.replace(hour=18), d.replace(hour=19))

    mock_ai_interp("single_operation", operations=[
        _op("UPDATE_EVENT", target_description="Car oil change", new_start=d.replace(hour=16).isoformat()),
    ])
    r = mo_propose(tc, "move car oil change to 4", "tok-p0-move-other").json()
    assert r["kind"] == "proposal_required"
    mo_confirm(tc, r["proposal"]["proposal_id"])
    assert _db_event(oil["id"])["start_time"] == d.replace(hour=16).isoformat()


def test_conflict_check_excludes_the_events_own_current_slot(alice, mock_ai_interp):
    """A move must never be reported as conflicting with the event's OWN
    current occupied time."""
    tc, _ = alice
    d = _now()
    _mk_event(tc, "Gym", d.replace(hour=20), d.replace(hour=21))

    mock_ai_interp("single_operation", operations=[
        _op("UPDATE_EVENT", target_description="Gym", new_start=d.replace(hour=20, minute=15).isoformat()),
    ])
    r = mo_propose(tc, "move gym to 8:15", "tok-p0-self-exclude").json()
    assert r["kind"] == "proposal_required"       # not rejected as "conflicting with itself"


def test_updating_a_non_time_field_never_triggers_the_conflict_check(alice, mock_ai_interp):
    tc, _ = alice
    d = _now()
    gym = _mk_event(tc, "Gym", d.replace(hour=20), d.replace(hour=21))
    _mk_event(tc, "Car oil change", d.replace(hour=18), d.replace(hour=19))

    mock_ai_interp("single_operation", operations=[
        _op("UPDATE_EVENT", target_description="Gym", new_title="Gym Session"),
    ])
    r = mo_propose(tc, "rename gym to Gym Session", "tok-p0-title-only").json()
    assert r["kind"] == "proposal_required"
    mo_confirm(tc, r["proposal"]["proposal_id"])
    assert _db_event(gym["id"])["title"] == "Gym Session"


def test_confirm_time_revalidation_blocks_a_conflict_that_appeared_after_the_proposal(alice, mock_ai_interp):
    """A new conflicting event is created AFTER the proposal was built but
    BEFORE Confirm — Confirm must catch it and go stale, never silently move
    Gym into the now-conflicting time and never silently pick another time."""
    tc, _ = alice
    d = _now()
    gym = _mk_event(tc, "Gym", d.replace(hour=20), d.replace(hour=21))

    mock_ai_interp("single_operation", operations=[
        _op("UPDATE_EVENT", target_description="Gym", new_start=d.replace(hour=18, minute=30).isoformat()),
    ])
    r = mo_propose(tc, "move gym to 6:30", "tok-p0-race").json()
    assert r["kind"] == "proposal_required"

    # a NEW conflicting event appears between proposal and confirm
    _mk_event(tc, "Car oil change", d.replace(hour=18), d.replace(hour=19))

    c1 = mo_confirm(tc, r["proposal"]["proposal_id"]).json()
    assert c1["kind"] == "stale"
    assert "review it again" in c1["message"].lower()
    assert _db_event(gym["id"])["start_time"] == d.replace(hour=20).isoformat()   # UNCHANGED

    # a repeated confirm keeps answering the same honest way — never executes.
    c2 = mo_confirm(tc, r["proposal"]["proposal_id"]).json()
    assert c2["kind"] == "stale"
    assert _db_event(gym["id"])["start_time"] == d.replace(hour=20).isoformat()


def test_batch_move_and_create_display_matches_persisted_for_both(alice, mock_ai_interp):
    tc, _ = alice
    d = _now()
    gym = _mk_event(tc, "Gym", d.replace(hour=20), d.replace(hour=21))

    mock_ai_interp("multi_operation", operations=[
        _op("UPDATE_EVENT", target_description="Gym", new_start=d.replace(hour=18, minute=30).isoformat()),
        _op("CREATE_EVENT", title="Dinner", start=d.replace(hour=20).isoformat(), end=None, all_day=False),
    ])
    r = mo_propose(tc, "move gym to 6:30 and add dinner at 8", "tok-p0-batch-fidelity").json()
    assert r["kind"] == "proposal_required"
    items = r["proposal"]["display"]["items"]
    gym_item = next(it for it in items if it["title"] == "Gym")
    dinner_item = next(it for it in items if it["title"] == "Dinner")
    assert gym_item["when"].endswith("06:30 PM")
    assert dinner_item["when"].endswith("08:00 PM")

    mo_confirm(tc, r["proposal"]["proposal_id"])
    assert _db_event(gym["id"])["start_time"] == d.replace(hour=18, minute=30).isoformat()
    dinner_rows = [e for e in tc.get("/api/schedule").json()["all_events"] if e["title"] == "Dinner"]
    assert len(dinner_rows) == 1
    assert dinner_rows[0]["start_time"] == d.replace(hour=20).isoformat()


def test_repeated_confirm_of_a_time_move_is_idempotent(alice, mock_ai_interp):
    tc, _ = alice
    d = _now()
    gym = _mk_event(tc, "Gym", d.replace(hour=20), d.replace(hour=21))
    mock_ai_interp("single_operation", operations=[
        _op("UPDATE_EVENT", target_description="Gym", new_start=d.replace(hour=18, minute=30).isoformat()),
    ])
    r = mo_propose(tc, "move gym to 6:30", "tok-p0-idem").json()
    pid = r["proposal"]["proposal_id"]
    c1 = mo_confirm(tc, pid).json()
    assert c1["kind"] == "executed"
    c2 = mo_confirm(tc, pid).json()
    assert c2["kind"] == "executed"
    assert _db_event(gym["id"])["start_time"] == d.replace(hour=18, minute=30).isoformat()


def test_ambiguous_conflicting_target_still_clarifies_never_guesses(alice, mock_ai_interp):
    """Pre-existing invariant, re-verified after this patch: multiple matching
    events for the SAME move request still produce a clarification, never an
    arbitrary pick — this fix must not weaken target resolution."""
    tc, _ = alice
    d = _now()
    _mk_event(tc, "Gym", d.replace(hour=8), d.replace(hour=9))
    _mk_event(tc, "Gym", d.replace(hour=20), d.replace(hour=21))
    mock_ai_interp("single_operation", operations=[
        _op("UPDATE_EVENT", target_description="Gym", new_start=d.replace(hour=18, minute=30).isoformat()),
    ])
    r = mo_propose(tc, "move gym to 6:30", "tok-p0-ambiguous").json()
    assert r["kind"] == "clarification_required"


# =========================================================================== #
# BUG #2 — CONTEXT FIDELITY: a contextual suggestion must keep its own date
# =========================================================================== #
def test_suggestion_from_tomorrows_summary_targets_tomorrow_when_used(alice, mock_ai_interp):
    tc, _ = alice
    d_tomorrow = (_now() + dt.timedelta(days=1)).date().isoformat()
    _mk_task(tc, "Work on my FYP", due=(_now() + dt.timedelta(days=5)).isoformat())

    ans = _ask_schedule_for(tc, mock_ai_interp, d_tomorrow)
    s = _sug(ans, "schedule_task_block")
    assert s is not None, ans

    r = _prepare(tc, s["token"])
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["kind"] == "proposal_required"
    # the candidate's own planned_at date must be tomorrow, not today
    proposal_id = j["proposal"]["proposal_id"]
    conn = db_layer.get_connection()
    try:
        row = conn.execute("SELECT payload_json FROM mo_proposals WHERE id=?", (proposal_id,)).fetchone()
    finally:
        conn.close()
    import json as _json
    planned_at = _json.loads(row["payload_json"])["planned_at"]
    assert planned_at[:10] == d_tomorrow


def test_suggestion_from_an_explicit_monday_preserves_monday(alice, mock_ai_interp):
    tc, _ = alice
    d = _now()
    monday = d + dt.timedelta(days=(7 - d.weekday()) % 7 or 7)
    monday_date = monday.date().isoformat()
    _mk_task(tc, "Read chapter 5", due=(d + dt.timedelta(days=10)).isoformat())

    ans = _ask_schedule_for(tc, mock_ai_interp, monday_date)
    s = _sug(ans, "schedule_task_block")
    assert s is not None, ans
    r = _prepare(tc, s["token"])
    j = r.json()
    assert j["kind"] == "proposal_required"

    import json as _json
    conn = db_layer.get_connection()
    try:
        row = conn.execute(
            "SELECT payload_json FROM mo_proposals WHERE id=?", (j["proposal"]["proposal_id"],)
        ).fetchone()
    finally:
        conn.close()
    assert _json.loads(row["payload_json"])["planned_at"][:10] == monday_date


def test_a_suggestion_still_targets_its_own_embedded_date_even_after_mo_context_is_cleared(alice, mock_ai_interp):
    """Context travels WITH the action (Section 13) — the suggestion must not
    depend on short-term mo_context still being valid at click time."""
    tc, _ = alice
    d_tomorrow = (_now() + dt.timedelta(days=1)).date().isoformat()
    _mk_task(tc, "Work on my FYP", due=(_now() + dt.timedelta(days=5)).isoformat())

    ans = _ask_schedule_for(tc, mock_ai_interp, d_tomorrow)
    s = _sug(ans, "schedule_task_block")
    assert s is not None

    # simulate mo_context having moved on / expired entirely
    conn = db_layer.get_connection()
    try:
        conn.execute("DELETE FROM mo_context WHERE user_id=?", (_uid(tc),))
        conn.commit()
    finally:
        conn.close()

    r = _prepare(tc, s["token"])
    j = r.json()
    assert j["kind"] == "proposal_required"
    import json as _json
    conn = db_layer.get_connection()
    try:
        row = conn.execute(
            "SELECT payload_json FROM mo_proposals WHERE id=?", (j["proposal"]["proposal_id"],)
        ).fetchone()
    finally:
        conn.close()
    assert _json.loads(row["payload_json"])["planned_at"][:10] == d_tomorrow


def test_generic_navigation_suggestions_never_carry_a_schedule_date(alice, mock_ai_interp):
    tc, _ = alice
    mock_ai_interp("unsupported", operations=[], unsupported_items=["buying a phone"])
    r = mo_propose(tc, "buy me a phone", "tok-p0-nav")
    j = r.json()
    for s in j.get("suggested_actions", []):
        assert "date" not in s and "target_date" not in s


def test_two_users_different_active_dates_no_context_leakage(alice, bob, mock_ai_interp):
    tc_a, _ = alice
    tc_b, _ = bob
    d_tomorrow = (_now() + dt.timedelta(days=1)).date().isoformat()
    d_in_5 = (_now() + dt.timedelta(days=5)).date().isoformat()
    _mk_task(tc_a, "Alice task", due=(_now() + dt.timedelta(days=20)).isoformat())
    _mk_task(tc_b, "Bob task", due=(_now() + dt.timedelta(days=20)).isoformat())

    ans_a = _ask_schedule_for(tc_a, mock_ai_interp, d_tomorrow)
    s_a = _sug(ans_a, "schedule_task_block")
    ans_b = _ask_schedule_for(tc_b, mock_ai_interp, d_in_5)
    s_b = _sug(ans_b, "schedule_task_block")
    assert s_a is not None and s_b is not None
    assert s_a["token"] != s_b["token"]

    # a token forged/reused across users must never resolve for the other user
    r_cross = tc_b.post(f"/api/process-input/suggested/{s_a['token']}")
    assert r_cross.status_code == 409

    import json as _json
    ja = tc_a.post(f"/api/process-input/suggested/{s_a['token']}").json()
    jb = tc_b.post(f"/api/process-input/suggested/{s_b['token']}").json()
    conn = db_layer.get_connection()
    try:
        row_a = conn.execute("SELECT payload_json FROM mo_proposals WHERE id=?",
                             (ja["proposal"]["proposal_id"],)).fetchone()
        row_b = conn.execute("SELECT payload_json FROM mo_proposals WHERE id=?",
                             (jb["proposal"]["proposal_id"],)).fetchone()
    finally:
        conn.close()
    assert _json.loads(row_a["payload_json"])["planned_at"][:10] == d_tomorrow
    assert _json.loads(row_b["payload_json"])["planned_at"][:10] == d_in_5
