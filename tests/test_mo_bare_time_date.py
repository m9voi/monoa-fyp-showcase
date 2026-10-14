"""PRODUCT CLOSURE BATCH 05 — Bug A: Mo bare-time move on another day.

"Move [event] to 7 PM" with NO explicit date must preserve the RESOLVED
TARGET EVENT'S OWN calendar date, never silently resolve against today. The
model is never given the event's real date (target resolution is
deterministic and server-side, DB-backed) so it is asked to return a bare
"HH:MM" (new_start_time_only) whenever the user names no date, and the
server combines it with the resolved target's real date
(_mo_combine_bare_time_with_date). An explicit new date the user DOES name
still wins outright (new_start, unchanged path).

Also covers the companion duration-preservation fix in
_event_update_columns: a move that changes only the start time (no explicit
new end) must keep the event's OWN existing duration, not silently collapse
to a flat 1 hour.

Temp DB via conftest; the AI boundary is mock_ai_interp — no real model call.
"""
import datetime as dt

import db as db_layer
from conftest import mo_confirm, mo_propose


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


def _op(intent, **params):
    return {"intent": intent, "parameters": params}


def _move_bare_time(tc, mock_ai_interp, title, time_str, token, **extra_params):
    mock_ai_interp("single_operation", operations=[
        _op("UPDATE_EVENT", target_description=title, new_start_time_only=time_str, **extra_params),
    ])
    return mo_propose(tc, f"move {title} to {time_str}", token)


# =========================================================================== #
# Same-day / future-day / tomorrow — the target's OWN date must be preserved
# =========================================================================== #
def test_same_day_event_bare_time_stays_on_that_same_day(alice, mock_ai_interp):
    tc, _ = alice
    d = _now()
    ev = _mk_event(tc, "Design Review", d.replace(hour=15), d.replace(hour=16))

    r = _move_bare_time(tc, mock_ai_interp, "Design Review", "19:00", "tok-bt-sameday")
    assert r.json()["kind"] == "proposal_required", r.text
    mo_confirm(tc, r.json()["proposal"]["proposal_id"])

    row = _db_event(ev["id"])
    assert row["start_time"] == d.replace(hour=19).isoformat()


def test_future_day_event_bare_time_preserves_that_future_date_not_today(alice, mock_ai_interp):
    """The exact reported bug: an event several days away, moved by a bare
    clock time, must NOT silently jump to today."""
    tc, _ = alice
    future = (_now() + dt.timedelta(days=5)).replace(hour=15)
    ev = _mk_event(tc, "Design Review", future, future + dt.timedelta(hours=1))

    r = _move_bare_time(tc, mock_ai_interp, "Design Review", "19:00", "tok-bt-future")
    assert r.json()["kind"] == "proposal_required", r.text
    proposed_when = r.json()["proposal"]["display"]["items"][0]["when"]
    assert proposed_when.endswith("07:00 PM")

    mo_confirm(tc, r.json()["proposal"]["proposal_id"])
    row = _db_event(ev["id"])
    expected = future.replace(hour=19)
    assert row["start_time"] == expected.isoformat()
    assert row["start_time"][:10] == future.date().isoformat()   # still 5 days out
    assert row["start_time"][:10] != _now().date().isoformat()   # NEVER today


def test_tomorrow_event_bare_time_preserves_tomorrow(alice, mock_ai_interp):
    tc, _ = alice
    tomorrow = (_now() + dt.timedelta(days=1)).replace(hour=9)
    ev = _mk_event(tc, "Standup", tomorrow, tomorrow + dt.timedelta(minutes=30))

    r = _move_bare_time(tc, mock_ai_interp, "Standup", "10:30", "tok-bt-tomorrow")
    assert r.json()["kind"] == "proposal_required", r.text
    mo_confirm(tc, r.json()["proposal"]["proposal_id"])

    row = _db_event(ev["id"])
    assert row["start_time"] == tomorrow.replace(hour=10, minute=30).isoformat()


# =========================================================================== #
# Explicit new date always wins
# =========================================================================== #
def test_explicit_new_date_and_time_wins_and_is_used_verbatim(alice, mock_ai_interp):
    tc, _ = alice
    d = _now()
    ev = _mk_event(tc, "Design Review", d.replace(hour=15), d.replace(hour=16))
    tomorrow_7pm = (d + dt.timedelta(days=1)).replace(hour=19)

    mock_ai_interp("single_operation", operations=[
        _op("UPDATE_EVENT", target_description="Design Review", new_start=tomorrow_7pm.isoformat()),
    ])
    r = mo_propose(tc, "move Design Review to tomorrow at 7pm", "tok-bt-explicit-date")
    assert r.json()["kind"] == "proposal_required", r.text
    mo_confirm(tc, r.json()["proposal"]["proposal_id"])

    row = _db_event(ev["id"])
    assert row["start_time"] == tomorrow_7pm.isoformat()


