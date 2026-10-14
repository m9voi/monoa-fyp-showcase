"""Phase 4 — Planning & Schedule engine tests. Temp DB, AI mocked, no paid calls."""
import datetime
import sqlite3

import pytest

import main
import db as db_layer
import schedule_core as sched
from conftest import new_authed_client

MYT = datetime.timezone(datetime.timedelta(hours=8))


def _now():
    return datetime.datetime.now(MYT).replace(microsecond=0)


def _iso(dt):
    return dt.isoformat()


def _mk_event(tc, title, start, end=None, **kw):
    body = {"title": title, "start": _iso(start)}
    if end is not None:
        body["end"] = _iso(end)
    body.update(kw)
    return tc.post("/api/events", json=body)


# ===================== schedule_core unit =====================
@pytest.mark.parametrize("a0,a1,b0,b1,expect", [
    ("2026-09-04T10:00:00+08:00", "2026-09-04T11:00:00+08:00",
     "2026-09-04T11:00:00+08:00", "2026-09-04T12:00:00+08:00", False),   # touching
    ("2026-09-04T10:00:00+08:00", "2026-09-04T11:30:00+08:00",
     "2026-09-04T11:00:00+08:00", "2026-09-04T12:00:00+08:00", True),    # overlap
    ("2026-09-04T10:00:00+08:00", "2026-09-04T11:00:00+08:00",
     "2026-09-04T10:00:00+08:00", "2026-09-04T11:00:00+08:00", True),    # identical
    ("2026-09-04T10:00:00+08:00", "2026-09-04T11:00:00+08:00",
     "2026-09-04T09:00:00+08:00", "2026-09-04T09:30:00+08:00", False),   # disjoint
])
def test_interval_overlap(a0, a1, b0, b1, expect):
    assert sched.intervals_overlap(a0, a1, b0, b1) is expect


def test_window_clamp_safety():
    a, b = sched.clamp_window("2026-01-01T00:00:00+08:00", "2027-01-01T00:00:00+08:00")
    assert (b - a).days == sched.MAX_WINDOW_DAYS


def test_recurrence_weekly_bounded():
    row = {"id": "r", "title": "Class", "start_time": "2026-09-07T09:00:00+08:00",
           "end_time": "2026-09-07T10:00:00+08:00", "recurrence": "weekly",
           "recurrence_until": "2026-09-30T00:00:00+08:00"}
    ws, we = sched.clamp_window("2026-09-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00")
    dates = [o["occurrence_date"] for o in sched.expand_event(row, ws, we)]
    assert dates == ["2026-09-07", "2026-09-14", "2026-09-21", "2026-09-28"]


def test_recurrence_weekdays():
    row = {"id": "r", "title": "Standup", "start_time": "2026-09-07T09:00:00+08:00",
           "end_time": "2026-09-07T09:15:00+08:00", "recurrence": "weekdays"}
    ws, we = sched.clamp_window("2026-09-07T00:00:00+08:00", "2026-09-14T00:00:00+08:00")
    assert [o["occurrence_date"] for o in sched.expand_event(row, ws, we)] == \
        ["2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"]


# ===================== auth / IDOR =====================
def test_schedule_endpoints_require_auth(client):
    for m, p in [("get", "/api/schedule"), ("get", "/api/schedule/day?date=2026-09-04"),
                 ("get", "/api/schedule/month?year=2026&month=9")]:
        assert getattr(client, m)(p).status_code == 401
    assert client.post("/api/events", json={"title": "x", "start": "2026-09-04T10:00:00+08:00"}).status_code == 401
    assert client.post("/api/tasks", json={"title": "x"}).status_code == 401


def test_event_isolation(alice, bob):
    ac, _ = alice
    bc, _ = bob
    ev = _mk_event(bc, "Bob private", _now().replace(hour=10), _now().replace(hour=11)).json()["event"]["id"]
    assert all(e["title"] != "Bob private" for e in ac.get("/api/schedule").json()["events"])
    assert ac.patch(f"/api/events/{ev}", json={"title": "hijack"}).status_code == 404
    assert ac.delete(f"/api/events/{ev}").status_code == 404
    assert bc.get("/api/schedule").json()["events"][0]["title"] == "Bob private"


