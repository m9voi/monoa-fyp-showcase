"""MO INTERACTION + ACTION UX CLOSURE.

Temp DB via conftest; the AI boundary is `mock_ai_interp` (no real model
call) — same pattern as tests/test_mo_multi_operation.py.

Rules under test:
  - a multi-operation batch is validated against the PROPOSED post-change
    schedule, not only today's real one: an item moved or deleted elsewhere
    in the SAME batch actually frees its old slot for a sibling CREATE_EVENT
    (Section 2/3), and a failed move/delete leaves the real schedule as the
    ground truth for everything else in the batch
  - the excluded/not-found portion of a mixed batch is named in a SEPARATE
    secondary note, never mixed into the primary conversational summary
    (Section 4/5)
  - a typed "yes"/"no" is honored ONLY as a fallback for the caller's own
    single active pending proposal, and never mutates anything otherwise
    (Section 9/10)
"""
import datetime as dt

import db as db_layer
from conftest import mo_propose, mo_confirm, mo_cancel, mo_pending

MYT = dt.timezone(dt.timedelta(hours=8))


def _now():
    return dt.datetime.now(MYT).replace(minute=0, second=0, microsecond=0)


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


def _op(intent, **params):
    return {"intent": intent, "parameters": params}


# --------------------------------------------------------------------------- #
# Section 2/3 — batch post-change ("shadow") reasoning
# --------------------------------------------------------------------------- #
def test_batch_move_away_frees_the_slot_for_a_sibling_create_same_batch(alice, mock_ai_interp):
    """The real-device bug: existing Gym 8-9 PM; the SAME request moves Gym to
    6:30 and adds Dinner at 8 PM. Dinner must NOT be rejected against the OLD
    Gym slot the same batch is vacating."""
    tc, _ = alice
    d = _now() + dt.timedelta(days=1)
    gym = _mk_event(tc, "Gym", d.replace(hour=20), d.replace(hour=21))

    mock_ai_interp("multi_operation", operations=[
        _op("UPDATE_EVENT", target_description="Gym", new_start=d.replace(hour=18, minute=30).isoformat()),
        _op("CREATE_EVENT", title="Dinner", start=d.replace(hour=20).isoformat(), end=None, all_day=False),
    ])
    r = mo_propose(tc, "move gym to 6:30 and add dinner at 8", "tok-shadow-move-create").json()
    assert r["kind"] == "proposal_required"
    items = r["proposal"]["display"]["items"]
    assert [it["title"] for it in items] == ["Gym", "Dinner"]
    assert [it["op"] for it in items] == ["change", "add"]        # Dinner NOT excluded as a conflict

    c1 = mo_confirm(tc, r["proposal"]["proposal_id"]).json()
    assert c1["kind"] == "executed"
    assert _db_event(gym["id"])["start_time"] == d.replace(hour=18, minute=30).isoformat()
    assert len(_events_by_title(tc, "Dinner")) == 1


def test_failed_move_leaves_real_schedule_as_ground_truth_conflict_remains(alice, mock_ai_interp):
    """If the move itself can't be resolved (target not found), the batch must
    NOT pretend the old slot was freed — a sibling create at that time still
    conflicts with the untouched real event. Nothing in this batch resolves,
    so there is no proposal at all — a plain, honest message instead, naming
    both problems, never a silent partial success."""
    tc, _ = alice
    d = _now() + dt.timedelta(days=1)
    _mk_event(tc, "Gym", d.replace(hour=20), d.replace(hour=21))

    mock_ai_interp("multi_operation", operations=[
        _op("UPDATE_EVENT", target_description="Nonexistent Thing", new_start=d.replace(hour=18, minute=30).isoformat()),
        _op("CREATE_EVENT", title="Dinner", start=d.replace(hour=20).isoformat(), end=None, all_day=False),
    ])
    r = mo_propose(tc, "move the nonexistent thing to 6:30 and add dinner at 8", "tok-shadow-failed-move").json()
    assert r["kind"] == "error"                                   # nothing resolved -> no proposal
    assert "Gym" in r["message"]                                  # Dinner still conflicts with the REAL Gym
    assert _events_by_title(tc, "Dinner") == []


def test_delete_then_create_uses_the_freed_time_in_the_same_batch(alice, mock_ai_interp):
    """DELETE_EVENT + CREATE_EVENT at the SAME slot, one request: the create
    must see the proposed post-delete state, not the still-occupied real one."""
    tc, _ = alice
    d = _now() + dt.timedelta(days=1)
    dentist = _mk_event(tc, "Dentist appointment", d.replace(hour=20), d.replace(hour=21))

    mock_ai_interp("multi_operation", operations=[
        _op("DELETE_EVENT", target_description="Dentist"),
        _op("CREATE_EVENT", title="Dinner", start=d.replace(hour=20).isoformat(), end=None, all_day=False),
    ])
    r = mo_propose(tc, "delete the dentist appointment and add dinner at 8", "tok-shadow-delete-create").json()
    assert r["kind"] == "proposal_required"
    items = r["proposal"]["display"]["items"]
    assert [it["title"] for it in items] == ["Dentist appointment", "Dinner"]
    assert [it["op"] for it in items] == ["delete", "add"]

    c1 = mo_confirm(tc, r["proposal"]["proposal_id"]).json()
    assert c1["kind"] == "executed"
    assert _db_event(dentist["id"]) is None
    assert len(_events_by_title(tc, "Dinner")) == 1