def test_explicit_new_start_takes_precedence_over_a_bare_time_hint_if_both_given(alice, mock_ai_interp):
    """Defensive: if the model (incorrectly) supplies both, the explicit
    resolved new_start must win outright — it represents an explicit date the
    user's own words named."""
    tc, _ = alice
    d = _now()
    ev = _mk_event(tc, "Design Review", d.replace(hour=15), d.replace(hour=16))
    explicit = (d + dt.timedelta(days=2)).replace(hour=11)

    mock_ai_interp("single_operation", operations=[
        _op("UPDATE_EVENT", target_description="Design Review",
            new_start=explicit.isoformat(), new_start_time_only="19:00"),
    ])
    r = mo_propose(tc, "move Design Review", "tok-bt-both-given")
    mo_confirm(tc, r.json()["proposal"]["proposal_id"])
    assert _db_event(ev["id"])["start_time"] == explicit.isoformat()


# =========================================================================== #
# Midnight / noon edge cases
# =========================================================================== #
def test_bare_time_midnight_and_noon_land_on_the_correct_preserved_date(alice, mock_ai_interp):
    tc, _ = alice
    future = (_now() + dt.timedelta(days=3)).replace(hour=15)
    ev = _mk_event(tc, "Design Review", future, future + dt.timedelta(hours=1))

    r = _move_bare_time(tc, mock_ai_interp, "Design Review", "00:00", "tok-bt-midnight")
    mo_confirm(tc, r.json()["proposal"]["proposal_id"])
    row = _db_event(ev["id"])
    assert row["start_time"] == future.replace(hour=0, minute=0).isoformat()
    assert row["start_time"][:10] == future.date().isoformat()

    ev2 = _mk_event(tc, "Lunch Sync", future + dt.timedelta(hours=4), future + dt.timedelta(hours=5))
    r2 = _move_bare_time(tc, mock_ai_interp, "Lunch Sync", "12:00", "tok-bt-noon")
    mo_confirm(tc, r2.json()["proposal"]["proposal_id"])
    row2 = _db_event(ev2["id"])
    assert row2["start_time"] == future.replace(hour=12, minute=0).isoformat()


# =========================================================================== #
# Duration preservation
# =========================================================================== #
def test_bare_time_move_preserves_a_non_default_duration(alice, mock_ai_interp):
    """A 2.5-hour event moved by bare time must stay 2.5 hours long — not
    collapse to the create-time default of 1 hour."""
    tc, _ = alice
    d = _now()
    ev = _mk_event(tc, "Workshop", d.replace(hour=9), d.replace(hour=11, minute=30))

    r = _move_bare_time(tc, mock_ai_interp, "Workshop", "14:00", "tok-bt-duration")
    mo_confirm(tc, r.json()["proposal"]["proposal_id"])

    row = _db_event(ev["id"])
    assert row["start_time"] == d.replace(hour=14).isoformat()
    assert row["end_time"] == d.replace(hour=16, minute=30).isoformat()   # same 2.5h duration


def test_event_with_explicit_new_end_overrides_duration_preservation(alice, mock_ai_interp):
    """When the user (via the model) DOES supply an explicit new end, that
    explicit value wins — duration preservation only fills a gap, it never
    overrides something actually supplied."""
    tc, _ = alice
    d = _now()
    ev = _mk_event(tc, "Workshop", d.replace(hour=9), d.replace(hour=11, minute=30))

    mock_ai_interp("single_operation", operations=[
        _op("UPDATE_EVENT", target_description="Workshop",
            new_start_time_only="14:00", new_end=d.replace(hour=15).isoformat()),
    ])
    r = mo_propose(tc, "move Workshop to 2pm, ending at 3pm", "tok-bt-explicit-end")
    mo_confirm(tc, r.json()["proposal"]["proposal_id"])

    row = _db_event(ev["id"])
    assert row["start_time"] == d.replace(hour=14).isoformat()
    assert row["end_time"] == d.replace(hour=15).isoformat()   # explicit end honored, not 11:30 + delta


def test_manual_patch_start_only_also_preserves_original_duration(alice):
    """The fix lives in the SAME shared _event_update_columns the manual
    PATCH endpoint uses — proving the manual edit path gets it too."""
    tc, _ = alice
    d = _now()
    ev = _mk_event(tc, "Workshop", d.replace(hour=9), d.replace(hour=11, minute=30))

    r = tc.patch(f"/api/events/{ev['id']}", json={"start": d.replace(hour=14).isoformat()})
    assert r.status_code == 200, r.text
    row = _db_event(ev["id"])
    assert row["start_time"] == d.replace(hour=14).isoformat()
    assert row["end_time"] == d.replace(hour=16, minute=30).isoformat()