def test_task_isolation(alice, bob):
    ac, _ = alice
    bc, _ = bob
    tk = bc.post("/api/tasks", json={"title": "Bob task"}).json()["task"]["id"]
    assert all(t["title"] != "Bob task" for t in ac.get("/api/schedule").json()["tasks"])
    assert ac.patch(f"/api/tasks/{tk}", json={"title": "x"}).status_code == 404
    assert ac.post(f"/api/tasks/{tk}/complete").status_code == 404
    assert ac.delete(f"/api/tasks/{tk}").status_code == 404


# ===================== event CRUD =====================
def test_event_create_and_defaults(alice):
    tc, _ = alice
    r = _mk_event(tc, "  Lecture  ", _now().replace(hour=9, minute=0))
    assert r.status_code == 201
    ev = r.json()["event"]
    assert ev["title"] == "Lecture" and ev["status"] == "confirmed" and ev["source_type"] == "coreos"
    # no end supplied -> defaulted to start + 1h
    assert datetime.datetime.fromisoformat(ev["end_time"]) - datetime.datetime.fromisoformat(ev["start_time"]) \
        == datetime.timedelta(hours=1)


@pytest.mark.parametrize("body,code", [
    ({"title": "  ", "start": "2026-09-04T10:00:00+08:00"}, 422),
    ({"title": "x", "start": "not-a-date"}, 422),
    ({"title": "x", "start": "2026-09-04T12:00:00+08:00", "end": "2026-09-04T11:00:00+08:00"}, 422),
    ({"title": "x", "start": "2026-09-04T10:00:00+08:00", "recurrence": "hourly"}, 422),
    ({"title": "x" * 250, "start": "2026-09-04T10:00:00+08:00"}, 422),
])
def test_event_create_validation(alice, body, code):
    tc, _ = alice
    assert tc.post("/api/events", json=body).status_code == code


def test_event_edit_and_persist(alice):
    tc, _ = alice
    ev = _mk_event(tc, "Meeting", _now().replace(hour=10), _now().replace(hour=11)).json()["event"]["id"]
    new_start = _now().replace(hour=14, minute=30)
    r = tc.patch(f"/api/events/{ev}", json={"title": "Meeting v2", "start": _iso(new_start),
                                            "end": _iso(new_start + datetime.timedelta(hours=1)),
                                            "location": "Room 5"})
    assert r.status_code == 200 and r.json()["event"]["title"] == "Meeting v2"
    # refetch
    got = next(e for e in tc.get("/api/schedule").json()["all_events"] if e["id"] == ev)
    assert got["title"] == "Meeting v2" and got["location"] == "Room 5"


def test_event_edit_nonexistent_and_empty(alice):
    tc, _ = alice
    assert tc.patch("/api/events/nope", json={"title": "z"}).status_code == 404
    ev = _mk_event(tc, "E", _now()).json()["event"]["id"]
    assert tc.patch(f"/api/events/{ev}", json={}).status_code == 422


def test_event_delete(alice):
    tc, _ = alice
    ev = _mk_event(tc, "Del", _now()).json()["event"]["id"]
    assert tc.delete(f"/api/events/{ev}").status_code == 200
    assert all(e["id"] != ev for e in tc.get("/api/schedule").json()["all_events"])
    assert tc.delete(f"/api/events/{ev}").status_code == 404


def test_related_task_link(alice):
    tc, _ = alice
    tk = tc.post("/api/tasks", json={"title": "Prep"}).json()["task"]["id"]
    r = _mk_event(tc, "FYP", _now() + datetime.timedelta(days=1), related_task_id=tk)
    assert r.status_code == 201 and r.json()["event"]["related_task_id"] == tk
    assert _mk_event(tc, "x", _now(), related_task_id="ghost").status_code == 404


# ===================== task CRUD + completion =====================
def test_task_lifecycle(alice):
    tc, _ = alice
    r = tc.post("/api/tasks", json={"title": "Slides", "notes": "20 pages",
                                    "due_date": _iso(_now() + datetime.timedelta(days=2))})
    tk = r.json()["task"]["id"]
    assert r.status_code == 201 and r.json()["task"]["is_completed"] is False

    r = tc.post(f"/api/tasks/{tk}/complete")
    assert r.status_code == 200 and r.json()["task"]["is_completed"] is True and r.json()["task"]["completed_at"]

    r = tc.post(f"/api/tasks/{tk}/uncomplete")
    assert r.status_code == 200 and r.json()["task"]["is_completed"] is False and r.json()["task"]["completed_at"] is None

    r = tc.patch(f"/api/tasks/{tk}", json={"title": "Slides v2",
                                           "reminder_at": _iso(_now() + datetime.timedelta(hours=5))})
    assert r.status_code == 200 and r.json()["task"]["reminder_status"] == "PENDING"

    assert tc.delete(f"/api/tasks/{tk}").status_code == 200
    assert tc.delete(f"/api/tasks/{tk}").status_code == 404
    assert tc.post("/api/tasks/nope/complete").status_code == 404


