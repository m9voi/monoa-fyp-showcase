"""Mo final intelligence + multi-operation closure.

Temp DB via conftest; the AI boundary is the `mock_ai_interp` fixture (no real
model call) — it patches AIService.interpret with a hand-built
MoInterpretation(mode, operations=[...]) so these tests exercise the SAME
deterministic resolution/conflict/execution code the real OpenAI-backed path
uses, without ever calling OpenAI. `mock_ai` (single-intent) tests continue to
live in test_mo_hitl.py / test_mo_edit_delete.py, unchanged.

Rules under test:
  - a batch is ONE mo_proposals row (action_type="batch_operations")
  - every requested item ends in included / conflict / clarification / error —
    never silently dropped
  - HARD conflicts are never silently created; SOFT conflicts may be nudged to
    a nearby free slot (a small bounded search, not a solver)
  - plan_day recognizes an item that's already on the real schedule instead of
    duplicating it
  - target resolution narrows ambiguity using structured date/time hints and,
    as a last resort, short-term per-user conversational context — never a
    guess, never an AI-supplied id
  - a batch confirms/executes exactly once and reports partial failure
    honestly rather than claiming full success
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


def _db_event(event_id):
    c = db_layer.get_connection()
    try:
        r = c.execute("SELECT * FROM schedule_events WHERE id = ?", (event_id,)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


def _events_by_title(tc, title):
    all_ev = tc.get("/api/schedule").json()["all_events"]
    return [e for e in all_ev if e["title"] == title]


def _tasks_by_title(user_id, title):
    c = db_layer.get_connection()
    try:
        rows = c.execute(
            "SELECT * FROM tasks WHERE user_id = ? AND title = ? AND is_completed = 0",
            (user_id, title),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


def _op(intent, **params):
    return {"intent": intent, "parameters": params}


# --------------------------------------------------------------------------- #
# MULTI CREATE
# --------------------------------------------------------------------------- #
def test_two_item_multi_create_both_represented_and_persisted(alice, mock_ai_interp):
    tc, _ = alice
    d = _now() + dt.timedelta(days=1)
    mock_ai_interp("multi_operation", operations=[
        _op("CREATE_EVENT", title="Meeting", start=d.replace(hour=10).isoformat(), end=None, all_day=False),
        _op("CREATE_EVENT", title="Lunch", start=d.replace(hour=14).isoformat(), end=None, all_day=False),
    ])
    r = mo_propose(tc, "add meeting at 10 and lunch at 2", "tok-multi-2").json()
    assert r["kind"] == "proposal_required"
    pid = r["proposal"]["proposal_id"]
    assert r["proposal"]["action_type"] == "batch_operations"
    titles = [it["title"] for it in r["proposal"]["display"]["items"]]
    assert titles == ["Meeting", "Lunch"]

    c1 = mo_confirm(tc, pid).json()
    assert c1["kind"] == "executed"
    assert len(_events_by_title(tc, "Meeting")) == 1
    assert len(_events_by_title(tc, "Lunch")) == 1


def test_four_item_real_world_phrasing_all_represented(alice, mock_ai_interp):
    tc, _ = alice
    d = _now() + dt.timedelta(days=1)
    mock_ai_interp("multi_operation", operations=[
        _op("CREATE_EVENT", title="Meeting", start=d.replace(hour=10).isoformat(), end=None, all_day=False),
        _op("CREATE_EVENT", title="Lunch", start=d.replace(hour=14).isoformat(), end=None, all_day=False),
        _op("CREATE_EVENT", title="Car oil change", start=d.replace(hour=18).isoformat(), end=None, all_day=False),
        _op("CREATE_EVENT", title="Gym", start=d.replace(hour=20).isoformat(), end=None, all_day=False),
    ])
    r = mo_propose(
        tc,
        "I have a meeting at 10 AM, lunch at 2 PM, car oil change at 6 PM, and gym at 8 PM. Add them to my day.",
        "tok-multi-4",
    ).json()
    assert r["kind"] == "proposal_required"
    titles = [it["title"] for it in r["proposal"]["display"]["items"]]
    assert titles == ["Meeting", "Lunch", "Car oil change", "Gym"]   # NONE silently dropped

    c1 = mo_confirm(tc, r["proposal"]["proposal_id"]).json()
    assert c1["kind"] == "executed"
    for t in titles:
        assert len(_events_by_title(tc, t)) == 1


# --------------------------------------------------------------------------- #
# MIXED OPERATIONS
# --------------------------------------------------------------------------- #
def test_mixed_create_update_delete_one_combined_proposal(alice, mock_ai_interp):
    tc, _ = alice
    d = _now() + dt.timedelta(days=1)
    meeting = _mk_event(tc, "Team meeting", d.replace(hour=9), d.replace(hour=10))
    dentist = _mk_event(tc, "Dentist appointment", d.replace(hour=15), d.replace(hour=16))

    mock_ai_interp("multi_operation", operations=[
        _op("UPDATE_EVENT", target_description="Team meeting", new_start=d.replace(hour=17).isoformat()),
        _op("DELETE_EVENT", target_description="Dentist"),
        _op("CREATE_EVENT", title="Gym", start=(d + dt.timedelta(days=1)).replace(hour=20).isoformat(),
            end=None, all_day=False),
    ])
    r = mo_propose(tc, "move meeting to 5, delete dentist, add gym tomorrow at 8", "tok-mixed-1").json()
    assert r["kind"] == "proposal_required"
    ops_kinds = [it["op"] for it in r["proposal"]["display"]["items"]]
    assert ops_kinds == ["change", "delete", "add"]

    c1 = mo_confirm(tc, r["proposal"]["proposal_id"]).json()
    assert c1["kind"] == "executed"
    assert _db_event(meeting["id"])["start_time"] == d.replace(hour=17).isoformat()
    assert _db_event(dentist["id"]) is None
    assert len(_events_by_title(tc, "Gym")) == 1


# --------------------------------------------------------------------------- #
# PLAN_DAY — conflicts, constraints, duplicate detection
# --------------------------------------------------------------------------- #
def test_plan_day_hard_conflict_is_excluded_not_silently_created(alice, mock_ai_interp):
    tc, _ = alice
    d = _now() + dt.timedelta(days=1)
    _mk_event(tc, "Existing meeting", d.replace(hour=9), d.replace(hour=11))

    mock_ai_interp("plan_day", narrative="Here's a plan for the day.", operations=[
        _op("CREATE_EVENT", title="Football", start=d.replace(hour=10).isoformat(), end=None, all_day=False),
        _op("CREATE_EVENT", title="Recovery", start=d.replace(hour=19).isoformat(), end=None, all_day=False),
    ])
    r = mo_propose(tc, "plan my day: football at 10, recovery at 7", "tok-plan-conflict").json()
    assert r["kind"] == "proposal_required"
    titles = [it["title"] for it in r["proposal"]["display"]["items"]]
    assert titles == ["Recovery"]                       # Football excluded, never silently created
    # Section 4/5: the conflict is NAMED, but as a SEPARATE secondary note —
    # never mixed into the primary conversational summary sentence.
    assert "Football" in (r["proposal"]["display"].get("note") or "")
    assert "Football" not in r["proposal"]["summary"]

    mo_confirm(tc, r["proposal"]["proposal_id"])
    assert _events_by_title(tc, "Football") == []        # confirmed only what was proposed


def test_plan_day_soft_conflict_is_nudged_to_a_free_slot(alice, mock_ai_interp):
    tc, _ = alice
    d = _now() + dt.timedelta(days=1)
    _mk_event(tc, "Existing meeting", d.replace(hour=18), d.replace(hour=19))

    mock_ai_interp("plan_day", operations=[
        _op("CREATE_EVENT", title="Dinner", start=d.replace(hour=18, minute=30).isoformat(),
            end=d.replace(hour=19, minute=30).isoformat(), all_day=False)
        | {"constraint": "soft"},
    ])
    r = mo_propose(tc, "plan my day: dinner around 6:30", "tok-plan-soft").json()
    assert r["kind"] == "proposal_required"
    item = r["proposal"]["display"]["items"][0]
    assert item["title"] == "Dinner"
    assert item.get("old_when")                          # explicitly marked as moved

    c1 = mo_confirm(tc, r["proposal"]["proposal_id"]).json()
    assert c1["kind"] == "executed"
    dinner_id = _events_by_title(tc, "Dinner")[0]["id"]
    assert _db_event(dinner_id)["start_time"] != d.replace(hour=18, minute=30).isoformat()  # actually moved


def test_plan_day_recognizes_existing_item_as_duplicate(alice, mock_ai_interp):
    tc, _ = alice
    d = _now() + dt.timedelta(days=1)
    _mk_event(tc, "Meeting", d.replace(hour=9), d.replace(hour=10))

    mock_ai_interp("plan_day", operations=[
        _op("CREATE_EVENT", title="Meeting", start=d.replace(hour=9).isoformat(), end=None, all_day=False),
        _op("CREATE_EVENT", title="Lunch", start=d.replace(hour=13).isoformat(), end=None, all_day=False),
    ])
    r = mo_propose(tc, "plan my day: meeting at 9, lunch at 1", "tok-plan-dup").json()
    assert r["kind"] == "proposal_required"
    titles = [it["title"] for it in r["proposal"]["display"]["items"]]
    assert titles == ["Lunch"]                            # Meeting recognized as already scheduled

    mo_confirm(tc, r["proposal"]["proposal_id"])
    assert len(_events_by_title(tc, "Meeting")) == 1      # still exactly one — no duplicate made


# --------------------------------------------------------------------------- #
# READ + CONDITIONAL ACTION ("if_free")
# --------------------------------------------------------------------------- #
def test_read_and_conditional_add_when_free(alice, mock_ai_interp):
    tc, _ = alice
    d = _now() + dt.timedelta(days=1)
    mock_ai_interp("multi_operation", operations=[
        _op("GET_SCHEDULE_SUMMARY"),
        _op("CREATE_EVENT", title="Gym", start=d.replace(hour=20).isoformat(), end=None, all_day=False)
        | {"condition": "if_free"},
    ])
    r = mo_propose(tc, "what do I have tomorrow, add gym at 8pm if free", "tok-cond-free").json()
    assert r["kind"] == "proposal_required"
    titles = [it["title"] for it in r["proposal"]["display"]["items"]]
    assert titles == ["Gym"]


def test_read_and_conditional_add_when_conflicted_no_blind_creation(alice, mock_ai_interp):
    tc, _ = alice
    d = _now() + dt.timedelta(days=1)
    _mk_event(tc, "Busy", d.replace(hour=19, minute=30), d.replace(hour=20, minute=30))

    mock_ai_interp("multi_operation", operations=[
        _op("GET_SCHEDULE_SUMMARY"),
        _op("CREATE_EVENT", title="Gym", start=d.replace(hour=20).isoformat(), end=None, all_day=False)
        | {"condition": "if_free"},
    ])
    r = mo_propose(tc, "what do I have tomorrow, add gym at 8pm if free", "tok-cond-busy").json()
    assert r["kind"] != "proposal_required"               # not blindly created
    assert _events_by_title(tc, "Gym") == []


# --------------------------------------------------------------------------- #
# TARGET RESOLUTION — structured discriminators + short-term context
# --------------------------------------------------------------------------- #
def test_target_date_discriminator_disambiguates_same_title(alice, mock_ai_interp):
    tc, _ = alice
    monday = _now() + dt.timedelta(days=1)
    thursday = _now() + dt.timedelta(days=4)
    _mk_event(tc, "Gym", monday.replace(hour=20), monday.replace(hour=21))
    ev_thu = _mk_event(tc, "Gym", thursday.replace(hour=20), thursday.replace(hour=21))

    mock_ai_interp("single_operation", operations=[
        _op("UPDATE_EVENT", target_description="Gym",
            target_date=thursday.date().isoformat(), new_start=thursday.replace(hour=19).isoformat()),
    ])
    r = mo_propose(tc, "move thursday's gym to 7", "tok-target-date").json()
    assert r["kind"] == "proposal_required"
    mo_confirm(tc, r["proposal"]["proposal_id"])
    assert _db_event(ev_thu["id"])["start_time"] == thursday.replace(hour=19).isoformat()


def test_recent_context_narrows_ambiguous_target(alice, mock_ai_interp):
    tc, _ = alice
    monday = _now() + dt.timedelta(days=1)
    thursday = _now() + dt.timedelta(days=4)
    ev_mon = _mk_event(tc, "Gym", monday.replace(hour=20), monday.replace(hour=21))
    _mk_event(tc, "Gym", thursday.replace(hour=20), thursday.replace(hour=21))

    # Turn 1 — establish Monday as the conversational anchor via a real proposal.
    mock_ai_interp("single_operation", operations=[
        _op("CREATE_TASK", title="Prep for Monday", due_date=monday.isoformat(), reminder_at=None),
    ])
    mo_propose(tc, "remind me to prep for Monday", "tok-ctx-seed")

    # Turn 2 — no target_date given; short-term context should narrow to Monday's Gym.
    mock_ai_interp("single_operation", operations=[
        _op("UPDATE_EVENT", target_description="Gym", new_start=monday.replace(hour=19).isoformat()),
    ])
    r = mo_propose(tc, "move gym to 7", "tok-ctx-use").json()
    assert r["kind"] == "proposal_required"
    mo_confirm(tc, r["proposal"]["proposal_id"])
    assert _db_event(ev_mon["id"])["start_time"] == monday.replace(hour=19).isoformat()


# --------------------------------------------------------------------------- #
# SAFETY — idempotency, partial failure, cross-user
# --------------------------------------------------------------------------- #
def test_batch_confirm_is_idempotent_across_repeats(alice, mock_ai_interp):
    tc, _ = alice
    d = _now() + dt.timedelta(days=1)
    mock_ai_interp("multi_operation", operations=[
        _op("CREATE_EVENT", title="Meeting", start=d.replace(hour=10).isoformat(), end=None, all_day=False),
        _op("CREATE_EVENT", title="Lunch", start=d.replace(hour=14).isoformat(), end=None, all_day=False),
    ])
    r = mo_propose(tc, "add meeting at 10 and lunch at 2", "tok-idem-batch").json()
    pid = r["proposal"]["proposal_id"]
    for _ in range(3):
        c = mo_confirm(tc, pid).json()
        assert c["kind"] == "executed"
    assert len(_events_by_title(tc, "Meeting")) == 1
    assert len(_events_by_title(tc, "Lunch")) == 1


def test_batch_partial_failure_reported_honestly(alice, mock_ai_interp):
    tc, _ = alice
    d = _now() + dt.timedelta(days=1)
    doomed = _mk_event(tc, "Doomed", d.replace(hour=9), d.replace(hour=10))

    mock_ai_interp("multi_operation", operations=[
        _op("CREATE_EVENT", title="Lunch", start=d.replace(hour=14).isoformat(), end=None, all_day=False),
        _op("DELETE_EVENT", target_description="Doomed"),
    ])
    r = mo_propose(tc, "add lunch at 2 and delete doomed", "tok-partial-fail").json()
    pid = r["proposal"]["proposal_id"]

    # the target vanishes (another client deleted it) BEFORE Confirm.
    assert tc.delete(f"/api/events/{doomed['id']}").status_code == 200

    c1 = mo_confirm(tc, pid).json()
    assert c1["kind"] == "executed"                       # at least one item succeeded
    assert "Lunch" in c1["message"]
    assert len(_events_by_title(tc, "Lunch")) == 1         # the good item still went through


def test_batch_cross_user_protection(alice, bob, mock_ai_interp):
    ac, _a = alice
    bc, _b = bob
    d = _now() + dt.timedelta(days=1)
    mock_ai_interp("multi_operation", operations=[
        _op("CREATE_EVENT", title="Meeting", start=d.replace(hour=10).isoformat(), end=None, all_day=False),
        _op("CREATE_EVENT", title="Lunch", start=d.replace(hour=14).isoformat(), end=None, all_day=False),
    ])
    r = mo_propose(ac, "add meeting at 10 and lunch at 2", "tok-batch-cross").json()
    pid = r["proposal"]["proposal_id"]

    assert mo_confirm(bc, pid).status_code == 404
    assert mo_pending(bc).json()["proposal"] is None


# --------------------------------------------------------------------------- #
# FINAL CORRECTNESS PATCH — unsupported items must never be silently dropped
# --------------------------------------------------------------------------- #
def test_supported_and_unsupported_both_represented(alice, mock_ai_interp):
    tc, _ = alice
    d = _now() + dt.timedelta(days=1)
    mock_ai_interp("multi_operation", operations=[
        _op("CREATE_EVENT", title="Gym", start=d.replace(hour=20).isoformat(), end=None, all_day=False),
    ], unsupported_items=["buying football shoes"])
    r = mo_propose(tc, "add gym tomorrow at 8 and buy me new football shoes", "tok-unsup-mix").json()
    assert r["kind"] == "proposal_required"
    assert r["proposal"]["action_type"] == "create_event"          # single-item shape, unchanged
    assert "football shoes" in r["message"]

    mo_confirm(tc, r["proposal"]["proposal_id"])
    assert len(_events_by_title(tc, "Gym")) == 1


def test_multiple_supported_and_one_unsupported(alice, mock_ai_interp):
    tc, _ = alice
    d = _now() + dt.timedelta(days=1)
    mock_ai_interp("multi_operation", operations=[
        _op("CREATE_EVENT", title="Meeting", start=d.replace(hour=10).isoformat(), end=None, all_day=False),
        _op("CREATE_EVENT", title="Gym", start=d.replace(hour=20).isoformat(), end=None, all_day=False),
    ], unsupported_items=["buying football shoes"])
    r = mo_propose(
        tc, "add a meeting at 10, gym at 8, and buy me new football shoes", "tok-unsup-batch"
    ).json()
    assert r["kind"] == "proposal_required"
    assert r["proposal"]["action_type"] == "batch_operations"
    titles = [it["title"] for it in r["proposal"]["display"]["items"]]
    assert titles == ["Meeting", "Gym"]
    assert "football shoes" in r["message"]


def test_unsupported_only_request_names_it(alice, mock_ai_interp):
    tc, _ = alice
    mock_ai_interp("unsupported", operations=[], unsupported_items=["buying a new phone"])
    r = mo_propose(tc, "buy me a new phone", "tok-unsup-only").json()
    assert r["kind"] == "unsupported"
    assert "new phone" in r["message"]


def test_unsupported_portion_is_never_mutated(alice, mock_ai_interp):
    tc, _ = alice
    d = _now() + dt.timedelta(days=1)
    before = len(tc.get("/api/schedule").json()["all_events"])
    mock_ai_interp("multi_operation", operations=[
        _op("CREATE_EVENT", title="Gym", start=d.replace(hour=20).isoformat(), end=None, all_day=False),
    ], unsupported_items=["buying football shoes"])
    r = mo_propose(tc, "add gym tomorrow at 8 and buy me new football shoes", "tok-unsup-nomut").json()
    mo_confirm(tc, r["proposal"]["proposal_id"])

    all_ev = tc.get("/api/schedule").json()["all_events"]
    assert len(all_ev) == before + 1                     # only Gym was created
    assert not any("shoe" in e["title"].lower() for e in all_ev)


def test_unsupported_note_explicit_in_user_facing_message(alice, mock_ai_interp):
    tc, _ = alice
    d = _now() + dt.timedelta(days=1)
    mock_ai_interp("multi_operation", operations=[
        _op("CREATE_EVENT", title="Gym", start=d.replace(hour=20).isoformat(), end=None, all_day=False),
    ], unsupported_items=["buying football shoes"])
    r = mo_propose(tc, "add gym tomorrow at 8 and buy me new football shoes", "tok-unsup-msg").json()
    assert "football shoes" in r["message"]
    assert "can’t" in r["message"] or "can't" in r["message"]


# --------------------------------------------------------------------------- #
# FINAL CORRECTNESS PATCH — a pure read/discussion turn also seeds short-term
# conversational context (not only a turn that happens to create a proposal)
# --------------------------------------------------------------------------- #
def test_pure_read_turn_seeds_context_for_next_turn(alice, mock_ai_interp):
    tc, _ = alice
    monday = _now() + dt.timedelta(days=1)
    thursday = _now() + dt.timedelta(days=4)
    ev_mon = _mk_event(tc, "Gym", monday.replace(hour=20), monday.replace(hour=21))
    ev_thu = _mk_event(tc, "Gym", thursday.replace(hour=20), thursday.replace(hour=21))

    # Turn 1 — a pure discussion/read turn. No proposal is ever created.
    mock_ai_interp("read", operations=[_op("GET_SCHEDULE_SUMMARY", target_date=monday.date().isoformat())])
    r1 = mo_propose(tc, "let's look at Monday", "tok-ctx-read-seed").json()
    assert r1["kind"] == "answer"

    # Turn 2 — no target_date given; short-term context should narrow to Monday's Gym.
    mock_ai_interp("single_operation", operations=[
        _op("UPDATE_EVENT", target_description="Gym", new_start=monday.replace(hour=19).isoformat()),
    ])
    r2 = mo_propose(tc, "move gym from 8 to 7 PM", "tok-ctx-read-use").json()
    assert r2["kind"] == "proposal_required"
    mo_confirm(tc, r2["proposal"]["proposal_id"])

    assert _db_event(ev_mon["id"])["start_time"] == monday.replace(hour=19).isoformat()
    assert _db_event(ev_thu["id"])["start_time"] == thursday.replace(hour=20).isoformat()  # untouched


def test_context_never_bypasses_deterministic_resolution_on_true_ambiguity(alice, mock_ai_interp):
    tc, _ = alice
    monday = _now() + dt.timedelta(days=1)
    _mk_event(tc, "Gym", monday.replace(hour=8), monday.replace(hour=9))
    _mk_event(tc, "Gym", monday.replace(hour=20), monday.replace(hour=21))

    mock_ai_interp("read", operations=[_op("GET_SCHEDULE_SUMMARY", target_date=monday.date().isoformat())])
    mo_propose(tc, "let's look at Monday", "tok-ctx-ambig-seed")

    # two Monday Gym events exist -> context narrows the DAY but not which one.
    mock_ai_interp("single_operation", operations=[
        _op("UPDATE_EVENT", target_description="Gym", new_start=monday.replace(hour=19).isoformat()),
    ])
    r = mo_propose(tc, "move gym to 7", "tok-ctx-ambig-use").json()
    assert r["kind"] == "clarification_required"


# --------------------------------------------------------------------------- #
# REAL-DEVICE CORRECTION — never invent an exact time or a cadence
# --------------------------------------------------------------------------- #
def test_create_event_with_no_start_time_is_never_defaulted_to_now(alice, mock_ai_interp):
    """Backend safety net: if the model ever omits "start" for a CREATE_EVENT
    (it should ask for clarification instead — see the prompt fix — but this
    proves the BACKEND itself has no hidden fallback to current time either)."""
    tc, _ = alice
    mock_ai_interp("single_operation", operations=[
        _op("CREATE_EVENT", title="Gym Session", end=None, all_day=False),  # no "start"
    ])
    r = mo_propose(tc, "make a gym session", "tok-no-start").json()
    assert r["kind"] != "proposal_required"
    assert _events_by_title(tc, "Gym Session") == []


def test_clarification_mode_for_bulk_request_creates_no_proposal_and_no_events(alice, mock_ai_interp):
    """"Make 10 gym sessions." with neither a time nor a cadence given — a
    correctly prompted model returns mode=clarification; verify the backend
    never falls through to a batch proposal or any mutation in that case."""
    tc, _ = alice
    mock_ai_interp("clarification", operations=[],
                    clarification_question="What time do you prefer, and how should I "
                                            "spread the 10 sessions — for example 3–4 times a week?")
    r = mo_propose(tc, "can you make maybe 10 gym sessions", "tok-bulk-clarify").json()
    assert r["kind"] == "clarification_required"
    assert "time" in r["message"] and "spread" in r["message"]
    assert _events_by_title(tc, "Gym Session") == []


def test_plan_day_clarification_for_unknown_time_creates_no_fabricated_event(alice, mock_ai_interp):
    """A plan_day item that genuinely needs a real time slot the user never
    gave must ask for it, never invent a plausible-sounding one."""
    tc, _ = alice
    mock_ai_interp("clarification", operations=[],
                    clarification_question="What time is your class?")
    r = mo_propose(tc, "plan my day: classes, lunch break, and jog for tomorrow", "tok-plan-unknown-time").json()
    assert r["kind"] == "clarification_required"
    assert _events_by_title(tc, "Class") == []


def test_explicit_now_still_works_when_the_model_states_it(alice, mock_ai_interp):
    """Contrast case: when the model DOES resolve an explicit "now" (the user
    actually said "now"/"right now"), that remains a legitimate value — this
    is not blocked by the correction, only silent invention is."""
    tc, _ = alice
    mock_ai_interp("single_operation", operations=[
        _op("CREATE_EVENT", title="Timer start", start="now", end=None, all_day=False),
    ])
    r = mo_propose(tc, "start a timer now", "tok-explicit-now").json()
    assert r["kind"] == "proposal_required"
    assert r["proposal"]["action_type"] == "create_event"


# --------------------------------------------------------------------------- #
# REAL-DEVICE CORRECTION — proposal copy must never imply a mutation already
# happened, and a large uniform batch must not repeat itself
# --------------------------------------------------------------------------- #
def test_proposal_copy_never_uses_past_tense_completion_language(alice, mock_ai_interp):
    tc, _ = alice
    d = _now() + dt.timedelta(days=1)
    mock_ai_interp("plan_day",
                    narrative="I have scheduled your classes, lunch break, and jog for tomorrow.",
                    operations=[
                        _op("CREATE_EVENT", title="Lunch Break", start=d.replace(hour=13).isoformat(),
                            end=None, all_day=False),
                        _op("CREATE_EVENT", title="Jog", start=d.replace(hour=18).isoformat(),
                            end=None, all_day=False),
                    ])
    r = mo_propose(tc, "plan my day: lunch break and jog for tomorrow", "tok-tense").json()
    assert r["kind"] == "proposal_required"
    for field in ("effect", "summary", "message"):
        text = r["proposal"].get(field) if field != "message" else r.get("message")
        assert "I have" not in (text or "")
        assert "scheduled" not in (text or "").lower()


def test_confirmed_copy_may_use_completion_language(alice, mock_ai_interp):
    """Pre-confirmation copy must stay prospective (see the tense test above);
    the POST-confirmation message is a different code path (_mo_exec_batch)
    and is correctly allowed — expected — to say the mutation is done."""
    tc, _ = alice
    d = _now() + dt.timedelta(days=1)
    mock_ai_interp("multi_operation", operations=[
        _op("CREATE_EVENT", title="Gym", start=d.replace(hour=19).isoformat(), end=None, all_day=False),
        _op("CREATE_EVENT", title="Lunch", start=d.replace(hour=13).isoformat(), end=None, all_day=False),
    ])
    r = mo_propose(tc, "add gym at 7pm and lunch at 1pm tomorrow", "tok-confirm-tense").json()
    c = mo_confirm(tc, r["proposal"]["proposal_id"]).json()
    assert c["kind"] == "executed"
    assert "Done" in c["message"]


def test_large_uniform_batch_summary_is_concise_not_repeated(alice, mock_ai_interp):
    tc, _ = alice
    d = _now() + dt.timedelta(days=1)
    ops = [
        _op("CREATE_EVENT", title="Gym Session", start=(d + dt.timedelta(days=i)).replace(hour=19).isoformat(),
            end=None, all_day=False)
        for i in range(10)
    ]
    mock_ai_interp("multi_operation", operations=ops)
    r = mo_propose(tc, "make 10 gym sessions at 7pm daily", "tok-large-batch").json()
    assert r["kind"] == "proposal_required"
    assert len(r["proposal"]["display"]["items"]) == 10          # structured rows keep every item
    assert r["proposal"]["effect"].count("Add") == 1              # prose collapses, not repeated 10x
    assert "10" in r["proposal"]["effect"]
    assert "10" in r["proposal"]["summary"]


# --------------------------------------------------------------------------- #
# SMARTNESS + EXPERIENCE CLOSURE — Bug A: explicit time must survive
# --------------------------------------------------------------------------- #
def test_create_task_preserves_explicit_due_time_not_date_only(alice, mock_ai_interp):
    """A correctly-prompted model resolves "tomorrow at 7 PM" to a full
    due_date datetime, not a bare date — this proves the BACKEND carries that
    full value through untouched (never truncates to date-only)."""
    tc, user = alice
    d = _now() + dt.timedelta(days=1)
    due = d.replace(hour=19)
    mock_ai_interp("single_operation", operations=[
        _op("CREATE_TASK", title="Study session", due_date=due.isoformat(), reminder_at=None),
    ])
    r = mo_propose(tc, "add a study session tomorrow at 7 PM", "tok-time-preserve").json()
    assert r["kind"] == "proposal_required"
    assert r["proposal"]["display"]["due"]                        # a date is shown
    mo_confirm(tc, r["proposal"]["proposal_id"])
    t = _tasks_by_title(user["id"], "Study session")[0]
    assert t["due_date"] == due.isoformat()                       # exact time preserved, not truncated


def test_create_event_preserves_explicit_time_for_session_phrasing(alice, mock_ai_interp):
    tc, _ = alice
    d = _now() + dt.timedelta(days=1)
    start = d.replace(hour=19)
    mock_ai_interp("single_operation", operations=[
        _op("CREATE_EVENT", title="Study session", start=start.isoformat(), end=None, all_day=False),
    ])
    r = mo_propose(tc, "add a study session tomorrow at 7 PM", "tok-time-preserve-ev").json()
    assert r["kind"] == "proposal_required"
    assert r["proposal"]["action_type"] == "create_event"
    assert r["proposal"]["display"]["time"] == "7:00 pm"
    mo_confirm(tc, r["proposal"]["proposal_id"])
    assert _events_by_title(tc, "Study session")[0]["start_time"] == start.isoformat()


def test_deadline_task_keeps_explicit_deadline_time(alice, mock_ai_interp):
    tc, user = alice
    d = _now() + dt.timedelta(days=1)
    due = d.replace(hour=19)
    mock_ai_interp("single_operation", operations=[
        _op("CREATE_TASK", title="Submit assignment", due_date=due.isoformat(), reminder_at=None),
    ])
    r = mo_propose(tc, "submit my assignment by 7 PM tomorrow", "tok-deadline").json()
    mo_confirm(tc, r["proposal"]["proposal_id"])
    t = _tasks_by_title(user["id"], "Submit assignment")[0]
    assert t["due_date"] == due.isoformat()


def test_reminder_keeps_explicit_reminder_time(alice, mock_ai_interp):
    tc, user = alice
    d = _now() + dt.timedelta(days=1)
    remind = d.replace(hour=19)
    mock_ai_interp("single_operation", operations=[
        _op("CREATE_REMINDER", title="Call Mum", due_date=None, reminder_at=remind.isoformat()),
    ])
    r = mo_propose(tc, "remind me at 7 PM tomorrow to call Mum", "tok-reminder").json()
    mo_confirm(tc, r["proposal"]["proposal_id"])
    t = _tasks_by_title(user["id"], "Call Mum")[0]
    assert t["reminder_at"] == remind.isoformat()


# --------------------------------------------------------------------------- #
# SMARTNESS + EXPERIENCE CLOSURE — Bug B: task/reminder duplicate detection
# --------------------------------------------------------------------------- #
def test_plan_day_recognizes_existing_task_as_duplicate(alice, mock_ai_interp):
    tc, user = alice
    d = _now() + dt.timedelta(days=1)
    _mk_task(tc, "Recovery workouts", due_date=d.replace(hour=19).isoformat())

    mock_ai_interp("plan_day", operations=[
        _op("CREATE_EVENT", title="Meeting", start=d.replace(hour=9).isoformat(), end=None, all_day=False),
        _op("CREATE_TASK", title="Recovery workouts", due_date=d.replace(hour=19).isoformat(), reminder_at=None),
    ])
    r = mo_propose(tc, "plan my day: meeting at 9, recovery workouts at 7", "tok-task-dup").json()
    assert r["kind"] == "proposal_required"
    titles = [it["title"] for it in r["proposal"]["display"]["items"]]
    assert titles == ["Meeting"]                                  # Recovery workouts recognized as existing

    mo_confirm(tc, r["proposal"]["proposal_id"])
    recovery = _tasks_by_title(user["id"], "Recovery workouts")
    assert len(recovery) == 1                                     # still exactly one — no duplicate made


def test_single_create_task_duplicate_is_recognized_not_recreated(alice, mock_ai_interp):
    """Duplicate detection applies outside plan_day too — a plain single
    request for an item that already exists is recognized, not duplicated."""
    tc, user = alice
    d = _now() + dt.timedelta(days=1)
    _mk_task(tc, "Recovery workouts", due_date=d.replace(hour=19).isoformat())

    mock_ai_interp("single_operation", operations=[
        _op("CREATE_TASK", title="Recovery workouts", due_date=d.replace(hour=19).isoformat(), reminder_at=None),
    ])
    r = mo_propose(tc, "add recovery workouts tomorrow at 7", "tok-task-dup-single").json()
    assert r["kind"] == "answer"                                  # informational, not an error
    assert "already" in r["message"].lower()
    recovery = _tasks_by_title(user["id"], "Recovery workouts")
    assert len(recovery) == 1


def test_single_create_event_duplicate_is_recognized_outside_plan_day(alice, mock_ai_interp):
    tc, _ = alice
    d = _now() + dt.timedelta(days=1)
    _mk_event(tc, "Gym", d.replace(hour=20), d.replace(hour=21))

    mock_ai_interp("single_operation", operations=[
        _op("CREATE_EVENT", title="Gym", start=d.replace(hour=20).isoformat(), end=None, all_day=False),
    ])
    r = mo_propose(tc, "add gym tomorrow at 8pm", "tok-event-dup-single").json()
    assert r["kind"] == "answer"
    assert len(_events_by_title(tc, "Gym")) == 1


# --------------------------------------------------------------------------- #
# SMARTNESS + EXPERIENCE CLOSURE — Bug C: complete multi-item accounting
# --------------------------------------------------------------------------- #
def test_all_four_already_exist_no_proposal_concise_message(alice, mock_ai_interp):
    tc, _ = alice
    d = _now() + dt.timedelta(days=1)
    _mk_event(tc, "Meeting", d.replace(hour=10), d.replace(hour=11))
    _mk_event(tc, "Lunch", d.replace(hour=14), d.replace(hour=14, minute=30))
    _mk_event(tc, "Car oil change", d.replace(hour=18), d.replace(hour=19))
    _mk_event(tc, "Gym", d.replace(hour=20), d.replace(hour=21))

    mock_ai_interp("multi_operation", operations=[
        _op("CREATE_EVENT", title="Meeting", start=d.replace(hour=10).isoformat(), end=None, all_day=False),
        _op("CREATE_EVENT", title="Lunch", start=d.replace(hour=14).isoformat(), end=None, all_day=False),
        _op("CREATE_EVENT", title="Car oil change", start=d.replace(hour=18).isoformat(), end=None, all_day=False),
        _op("CREATE_EVENT", title="Gym", start=d.replace(hour=20).isoformat(), end=None, all_day=False),
    ])
    r = mo_propose(
        tc, "add meeting at 10, lunch at 2, oil change at 6 and gym at 8 for Monday", "tok-all-exist"
    ).json()
    assert r["kind"] == "answer"                                  # nothing to confirm
    for title in ("Meeting", "Lunch", "Car oil change", "Gym"):
        assert title in r["message"]                              # every item named, none silently dropped
    assert len(tc.get("/api/schedule").json()["all_events"]) == 4  # no new rows created


def test_conflict_plus_duplicate_plus_new_all_accounted(alice, mock_ai_interp):
    tc, _ = alice
    d = _now() + dt.timedelta(days=1)
    _mk_event(tc, "Existing meeting", d.replace(hour=10), d.replace(hour=11))
    _mk_event(tc, "Gym", d.replace(hour=20), d.replace(hour=21))

    mock_ai_interp("multi_operation", operations=[
        _op("CREATE_EVENT", title="Meeting", start=d.replace(hour=10, minute=30).isoformat(),
            end=None, all_day=False),                              # overlaps Existing meeting -> conflict
        _op("CREATE_EVENT", title="Gym", start=d.replace(hour=20).isoformat(), end=None, all_day=False),  # duplicate
        _op("CREATE_EVENT", title="Lunch", start=d.replace(hour=13).isoformat(), end=None, all_day=False),  # new
    ])
    r = mo_propose(tc, "add meeting at 10:30, gym at 8, lunch at 1", "tok-mixed-three").json()
    assert r["kind"] == "proposal_required"
    titles = [it["title"] for it in r["proposal"]["display"]["items"]]
    assert titles == ["Lunch"]                                     # only the genuinely new item proposed
    note = r["proposal"]["display"].get("note") or ""
    assert "Meeting" in note                                       # conflict named
    assert "Gym" in note                                           # duplicate named
    assert "Meeting" not in r["proposal"]["summary"]
    assert "Gym" not in r["proposal"]["summary"]

    mo_confirm(tc, r["proposal"]["proposal_id"])
    assert len(_events_by_title(tc, "Lunch")) == 1
    assert len(_events_by_title(tc, "Gym")) == 1                   # still exactly one — not duplicated


# --------------------------------------------------------------------------- #
# SMARTNESS + EXPERIENCE CLOSURE — bounded GENERAL_UTILITY
# --------------------------------------------------------------------------- #
def test_general_utility_arithmetic_is_computed_deterministically(alice, mock_ai_interp):
    tc, _ = alice
    # the model's own arithmetic is deliberately WRONG here — the backend must
    # compute the real answer itself, never trust free-form model math.
    mock_ai_interp("read", operations=[
        _op("GENERAL_UTILITY", kind="arithmetic", expression="150 * 0.20", answer_text="99"),
    ])
    r = mo_propose(tc, "what's 20% of 150", "tok-util-arith").json()
    assert r["kind"] == "answer"
    assert r["message"].strip() == "30"


def test_general_utility_time_diff_is_computed_deterministically(alice, mock_ai_interp):
    tc, _ = alice
    d = _now()
    mock_ai_interp("read", operations=[
        _op("GENERAL_UTILITY", kind="time_diff",
            from_time=d.replace(hour=3, minute=30).isoformat(),
            to_time=d.replace(hour=7, minute=0).isoformat()),
    ])
    r = mo_propose(tc, "how long from 3:30 to 7", "tok-util-timediff").json()
    assert r["kind"] == "answer"
    assert "3 hour" in r["message"] and "30 minute" in r["message"]


def test_general_utility_definition_uses_model_answer_text(alice, mock_ai_interp):
    tc, _ = alice
    mock_ai_interp("read", operations=[
        _op("GENERAL_UTILITY", kind="general",
            answer_text="Putting off tasks despite knowing it'll cost you later."),
    ])
    r = mo_propose(tc, "what does procrastination mean", "tok-util-def").json()
    assert r["kind"] == "answer"
    assert "Putting off" in r["message"]


# --------------------------------------------------------------------------- #
# SMARTNESS + EXPERIENCE CLOSURE — personalized SUGGEST_FREE_TIME judgment
# --------------------------------------------------------------------------- #
def test_suggest_free_time_finds_real_open_block(alice, mock_ai_interp):
    tc, _ = alice
    d = _now() + dt.timedelta(days=1)
    # the morning is fully booked (07:00 waking-hours default -> 13:00) so the
    # EARLIEST sufficient gap is unambiguously the one between Lunch and
    # Football — isolates the free-window search from the "earliest gap wins"
    # tie-break rule.
    _mk_event(tc, "Morning block", d.replace(hour=7), d.replace(hour=13))
    _mk_event(tc, "Lunch", d.replace(hour=13), d.replace(hour=14))
    _mk_event(tc, "Football", d.replace(hour=17), d.replace(hour=18))

    mock_ai_interp("read", operations=[
        _op("SUGGEST_FREE_TIME", target_date=d.date().isoformat(), purpose="study"),
    ])
    r = mo_propose(tc, "when should I study tomorrow", "tok-free-time").json()
    assert r["kind"] == "answer"
    assert "2:00 PM" in r["message"] and "5:00 PM" in r["message"]  # the real open block
    assert "Football" in r["message"]                                # names the next real commitment


def test_suggest_free_time_fully_booked_says_so(alice, mock_ai_interp):
    tc, _ = alice
    d = _now() + dt.timedelta(days=1)
    _mk_event(tc, "All day block", d.replace(hour=0, minute=0), d.replace(hour=23, minute=59))

    mock_ai_interp("read", operations=[
        _op("SUGGEST_FREE_TIME", target_date=d.date().isoformat(), purpose="study"),
    ])
    r = mo_propose(tc, "when should I study tomorrow", "tok-free-time-full").json()
    assert r["kind"] == "answer"
    assert "booked" in r["message"].lower()


# --------------------------------------------------------------------------- #
# SMARTNESS + EXPERIENCE CLOSURE — entity continuity ("it") across 3 turns
# --------------------------------------------------------------------------- #
def test_entity_context_resolves_bare_pronoun_across_three_turns(alice, mock_ai_interp):
    tc, _ = alice
    monday = _now() + dt.timedelta(days=1)
    thursday = _now() + dt.timedelta(days=4)
    ev_mon = _mk_event(tc, "Gym", monday.replace(hour=20), monday.replace(hour=21))
    ev_thu = _mk_event(tc, "Gym", thursday.replace(hour=20), thursday.replace(hour=21))

    # Turn 1 — pure discussion, seeds context_date only.
    mock_ai_interp("read", operations=[_op("GET_SCHEDULE_SUMMARY", target_date=monday.date().isoformat())])
    mo_propose(tc, "let's look at Monday", "tok-entity-t1")

    # Turn 2 — names Gym explicitly; resolves via context_date, seeds entity=Gym.
    mock_ai_interp("single_operation", operations=[
        _op("UPDATE_EVENT", target_description="Gym", new_start=monday.replace(hour=19).isoformat()),
    ])
    r2 = mo_propose(tc, "move gym earlier", "tok-entity-t2").json()
    assert r2["kind"] == "proposal_required"
    mo_confirm(tc, r2["proposal"]["proposal_id"])
    assert _db_event(ev_mon["id"])["start_time"] == monday.replace(hour=19).isoformat()

    # Turn 3 — bare pronoun, NO target_description at all in the model output;
    # only entity context ("Gym") lets the model even name a target — the
    # backend then still deterministically re-resolves it against real data.
    mock_ai_interp("single_operation", operations=[
        _op("UPDATE_EVENT", target_description="Gym", new_start=monday.replace(hour=18, minute=30).isoformat()),
    ])
    r3 = mo_propose(tc, "actually make it 6:30", "tok-entity-t3").json()
    assert r3["kind"] == "proposal_required"
    mo_confirm(tc, r3["proposal"]["proposal_id"])

    assert _db_event(ev_mon["id"])["start_time"] == monday.replace(hour=18, minute=30).isoformat()
    assert _db_event(ev_thu["id"])["start_time"] == thursday.replace(hour=20).isoformat()  # untouched