# =========================================================================== #
# Safety rules unaffected: ambiguity, isolation, malformed input
# =========================================================================== #
def test_ambiguous_event_resolution_still_requires_clarification(alice, mock_ai_interp):
    tc, _ = alice
    d = _now()
    _mk_event(tc, "Review", d.replace(hour=10), d.replace(hour=11))
    _mk_event(tc, "Review", (d + dt.timedelta(days=2)).replace(hour=10), (d + dt.timedelta(days=2)).replace(hour=11))

    r = _move_bare_time(tc, mock_ai_interp, "Review", "19:00", "tok-bt-ambiguous")
    assert r.json()["kind"] == "clarification_required", r.text


def test_bare_time_move_is_isolated_per_user(alice, bob, mock_ai_interp):
    tc_a, _ = alice
    tc_b, _ = bob
    d = _now()
    ev_a = _mk_event(tc_a, "Design Review", d.replace(hour=15), d.replace(hour=16))
    ev_b = _mk_event(tc_b, "Design Review", d.replace(hour=15), d.replace(hour=16))

    r = _move_bare_time(tc_a, mock_ai_interp, "Design Review", "19:00", "tok-bt-iso")
    mo_confirm(tc_a, r.json()["proposal"]["proposal_id"])

    assert _db_event(ev_a["id"])["start_time"] == d.replace(hour=19).isoformat()
    assert _db_event(ev_b["id"])["start_time"] == d.replace(hour=15).isoformat()   # untouched


def test_unparseable_bare_time_returns_a_safe_error_never_a_guess(alice, mock_ai_interp):
    tc, _ = alice
    d = _now()
    ev = _mk_event(tc, "Design Review", d.replace(hour=15), d.replace(hour=16))

    r = _move_bare_time(tc, mock_ai_interp, "Design Review", "sometime evening", "tok-bt-badtime")
    assert r.json()["kind"] == "error", r.text
    assert _db_event(ev["id"])["start_time"] == d.replace(hour=15).isoformat()   # untouched


# =========================================================================== #
# Confirmation/execution fidelity — proposed value persists EXACTLY
# =========================================================================== #
def test_confirmation_persists_the_exact_proposed_bare_time_datetime(alice, mock_ai_interp):
    tc, _ = alice
    future = (_now() + dt.timedelta(days=4)).replace(hour=15)
    ev = _mk_event(tc, "Design Review", future, future + dt.timedelta(hours=1))

    r = _move_bare_time(tc, mock_ai_interp, "Design Review", "19:00", "tok-bt-fidelity")
    proposed_when = r.json()["proposal"]["display"]["items"][0]["when"]

    c = mo_confirm(tc, r.json()["proposal"]["proposal_id"])
    assert c.json()["kind"] == "executed"
    row = _db_event(ev["id"])
    expected = future.replace(hour=19)
    assert row["start_time"] == expected.isoformat()
    assert proposed_when.endswith("07:00 PM")


# =========================================================================== #
# Conflict detection uses the PRESERVED date, not "today"
# =========================================================================== #
def test_bare_time_move_still_detects_a_real_conflict_on_the_preserved_future_date(alice, mock_ai_interp):
    tc, _ = alice
    future = (_now() + dt.timedelta(days=6)).replace(hour=15)
    ev = _mk_event(tc, "Design Review", future, future + dt.timedelta(hours=1))
    # a real conflicting event on the SAME future date, at the bare time being moved to
    _mk_event(tc, "Client Call", future.replace(hour=18, minute=30), future.replace(hour=19, minute=30))

    r = _move_bare_time(tc, mock_ai_interp, "Design Review", "19:00", "tok-bt-conflict")
    assert r.json()["kind"] == "error"
    assert "Client Call" in r.json()["message"]
    assert _db_event(ev["id"])["start_time"] == future.isoformat()   # untouched


def test_bare_time_move_is_not_falsely_blocked_by_a_same_clock_time_on_a_different_day(alice, mock_ai_interp):
    """Negative control proving the fix genuinely checks the PRESERVED date:
    an unrelated event at the identical clock time on TODAY must not block a
    move that lands on a different (future) day."""
    tc, _ = alice
    now = _now()
    future = (now + dt.timedelta(days=6)).replace(hour=15)
    ev = _mk_event(tc, "Design Review", future, future + dt.timedelta(hours=1))
    # same 19:00 slot, but on TODAY — irrelevant to the future-day move
    _mk_event(tc, "Unrelated Today Event", now.replace(hour=19), now.replace(hour=20))

    r = _move_bare_time(tc, mock_ai_interp, "Design Review", "19:00", "tok-bt-no-false-conflict")
    assert r.json()["kind"] == "proposal_required", r.text
    mo_confirm(tc, r.json()["proposal"]["proposal_id"])
    assert _db_event(ev["id"])["start_time"] == future.replace(hour=19).isoformat()