def test_task_create_validation(alice):
    tc, _ = alice
    assert tc.post("/api/tasks", json={"title": "  "}).status_code == 422
    assert tc.post("/api/tasks", json={"title": "x", "due_date": "garbage"}).status_code == 422


# ===================== conflict engine (API) =====================
def test_conflicts_deterministic(alice):
    tc, _ = alice
    d = _now().replace(hour=16, minute=0)
    a = _mk_event(tc, "A", d, d + datetime.timedelta(minutes=90)).json()["event"]["id"]           # 16:00-17:30
    b = _mk_event(tc, "B", d.replace(hour=17), d.replace(hour=18)).json()["event"]["id"]          # 17:00-18:00
    _mk_event(tc, "C", d.replace(hour=18), d.replace(hour=19))                                    # 18:00-19:00 boundary
    day = tc.get(f"/api/schedule/day?date={_now().date().isoformat()}").json()
    ids = {frozenset(c["event_ids"]) for c in day["conflicts"]}
    assert ids == {frozenset({a, b})}


def test_cancelled_event_not_in_conflict(alice):
    tc, _ = alice
    d = _now().replace(hour=9, minute=0)
    _mk_event(tc, "Live", d, d + datetime.timedelta(hours=2))
    c = _mk_event(tc, "Cancelled", d.replace(hour=10), d.replace(hour=11)).json()["event"]["id"]
    tc.patch(f"/api/events/{c}", json={"status": "cancelled"})
    assert tc.get(f"/api/schedule/day?date={_now().date().isoformat()}").json()["conflicts"] == []


def test_conflict_detection_uses_expanded_recurring_occurrences(alice):
    """A one-time event that overlaps ONE occurrence of a weekly series must be
    reported as a real conflict against that *expanded* occurrence — the stored
    recurring row's own start_time is on a different date, so this only works if
    conflict detection runs after recurrence expansion.

    Also asserts: an event touching the occurrence's end (12:00) is NOT a
    conflict; a different occurrence of the same series is not flagged; the
    conflict payload carries occurrence-level ids/times the UI needs; and
    recurrence expansion does not multiply the conflict.
    """
    tc, _ = alice
    # "University" — weekly Monday 10:00-12:00, series starts 2026-09-07, ~1 month.
    uni = _mk_event(
        tc, "University",
        datetime.datetime(2026, 9, 7, 10, 0, tzinfo=MYT),
        datetime.datetime(2026, 9, 7, 12, 0, tzinfo=MYT),
        recurrence="weekly", recurrence_until="2026-10-05",
    ).json()["event"]["id"]
    # "Meeting" — one-time, on the THIRD Monday occurrence (2026-09-21), 11:00-11:30.
    meet = _mk_event(
        tc, "Meeting",
        datetime.datetime(2026, 9, 21, 11, 0, tzinfo=MYT),
        datetime.datetime(2026, 9, 21, 11, 30, tzinfo=MYT),
    ).json()["event"]["id"]
    # "Boundary" — one-time, starts exactly when the University occurrence ends.
    _mk_event(
        tc, "Boundary",
        datetime.datetime(2026, 9, 21, 12, 0, tzinfo=MYT),
        datetime.datetime(2026, 9, 21, 13, 0, tzinfo=MYT),
    )

    # --- day query on the overlapping Monday: exactly one real conflict ---
    day = tc.get("/api/schedule/day?date=2026-09-21").json()
    pairs = [frozenset(c["event_ids"]) for c in day["conflicts"]]
    assert pairs == [frozenset({uni, meet})]
    assert len(pairs) == len(set(pairs))                     # no duplicate rows

    c = day["conflicts"][0]
    by_id = {c["a"]["id"]: c["a"], c["b"]["id"]: c["b"]}
    assert set(by_id) == {uni, meet}                          # ids identify both sides
    assert by_id[uni]["is_recurring_instance"] is True
    assert by_id[uni]["occurrence_date"] == "2026-09-21"      # the expanded date, not 09-07
    assert by_id[uni]["start"] == "2026-09-21T10:00:00+08:00"
    assert by_id[uni]["end"] == "2026-09-21T12:00:00+08:00"
    assert by_id[uni]["series_start"] == "2026-09-07T10:00:00+08:00"
    assert by_id[meet]["occurrence_date"] == "2026-09-21"
    # the 12:00 boundary event is never part of a conflict (half-open intervals)
    assert all("Boundary" not in (x["a"]["title"], x["b"]["title"]) for x in day["conflicts"])

    # --- a different occurrence of the same series is clean ---
    other = tc.get("/api/schedule/day?date=2026-09-14").json()
    assert other["conflicts"] == []
    assert any(e["id"] == uni and e["occurrence_date"] == "2026-09-14" for e in other["events"])

    # --- wide multi-week window: expansion must not multiply the conflict ---
    wide = tc.get("/api/schedule?date_from=2026-09-01&date_to=2026-09-30").json()
    assert len([e for e in wide["events"] if e["id"] == uni]) >= 4   # 09-07/14/21/28 expanded
    assert [frozenset(x["event_ids"]) for x in wide["conflicts"]] == [frozenset({uni, meet})]