def test_mixed_move_not_found_add_matches_the_real_device_scenario(alice, mock_ai_interp):
    """The exact real-device request: move Gym away (frees its slot), delete a
    dentist appointment that doesn't exist (named honestly, never silently
    dropped), add Dinner at the now-free slot. The primary summary names only
    what Mo will actually do; the not-found portion is a separate note."""
    tc, _ = alice
    d = _now() + dt.timedelta(days=1)
    gym = _mk_event(tc, "Gym", d.replace(hour=20), d.replace(hour=21))

    mock_ai_interp("multi_operation", operations=[
        _op("UPDATE_EVENT", target_description="Gym", new_start=d.replace(hour=18, minute=30).isoformat()),
        _op("DELETE_EVENT", target_description="dentist thing"),
        _op("CREATE_EVENT", title="Dinner", start=d.replace(hour=20).isoformat(), end=None, all_day=False),
    ])
    r = mo_propose(
        tc,
        "sort Monday out for me - push the gym to 6:30, get rid of the dentist thing if it's there, "
        "and put dinner at 8",
        "tok-shadow-real-device",
    ).json()
    assert r["kind"] == "proposal_required"
    items = r["proposal"]["display"]["items"]
    assert [it["title"] for it in items] == ["Gym", "Dinner"]       # only the executable half
    note = r["proposal"]["display"].get("note") or ""
    assert "dentist" in note.lower()                                # named, honestly, as a secondary note
    assert "dentist" not in r["proposal"]["summary"].lower()        # never mixed into the primary reply
    assert "change" not in r["proposal"]["summary"].lower()         # no technical/internal language
    assert "operation" not in r["proposal"]["summary"].lower()

    c1 = mo_confirm(tc, r["proposal"]["proposal_id"]).json()
    assert c1["kind"] == "executed"
    assert _db_event(gym["id"])["start_time"] == d.replace(hour=18, minute=30).isoformat()
    assert len(_events_by_title(tc, "Dinner")) == 1


# --------------------------------------------------------------------------- #
# Section 9/10 — confirmation vs clarification: typed yes/no fallback
# --------------------------------------------------------------------------- #
def test_typed_yes_confirms_the_one_active_pending_proposal(alice, mock_ai_interp):
    tc, _ = alice
    d = _now() + dt.timedelta(days=1)
    mock_ai_interp("single_operation", operations=[
        _op("CREATE_EVENT", title="Gym", start=d.replace(hour=20).isoformat(), end=None, all_day=False),
    ])
    r = mo_propose(tc, "add gym tomorrow at 8pm", "tok-yes-1").json()
    assert r["kind"] == "proposal_required"
    pid = r["proposal"]["proposal_id"]

    yes = mo_propose(tc, "yes", "tok-yes-1-confirm").json()
    assert yes["kind"] == "executed"
    assert yes["proposal"]["proposal_id"] == pid
    assert len(_events_by_title(tc, "Gym")) == 1


def test_typed_no_cancels_the_one_active_pending_proposal(alice, mock_ai_interp):
    tc, _ = alice
    d = _now() + dt.timedelta(days=1)
    mock_ai_interp("single_operation", operations=[
        _op("CREATE_EVENT", title="Gym", start=d.replace(hour=20).isoformat(), end=None, all_day=False),
    ])
    r = mo_propose(tc, "add gym tomorrow at 8pm", "tok-no-1").json()
    pid = r["proposal"]["proposal_id"]

    no = mo_propose(tc, "no", "tok-no-1-cancel").json()
    assert no["kind"] == "cancelled"
    assert no["proposal"]["proposal_id"] == pid
    assert _events_by_title(tc, "Gym") == []

    # Cancel is real — a later Confirm on the same id can never execute it.
    c = mo_confirm(tc, pid)
    assert c.status_code == 409


def test_typed_yes_with_no_pending_proposal_mutates_nothing(alice):
    # Never reaches the model at all -> no mock_ai_interp needed; the yes/no
    # fallback is checked BEFORE any AI call.
    tc, _ = alice
    before = len(tc.get("/api/schedule").json()["all_events"])
    r = mo_propose(tc, "yes", "tok-yes-orphan").json()
    assert r["kind"] == "answer"
    assert "nothing pending" in r["message"].lower()
    after = len(tc.get("/api/schedule").json()["all_events"])
    assert after == before


def test_typed_no_with_no_pending_proposal_mutates_nothing(alice):
    tc, _ = alice
    r = mo_propose(tc, "cancel that", "tok-no-orphan").json()
    assert r["kind"] == "answer"
    assert "nothing pending" in r["message"].lower()


def test_yes_no_words_never_match_as_a_substring_of_an_ordinary_message(alice, mock_ai_interp):
    """'yes'/'no' is an EXACT, closed-vocabulary match — an ordinary sentence
    that happens to start with one of those words must still reach the model,
    never be misread as a confirmation/cancellation of nothing."""
    tc, _ = alice
    mock_ai_interp("read", operations=[_op("GENERAL_UTILITY", kind="arithmetic", expression="1+1")])
    r = mo_propose(tc, "nope, not that — actually tell me a fact", "tok-yes-substring").json()
    # falls through to the (mocked) model, never short-circuited as "no"
    assert r["kind"] != "cancelled"


def test_typed_yes_is_idempotent_with_repeated_taps(alice, mock_ai_interp):
    tc, _ = alice
    d = _now() + dt.timedelta(days=1)
    mock_ai_interp("single_operation", operations=[
        _op("CREATE_EVENT", title="Gym", start=d.replace(hour=20).isoformat(), end=None, all_day=False),
    ])
    r = mo_propose(tc, "add gym tomorrow at 8pm", "tok-yes-idem").json()
    pid = r["proposal"]["proposal_id"]

    c1 = mo_confirm(tc, pid).json()
    assert c1["kind"] == "executed"
    c2 = mo_confirm(tc, pid).json()
    assert c2["kind"] == "executed"
    assert len(_events_by_title(tc, "Gym")) == 1                   # exactly one — never duplicated