# ===================== week-window calendar-day boundaries =====================
def test_resolve_window_params_semantics():
    # date-only params are MYT calendar dates; date_to is an INCLUSIVE end date
    # whose half-open backend range ends at the next day's 00:00 MYT.
    f, t = sched.resolve_window_params("2026-09-07", "2026-09-13")
    assert f == "2026-09-07T00:00:00+08:00"
    assert t == "2026-09-14T00:00:00+08:00"
    # full datetime params pass through (normalized to +08:00), not treated as dates
    f, t = sched.resolve_window_params("2026-09-07T09:30:00", "2026-09-10T18:00:00")
    assert f == "2026-09-07T09:30:00+08:00" and t == "2026-09-10T18:00:00+08:00"
    for bad in [("2026-09-40", "2026-09-41"), ("2026-09-13", "2026-09-07"), ("nonsense", "2026-09-07")]:
        with pytest.raises(ValueError):
            sched.resolve_window_params(*bad)


def test_week_window_uses_full_calendar_days(alice):
    """GET /api/schedule?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD must cover the
    whole Monday..Sunday span at true MYT midnight boundaries — no midday clip."""
    tc, _ = alice

    def mk(title, y, mo, d, h1, m1, h2, m2):
        return _mk_event(tc, title,
                         datetime.datetime(y, mo, d, h1, m1, tzinfo=MYT),
                         datetime.datetime(y, mo, d, h2, m2, tzinfo=MYT))

    mk("Mon early", 2026, 9, 7, 7, 0, 8, 0)        # inside, was clipped by old midday start
    mk("Mon late", 2026, 9, 7, 15, 0, 16, 0)       # inside
    mk("Sun evening", 2026, 9, 13, 21, 0, 22, 0)   # inside, was clipped by old midday end
    mk("Prev Sunday", 2026, 9, 6, 23, 0, 23, 30)   # outside (day before)
    mk("Next Monday", 2026, 9, 14, 0, 30, 1, 0)    # outside (day after)

    r = tc.get("/api/schedule?date_from=2026-09-07&date_to=2026-09-13")
    assert r.status_code == 200
    body = r.json()
    assert body["range"] == {"from": "2026-09-07T00:00:00+08:00",
                             "to": "2026-09-14T00:00:00+08:00"}
    titles = {e["title"] for e in body["events"]}
    assert {"Mon early", "Mon late", "Sun evening"} <= titles
    assert "Prev Sunday" not in titles and "Next Monday" not in titles


def test_week_window_expands_recurrence_and_conflicts_across_full_week(alice):
    """Recurrence expansion and conflict detection must run over the corrected
    full-week window (regression: a Monday-morning occurrence used to be clipped
    away before conflict detection saw it)."""
    tc, _ = alice
    uni = _mk_event(tc, "Weekly AM",
                    datetime.datetime(2026, 9, 7, 7, 0, tzinfo=MYT),
                    datetime.datetime(2026, 9, 7, 9, 0, tzinfo=MYT),
                    recurrence="weekly", recurrence_until="2026-10-05").json()["event"]["id"]
    clash = _mk_event(tc, "Early clash",
                      datetime.datetime(2026, 9, 7, 8, 0, tzinfo=MYT),
                      datetime.datetime(2026, 9, 7, 8, 30, tzinfo=MYT)).json()["event"]["id"]
    r = tc.get("/api/schedule?date_from=2026-09-07&date_to=2026-09-13").json()
    assert any(e["id"] == uni and e["occurrence_date"] == "2026-09-07" for e in r["events"])
    assert [frozenset(c["event_ids"]) for c in r["conflicts"]] == [frozenset({uni, clash})]


@pytest.mark.parametrize("qs,code", [
    ("date_from=2026-09-07&date_to=2026-09-13", 200),                       # canonical week
    ("date_from=2026-09-07T00:00:00&date_to=2026-09-14T00:00:00", 200),     # datetimes still supported
    ("date_from=2026-13-01&date_to=2026-13-07", 422),                       # impossible calendar date
    ("date_from=2026-09-07", 422),                                          # lone param
    ("date_to=2026-09-13", 422),                                            # lone param
    ("date_from=2026-09-20&date_to=2026-09-07", 422),                       # end before start
    ("date_from=garbage&date_to=2026-09-13", 422),                          # non-date
])
def test_schedule_window_param_validation(alice, qs, code):
    tc, _ = alice
    assert tc.get(f"/api/schedule?{qs}").status_code == code


def test_schedule_day_and_month_unaffected_by_window_fix(alice):
    """/api/schedule/day and /api/schedule/month use their own midnight-based
    bounds and must stay correct."""
    tc, _ = alice
    _mk_event(tc, "Edge AM",
              datetime.datetime(2026, 9, 7, 0, 15, tzinfo=MYT),
              datetime.datetime(2026, 9, 7, 0, 45, tzinfo=MYT))
    _mk_event(tc, "Edge PM",
              datetime.datetime(2026, 9, 7, 23, 15, tzinfo=MYT),
              datetime.datetime(2026, 9, 7, 23, 45, tzinfo=MYT))
    day = tc.get("/api/schedule/day?date=2026-09-07").json()
    assert {"Edge AM", "Edge PM"} <= {e["title"] for e in day["events"]}
    assert day["range"] == {"from": "2026-09-07T00:00:00+08:00",
                            "to": "2026-09-08T00:00:00+08:00"}
    prev = tc.get("/api/schedule/day?date=2026-09-06").json()
    assert "Edge AM" not in {e["title"] for e in prev["events"]}
    month = tc.get("/api/schedule/month?year=2026&month=9").json()
    assert month["day_counts"].get("2026-09-07", {}).get("events", 0) >= 2


# ===================== calendar / month =====================
def test_month_density_and_recurrence(alice):
    tc, _ = alice
    n = _now()
    mon = (n.date() - datetime.timedelta(days=n.weekday()))
    _mk_event(tc, "Standup",
              datetime.datetime.combine(mon, datetime.time(9, 0), MYT),
              datetime.datetime.combine(mon, datetime.time(9, 15), MYT),
              recurrence="weekly",
              recurrence_until=_iso(datetime.datetime.combine(mon + datetime.timedelta(days=28), datetime.time(0), MYT)))
    m = tc.get(f"/api/schedule/month?year={n.year}&month={n.month}").json()
    # at least the recurring occurrences that fall in this month are counted
    recurring_days = [k for k, v in m["day_counts"].items() if v["events"] >= 1]
    assert len(recurring_days) >= 3
    assert tc.get("/api/schedule/month?year=2026&month=13").status_code == 422


# ===================== timezone day boundary =====================
def test_myt_day_boundary(alice):
    tc, _ = alice
    n = _now()
    _mk_event(tc, "Late", n.replace(hour=23, minute=30), n.replace(hour=23, minute=45))
    tomo = n + datetime.timedelta(days=1)
    _mk_event(tc, "EarlyNext", tomo.replace(hour=0, minute=15), tomo.replace(hour=0, minute=30))
    today = tc.get(f"/api/schedule/day?date={n.date().isoformat()}").json()
    nxt = tc.get(f"/api/schedule/day?date={tomo.date().isoformat()}").json()
    today_titles = [e["title"] for e in today["events"]]
    assert "Late" in today_titles and "EarlyNext" not in today_titles
    assert "EarlyNext" in [e["title"] for e in nxt["events"]]


# ===================== recommendation snooze lifecycle =====================
def test_recommendation_snooze_reappears(alice):
    tc, user = alice
    c = db_layer.get_connection()
    c.execute("INSERT INTO ai_recommendations (id,user_id,module,type,message,status) "
              "VALUES ('rs', ?, 'SCHEDULE', 'INSIGHT', 'overlap', 'PENDING')", (user["id"],))
    c.commit(); c.close()
    assert any(r["id"] == "rs" for r in tc.get("/api/schedule").json()["recommendations"])

    assert tc.post("/api/recommendations/update",
                   json={"recommendation_id": "rs", "action": "SNOOZE", "snooze_minutes": 120}).status_code == 200
    assert all(r["id"] != "rs" for r in tc.get("/api/schedule").json()["recommendations"])

    # move snooze into the past -> it comes back (status was never terminal)
    c = db_layer.get_connection()
    c.execute("UPDATE ai_recommendations SET snooze_until = '2000-01-01T00:00:00+08:00' WHERE id='rs'")
    c.commit(); c.close()
    assert any(r["id"] == "rs" for r in tc.get("/api/schedule").json()["recommendations"])

    assert tc.post("/api/recommendations/update", json={"recommendation_id": "rs", "action": "DISMISSED"}).status_code == 200
    assert all(r["id"] != "rs" for r in tc.get("/api/schedule").json()["recommendations"])
    assert tc.post("/api/recommendations/update", json={"recommendation_id": "ghost", "action": "DISMISSED"}).status_code == 404


# ===================== Mo schedule actions (proposal -> confirm) =====================
from conftest import mo_propose, mo_confirm, mo_run  # noqa: E402


def test_ai_event_proposal_then_confirm_for_caller_only(alice, bob, mock_ai):
    ac, a = alice
    _, b = bob
    start = _iso(_now().replace(hour=15) + datetime.timedelta(days=1))
    mock_ai("CREATE_EVENT", parameters={"title": "AI meeting", "start": start, "end": None,
                                        "location": "HQ", "all_day": False,
                                        "user_id": b["id"], "owner": b["id"]})
    _p, conf = mo_run(ac, "add a meeting tomorrow 3pm")
    assert conf.status_code == 200 and conf.json()["kind"] == "executed"
    c = db_layer.get_connection()
    owner = c.execute("SELECT user_id FROM schedule_events WHERE title='AI meeting'").fetchone()[0]
    bcount = c.execute("SELECT COUNT(*) FROM schedule_events WHERE user_id=?", (b["id"],)).fetchone()[0]
    c.close()
    assert owner == a["id"] and bcount == 0


def test_ai_event_invalid_makes_no_proposal_and_no_mutation(alice, mock_ai):
    tc, _ = alice
    mock_ai("CREATE_EVENT", parameters={"title": "Broken", "start": "totally-invalid"})
    r = tc.post("/api/process-input", json={"user_input": "add broken"})
    assert r.status_code == 200 and r.json()["kind"] == "error"
    c = db_layer.get_connection()
    assert c.execute("SELECT COUNT(*) FROM schedule_events WHERE title='Broken'").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM mo_proposals").fetchone()[0] == 0
    c.close()


def test_ai_confirm_duplicate_does_not_duplicate_event(alice, mock_ai):
    tc, _ = alice
    start = _iso(_now().replace(hour=15) + datetime.timedelta(days=1))
    mock_ai("CREATE_EVENT", parameters={"title": "Solo", "start": start, "end": None,
                                        "location": None, "all_day": False})
    pid = mo_propose(tc, "add solo event tomorrow 3pm", "sched-tok-1").json()["proposal"]["proposal_id"]
    mo_confirm(tc, pid); mo_confirm(tc, pid)
    assert len(tc.get("/api/schedule").json()["all_events"]) == 1


def test_ai_schedule_summary_is_an_answer(alice, mock_ai):
    tc, _ = alice
    _mk_event(tc, "Standup", _now() + datetime.timedelta(minutes=30), _now() + datetime.timedelta(minutes=45))
    mock_ai("GET_SCHEDULE_SUMMARY")
    j = tc.post("/api/process-input", json={"user_input": "what's my day looking like"}).json()
    assert j["kind"] == "answer" and "event" in j["message"].lower()
    c = db_layer.get_connection()
    assert c.execute("SELECT COUNT(*) FROM mo_proposals").fetchone()[0] == 0
    c.close()


# ===================== persistence round-trips =====================
def test_event_persists_across_refetch(alice):
    tc, _ = alice
    ev = _mk_event(tc, "Persist", _now().replace(hour=9), _now().replace(hour=10)).json()["event"]["id"]
    assert any(e["id"] == ev for e in tc.get("/api/schedule").json()["all_events"])
    tc.patch(f"/api/events/{ev}", json={"title": "Persist v2"})
    assert next(e for e in tc.get("/api/schedule").json()["all_events"] if e["id"] == ev)["title"] == "Persist v2"
    tc.delete(f"/api/events/{ev}")
    assert all(e["id"] != ev for e in tc.get("/api/schedule").json()["all_events"])


def test_schedule_db_integrity_after_ops(alice):
    tc, _ = alice
    tk = tc.post("/api/tasks", json={"title": "T"}).json()["task"]["id"]
    _mk_event(tc, "E", _now(), _now() + datetime.timedelta(hours=1), related_task_id=tk)
    c = db_layer.get_connection()
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    c.close()


def test_related_task_fk_set_null_on_task_delete(alice):
    tc, _ = alice
    tk = tc.post("/api/tasks", json={"title": "Linked"}).json()["task"]["id"]
    ev = _mk_event(tc, "Has link", _now(), _now() + datetime.timedelta(hours=1),
                   related_task_id=tk).json()["event"]["id"]
    tc.delete(f"/api/tasks/{tk}")
    got = next(e for e in tc.get("/api/schedule").json()["all_events"] if e["id"] == ev)
    assert got["related_task_id"] is None


# ===================== old route gone =====================
def test_resolve_conflict_route_removed(alice):
    tc, _ = alice
    assert tc.post("/api/schedule/resolve-conflict", json={"x": 1}).status_code in (404, 405)


# ===================== migration 0009 =====================
def test_migration_0009_schema_and_preservation(fresh_db_path):
    import subprocess, sys, os
    root = os.path.dirname(os.path.dirname(__file__))
    env = dict(os.environ, COREOS_DB_PATH=fresh_db_path)

    c = sqlite3.connect(fresh_db_path)
    cols = {r[1] for r in c.execute("PRAGMA table_info(schedule_events)")}
    assert {"notes", "all_day", "status", "source_type", "external_id",
            "recurrence", "recurrence_until", "related_task_id"} <= cols
    assert {"notes", "planned_at", "completed_at"} <= {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
    assert "snooze_until" in {r[1] for r in c.execute("PRAGMA table_info(ai_recommendations)")}
    fks = [x[2] for x in c.execute("PRAGMA foreign_key_list(schedule_events)")]
    assert "tasks" in fks and "users" in fks
    c.close()

    # downgrade -> re-upgrade preserves & lands at head
    assert subprocess.run([sys.executable, "-m", "alembic", "downgrade", "0008_drop_user_budgets"],
                          cwd=root, env=env, capture_output=True).returncode == 0
    c = sqlite3.connect(fresh_db_path)
    assert "snooze_until" not in {r[1] for r in c.execute("PRAGMA table_info(ai_recommendations)")}
    assert {r[1] for r in c.execute("PRAGMA table_info(schedule_events)")} == \
        {"id", "user_id", "title", "location", "start_time", "end_time", "created_at", "updated_at"}
    c.close()
    assert subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                          cwd=root, env=env, capture_output=True).returncode == 0
    c = sqlite3.connect(fresh_db_path)
    # `upgrade head` must land on the single current head (0009 first re-applied,
    # then any later additive migration such as 0010_event_place_reference).
    head = subprocess.run([sys.executable, "-m", "alembic", "heads"],
                          cwd=root, env=env, capture_output=True, text=True).stdout.split()[0]
    assert c.execute("SELECT version_num FROM alembic_version").fetchone()[0] == head
    # 0009's schema is restored regardless of any migration stacked on top of it.
    assert "snooze_until" in {r[1] for r in c.execute("PRAGMA table_info(ai_recommendations)")}
    assert {"recurrence", "recurrence_until", "related_task_id"} <= \
        {r[1] for r in c.execute("PRAGMA table_info(schedule_events)")}
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    c.close()
