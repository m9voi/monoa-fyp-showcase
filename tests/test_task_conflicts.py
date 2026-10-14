"""B7 — task work-window <-> event schedule conflicts.

A task participates ONLY when it has a real [planned_at, scheduled_end) window
(end > start). A due_date / reminder_at alone is never a time-block conflict.
Moves change the task window only (planned_at + scheduled_end) — never the
deadline, never the event. Occurrence-aware for recurring events (B6). All
decisions persist, are exactly-once, IDOR-safe, and stale on a material edit.

DETERMINISTIC — temp DB via conftest; no paid calls; the reminder scheduler is
disabled (conftest sets COREOS_SCHEDULER_ENABLED=0).
"""
import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import tempfile

import pytest

import main
import db as db_layer
import scheduler as reminder_scheduler
import task_conflict as tec
from conftest import new_authed_client

MYT = dt.timezone(dt.timedelta(hours=8))


def _now():
    return dt.datetime.now(MYT).replace(minute=0, second=0, microsecond=0)


def _mk_event(tc, title, start, end=None, **kw):
    body = {"title": title, "start": start.isoformat()}
    if end is not None:
        body["end"] = end.isoformat()
    body.update(kw)
    r = tc.post("/api/events", json=body)
    assert r.status_code == 201, r.text
    return r.json()["event"]


def _mk_task(tc, title, *, planned=None, end=None, due=None, reminder=None):
    body = {"title": title}
    if planned is not None:
        body["planned_at"] = planned.isoformat()
    if end is not None:
        body["scheduled_end"] = end.isoformat()
    if due is not None:
        body["due_date"] = due if isinstance(due, str) else due.isoformat()
    if reminder is not None:
        body["reminder_at"] = reminder.isoformat()
    r = tc.post("/api/tasks", json=body)
    return r


def _task_conflicts(tc):
    r = tc.get("/api/schedule/task-conflicts")
    assert r.status_code == 200, r.text
    return r.json()


def _task_row(uid, tid):
    c = db_layer.get_connection()
    try:
        r = c.execute("SELECT title, planned_at, scheduled_end, due_date, reminder_at, reminder_status "
                      "FROM tasks WHERE id=? AND user_id=?", (tid, uid)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


def _decisions(uid):
    c = db_layer.get_connection()
    try:
        return [dict(r) for r in c.execute(
            "SELECT * FROM task_event_conflict_decisions WHERE user_id=?", (uid,)).fetchall()]
    finally:
        c.close()


def _relogin(email, password="password123"):
    from fastapi.testclient import TestClient
    tc = TestClient(main.app)
    assert tc.post("/api/auth/login", json={"email": email, "password": password}).status_code == 200
    return tc


# a deterministic overlap: event 14:00-15:00 tomorrow, task window 14:30-15:30
def _overlap(tc, *, day_offset=1):
    d = (_now() + dt.timedelta(days=day_offset)).replace(hour=14)
    ev = _mk_event(tc, "Board meeting", d, d + dt.timedelta(hours=1))
    r = _mk_task(tc, "Write report", planned=d + dt.timedelta(minutes=30),
                 end=d + dt.timedelta(minutes=90), due=(d + dt.timedelta(days=3)).date().isoformat())
    assert r.status_code == 201, r.text
    return ev, r.json()["task"], d


# ===================================================================== #
# 1 / 2 / 4 — eligibility: only a real work window conflicts
# ===================================================================== #
def test_untimed_task_never_conflicts():
    tc, _ = new_authed_client("b7untimed@example.com")
    d = (_now() + dt.timedelta(days=1)).replace(hour=14)
    _mk_event(tc, "Meeting", d, d + dt.timedelta(hours=1))
    assert _mk_task(tc, "Buy milk").status_code == 201
    assert _task_conflicts(tc)["conflicts"] == []


def test_deadline_only_task_is_not_a_time_block_conflict():
    tc, _ = new_authed_client("b7deadline@example.com")
    d = (_now() + dt.timedelta(days=1)).replace(hour=14)
    _mk_event(tc, "Meeting", d, d + dt.timedelta(hours=1))
    # due_date on the same day, and even a planned_at POINT inside the event
    assert _mk_task(tc, "Pay invoice", due=d.date().isoformat()).status_code == 201
    assert _mk_task(tc, "Call bank", planned=d + dt.timedelta(minutes=15)).status_code == 201
    assert _task_conflicts(tc)["conflicts"] == []


def test_scheduled_end_without_planned_start_is_422():
    tc, _ = new_authed_client("b7endonly@example.com")
    d = (_now() + dt.timedelta(days=1)).replace(hour=14)
    assert _mk_task(tc, "x", end=d).status_code == 422


def test_end_not_after_start_is_422():
    tc, _ = new_authed_client("b7badwin@example.com")
    d = (_now() + dt.timedelta(days=1)).replace(hour=14)
    assert _mk_task(tc, "x", planned=d, end=d).status_code == 422
    assert _mk_task(tc, "y", planned=d, end=d - dt.timedelta(hours=1)).status_code == 422


def test_non_overlapping_scheduled_task_has_no_conflict():
    tc, _ = new_authed_client("b7noover@example.com")
    d = (_now() + dt.timedelta(days=1)).replace(hour=14)
    _mk_event(tc, "Meeting", d, d + dt.timedelta(hours=1))
    # task window 15:00-16:00 — touches the 15:00 end, half-open => no overlap
    assert _mk_task(tc, "Deep work", planned=d + dt.timedelta(hours=1),
                    end=d + dt.timedelta(hours=2)).status_code == 201
    assert _task_conflicts(tc)["conflicts"] == []


# ===================================================================== #
# 3 — one real overlap -> exactly one conflict
# ===================================================================== #
def test_task_window_overlapping_event_yields_exactly_one_conflict():
    tc, _ = new_authed_client("b7one@example.com")
    ev, task, d = _overlap(tc)
    data = _task_conflicts(tc)
    assert len(data["conflicts"]) == 1 and len(data["unresolved"]) == 1
    c = data["conflicts"][0]
    assert c["conflict_type"] == "task_event_overlap"
    assert c["scope"] == "series" and c["occurrence_date"] is None
    assert c["task"]["id"] == task["id"] and c["event"]["id"] == ev["id"]
    assert c["task"]["due_date"]                       # deadline carried through, untouched
    assert c["proposal"] and c["proposal"]["summary"]
    # deterministic identity
    assert _task_conflicts(tc)["conflicts"][0]["fingerprint"] == c["fingerprint"]


# ===================================================================== #
# 5 / 7 — recurring event occurrence identity + independence
# ===================================================================== #
# Computed relative to whenever this suite runs (never a fixed calendar
# date) -- see the identical rationale/comment in test_recurring_conflicts.py.
# Task-conflict recompute uses its own (even tighter) back-window, see
# main.py's _TE_RECOMPUTE_BACK_DAYS -- so a hardcoded date goes stale even
# faster here.
def _b7_next_monday(base: dt.datetime) -> dt.date:
    days_ahead = (7 - base.weekday()) % 7 or 7
    return (base + dt.timedelta(days=days_ahead)).date()


_B7_ANCHOR = _b7_next_monday(dt.datetime.now(MYT))
B7_SERIES_START = dt.datetime(_B7_ANCHOR.year, _B7_ANCHOR.month, _B7_ANCHOR.day, 10, 0, tzinfo=MYT) - dt.timedelta(days=7)
B7_OCC_A_DATE = _B7_ANCHOR.isoformat()
B7_OCC_B_DATE = (_B7_ANCHOR + dt.timedelta(days=7)).isoformat()
B7_SERIES_DATES = tuple((B7_SERIES_START.date() + dt.timedelta(days=7 * k)).isoformat() for k in range(5))
B7_SERIES_RECURRENCE_UNTIL = B7_SERIES_DATES[-1]


def _weekly_event(tc):
    return _mk_event(tc, "Standup", B7_SERIES_START, B7_SERIES_START + dt.timedelta(hours=1),
                     recurrence="weekly", recurrence_until=B7_SERIES_RECURRENCE_UNTIL)


def test_task_over_recurring_occurrence_uses_that_occurrence_identity():
    tc, _ = new_authed_client("b7rec@example.com")
    _weekly_event(tc)
    # task window overlapping the OCC_A occurrence (10:00-11:00)
    a = dt.datetime.fromisoformat(B7_OCC_A_DATE).replace(hour=10, minute=30, tzinfo=MYT)
    assert _mk_task(tc, "Prep A", planned=a, end=a + dt.timedelta(hours=1)).status_code == 201
    b = dt.datetime.fromisoformat(B7_OCC_B_DATE).replace(hour=10, minute=30, tzinfo=MYT)
    assert _mk_task(tc, "Prep B", planned=b, end=b + dt.timedelta(hours=1)).status_code == 201

    cs = _task_conflicts(tc)["conflicts"]
    assert len(cs) == 2
    by_date = {c["occurrence_date"]: c for c in cs}
    assert set(by_date) == {B7_OCC_A_DATE, B7_OCC_B_DATE}
    for c in cs:
        assert c["scope"] == "occurrence" and c["event"]["recurring"] is True
    assert by_date[B7_OCC_A_DATE]["fingerprint"] != by_date[B7_OCC_B_DATE]["fingerprint"]


def test_keeping_occurrence_A_conflict_does_not_suppress_occurrence_B():
    email = "b7reckeep@example.com"
    tc, _ = new_authed_client(email)
    _weekly_event(tc)
    a = dt.datetime.fromisoformat(B7_OCC_A_DATE).replace(hour=10, minute=30, tzinfo=MYT)
    _mk_task(tc, "Prep A", planned=a, end=a + dt.timedelta(hours=1))
    b = dt.datetime.fromisoformat(B7_OCC_B_DATE).replace(hour=10, minute=30, tzinfo=MYT)
    _mk_task(tc, "Prep B", planned=b, end=b + dt.timedelta(hours=1))
    by_date = {c["occurrence_date"]: c for c in _task_conflicts(tc)["conflicts"]}
    fpA = by_date[B7_OCC_A_DATE]["fingerprint"]

    r = tc.post(f"/api/schedule/task-conflicts/{fpA}/keep")
    assert r.status_code == 200 and r.json()["scope"] == "occurrence"
    after = {c["occurrence_date"]: c for c in _task_conflicts(tc)["conflicts"]}
    assert after[B7_OCC_A_DATE]["decision"] == "kept"
    assert after[B7_OCC_B_DATE]["decision"] is None
    # persists across re-login
    tc2 = _relogin(email)
    after2 = {c["occurrence_date"]: c for c in _task_conflicts(tc2)["conflicts"]}
    assert after2[B7_OCC_A_DATE]["decision"] == "kept" and after2[B7_OCC_B_DATE]["decision"] is None


# ===================================================================== #
# 6 — Keep persists across refresh + logout/login
# ===================================================================== #
def test_keep_persists_across_refresh_and_relogin():
    email = "b7keep@example.com"
    tc, user = new_authed_client(email)
    ev, task, d = _overlap(tc)
    fp = _task_conflicts(tc)["conflicts"][0]["fingerprint"]
    assert tc.post(f"/api/schedule/task-conflicts/{fp}/keep").status_code == 200
    assert _task_conflicts(tc)["conflicts"][0]["decision"] == "kept"       # refresh
    assert _task_conflicts(tc)["unresolved"] == []
    tc2 = _relogin(email)
    assert _task_conflicts(tc2)["conflicts"][0]["decision"] == "kept"     # re-login
    # nothing about the task changed
    row = _task_row(user["id"], task["id"])
    assert row["planned_at"] == task["planned_at"] and row["scheduled_end"] == task["scheduled_end"]


# ===================================================================== #
# 8 / 9 / 10 — candidates: no mutation, duration preserved, collisions avoided
# ===================================================================== #
def test_searching_candidates_mutates_nothing():
    tc, user = new_authed_client("b7search@example.com")
    ev, task, d = _overlap(tc)
    fp = _task_conflicts(tc)["conflicts"][0]["fingerprint"]
    before = _task_row(user["id"], task["id"])
    r = tc.post(f"/api/schedule/task-conflicts/{fp}/alternatives")
    assert r.status_code == 200 and r.json()["proposal_id"] and r.json()["candidates"]
    assert _task_row(user["id"], task["id"]) == before
    assert _decisions(user["id"]) == []


def test_candidates_preserve_duration_and_avoid_everything():
    tc, user = new_authed_client("b7cand@example.com")
    d = (_now() + dt.timedelta(days=1)).replace(hour=14)
    _mk_event(tc, "Board meeting", d, d + dt.timedelta(hours=1))
    _mk_event(tc, "Later call", d + dt.timedelta(hours=3), d + dt.timedelta(hours=4))
    _weekly_event(tc)                                   # a recurring event too
    other = _mk_task(tc, "Other block", planned=d + dt.timedelta(hours=5),
                     end=d + dt.timedelta(hours=6)).json()["task"]
    task = _mk_task(tc, "Write report", planned=d + dt.timedelta(minutes=30),
                    end=d + dt.timedelta(minutes=90)).json()["task"]
    fp = _task_conflicts(tc)["conflicts"][0]["fingerprint"]
    cands = tc.post(f"/api/schedule/task-conflicts/{fp}/alternatives").json()["candidates"]
    assert cands
    dur = dt.timedelta(hours=1)                         # task window is 60 min
    # gather everything a candidate must miss
    busy = [(d, d + dt.timedelta(hours=1)),
            (d + dt.timedelta(hours=3), d + dt.timedelta(hours=4)),
            (d + dt.timedelta(hours=5), d + dt.timedelta(hours=6))]
    for c in cands:
        s = dt.datetime.fromisoformat(c["start"]); e = dt.datetime.fromisoformat(c["end"])
        assert e - s == dur                             # duration preserved
        assert s > dt.datetime.now(MYT)                 # not in the past
        for bs, be in busy:
            assert not (s < be and bs < e)             # no collision


# ===================================================================== #
# 11 / 12 / 13 — confirm: task window only, deadline intact, exactly once
# ===================================================================== #
def test_confirm_moves_only_the_task_window_and_leaves_deadline_and_event():
    tc, user = new_authed_client("b7confirm@example.com")
    ev, task, d = _overlap(tc)
    ev_before = tc.get("/api/schedule?date_from=%s&date_to=%s" % (
        (d - dt.timedelta(days=1)).date(), (d + dt.timedelta(days=1)).date())).json()
    ev_start_before = next(e for e in ev_before["all_events"] if e["id"] == ev["id"])["start_time"]
    due_before = task["due_date"]

    fp = _task_conflicts(tc)["conflicts"][0]["fingerprint"]
    alt = tc.post(f"/api/schedule/task-conflicts/{fp}/alternatives").json()
    pid = alt["proposal_id"]
    r = tc.post(f"/api/schedule/task-conflicts/alternatives/{pid}/confirm", json={"candidate_index": 0})
    assert r.status_code == 200 and r.json()["decision"] == "moved"
    chosen = alt["candidates"][0]

    row = _task_row(user["id"], task["id"])
    assert row["planned_at"] == dt.datetime.fromisoformat(chosen["start"]).isoformat()
    assert row["scheduled_end"] == dt.datetime.fromisoformat(chosen["end"]).isoformat()
    assert row["due_date"] == due_before               # deadline untouched
    # the event did not move
    ev_after = tc.get("/api/schedule?date_from=%s&date_to=%s" % (
        (d - dt.timedelta(days=1)).date(), (d + dt.timedelta(days=1)).date())).json()
    assert next(e for e in ev_after["all_events"] if e["id"] == ev["id"])["start_time"] == ev_start_before
    # conflict resolved
    assert _task_conflicts(tc)["unresolved"] == []


def test_repeated_confirm_executes_once():
    tc, user = new_authed_client("b7once@example.com")
    ev, task, d = _overlap(tc)
    fp = _task_conflicts(tc)["conflicts"][0]["fingerprint"]
    pid = tc.post(f"/api/schedule/task-conflicts/{fp}/alternatives").json()["proposal_id"]
    first = tc.post(f"/api/schedule/task-conflicts/alternatives/{pid}/confirm", json={"candidate_index": 0})
    assert first.status_code == 200 and first.json()["already"] is False
    moved_to = _task_row(user["id"], task["id"])
    for _ in range(4):
        again = tc.post(f"/api/schedule/task-conflicts/alternatives/{pid}/confirm", json={"candidate_index": 0})
        assert again.status_code == 200 and again.json()["already"] is True
    assert _task_row(user["id"], task["id"]) == moved_to
    assert len([x for x in _decisions(user["id"]) if x["decision"] == "moved"]) == 1


def test_confirm_out_of_range_index_is_422():
    tc, _ = new_authed_client("b7oor@example.com")
    ev, task, d = _overlap(tc)
    fp = _task_conflicts(tc)["conflicts"][0]["fingerprint"]
    pid = tc.post(f"/api/schedule/task-conflicts/{fp}/alternatives").json()["proposal_id"]
    assert tc.post(f"/api/schedule/task-conflicts/alternatives/{pid}/confirm",
                   json={"candidate_index": 99}).status_code == 422


# ===================================================================== #
# 14 / 15 — stale protection
# ===================================================================== #
def test_manual_task_edit_stales_a_pending_proposal():
    tc, user = new_authed_client("b7stale1@example.com")
    ev, task, d = _overlap(tc)
    fp = _task_conflicts(tc)["conflicts"][0]["fingerprint"]
    pid = tc.post(f"/api/schedule/task-conflicts/{fp}/alternatives").json()["proposal_id"]
    # move the task window manually so the old fingerprint no longer matches
    new_s = d + dt.timedelta(minutes=45)
    assert tc.patch(f"/api/tasks/{task['id']}", json={
        "planned_at": new_s.isoformat(), "scheduled_end": (new_s + dt.timedelta(hours=1)).isoformat()
    }).status_code == 200
    r = tc.post(f"/api/schedule/task-conflicts/alternatives/{pid}/confirm", json={"candidate_index": 0})
    assert r.status_code == 409
    assert [x for x in _decisions(user["id"]) if x["decision"] == "moved"] == []


def test_manual_event_edit_stales_a_kept_decision():
    tc, user = new_authed_client("b7stale2@example.com")
    ev, task, d = _overlap(tc)
    fp = _task_conflicts(tc)["conflicts"][0]["fingerprint"]
    assert tc.post(f"/api/schedule/task-conflicts/{fp}/keep").status_code == 200
    # push the event 3h later -> no overlap -> the kept decision is stale
    assert tc.patch(f"/api/events/{ev['id']}", json={
        "start": (d + dt.timedelta(hours=3)).isoformat(),
        "end": (d + dt.timedelta(hours=4)).isoformat()}).status_code == 200
    data = _task_conflicts(tc)
    assert data["conflicts"] == []                      # overlap gone
    c = db_layer.get_connection()
    try:
        row = c.execute("SELECT stale_at FROM task_event_conflict_decisions "
                        "WHERE user_id=? AND fingerprint=?", (user["id"], fp)).fetchone()
    finally:
        c.close()
    assert row and row["stale_at"] is not None


def test_deleted_task_or_event_rejects_confirmation():
    tc, user = new_authed_client("b7del@example.com")
    ev, task, d = _overlap(tc)
    fp = _task_conflicts(tc)["conflicts"][0]["fingerprint"]
    pid = tc.post(f"/api/schedule/task-conflicts/{fp}/alternatives").json()["proposal_id"]
    assert tc.delete(f"/api/events/{ev['id']}").status_code == 200
    r = tc.post(f"/api/schedule/task-conflicts/alternatives/{pid}/confirm", json={"candidate_index": 0})
    assert r.status_code == 409
    assert [x for x in _decisions(user["id"]) if x["decision"] == "moved"] == []


# ===================================================================== #
# 16 — cross-user
# ===================================================================== #
def test_cross_user_cannot_act_on_another_users_task_conflict():
    ta, ua = new_authed_client("b7owner@example.com")
    ev, task, d = _overlap(ta)
    fp = _task_conflicts(ta)["conflicts"][0]["fingerprint"]
    alt = ta.post(f"/api/schedule/task-conflicts/{fp}/alternatives").json()
    pid = alt["proposal_id"]

    tb, ub = new_authed_client("b7intruder@example.com")
    assert tb.get("/api/schedule/task-conflicts").json()["conflicts"] == []
    assert tb.post(f"/api/schedule/task-conflicts/{fp}/keep").status_code == 409
    assert tb.post(f"/api/schedule/task-conflicts/{fp}/alternatives").status_code == 409
    assert tb.post(f"/api/schedule/task-conflicts/alternatives/{pid}/confirm",
                   json={"candidate_index": 0}).status_code == 404
    assert tb.post(f"/api/schedule/task-conflicts/alternatives/{pid}/cancel").status_code == 404
    assert _decisions(ub["id"]) == []
    # owner can still act
    assert ta.post(f"/api/schedule/task-conflicts/{fp}/keep").status_code == 200


# ===================================================================== #
# 18 / 19 — reminders match their real schema semantics; no fake delivery
# ===================================================================== #
def test_reminder_is_a_notification_instant_not_a_schedule_conflict():
    tc, _ = new_authed_client("b7rem@example.com")
    d = (_now() + dt.timedelta(days=1)).replace(hour=14)
    _mk_event(tc, "Meeting", d, d + dt.timedelta(hours=1))
    # a reminder EXACTLY inside the event window
    r = _mk_task(tc, "Nudge me", reminder=d + dt.timedelta(minutes=30))
    assert r.status_code == 201
    assert r.json()["task"]["reminder_status"] == "PENDING"
    assert _task_conflicts(tc)["conflicts"] == []       # a reminder never conflicts


def test_reminder_crud_and_persistence_are_intact():
    email = "b7remcrud@example.com"
    tc, user = new_authed_client(email)
    when = (_now() + dt.timedelta(days=1)).replace(hour=9)
    t = _mk_task(tc, "Take pills", reminder=when).json()["task"]
    assert t["reminder_at"] and t["reminder_status"] == "PENDING"
    # edit
    assert tc.patch(f"/api/tasks/{t['id']}", json={
        "reminder_at": (when + dt.timedelta(hours=1)).isoformat()}).status_code == 200
    assert _task_row(user["id"], t["id"])["reminder_status"] == "PENDING"
    # clear
    assert tc.patch(f"/api/tasks/{t['id']}", json={"reminder_at": None}).status_code == 200
    row = _task_row(user["id"], t["id"])
    assert row["reminder_at"] is None and row["reminder_status"] == "NONE"
    # survives re-login
    tc2 = _relogin(email)
    got = tc2.get("/api/schedule/day?date=%s" % when.date().isoformat()).json()
    assert any(x["id"] == t["id"] for x in got["tasks"])


def test_reminder_scheduler_only_flips_status_and_delivers_nothing():
    # the scheduler's sole job is PENDING -> DUE; there is no delivery channel.
    tc, user = new_authed_client("b7remsched@example.com")
    past = _now() - dt.timedelta(hours=2)
    t = _mk_task(tc, "Overdue nudge", reminder=past).json()["task"]
    promoted = reminder_scheduler.scan_due_reminders_once()
    assert promoted >= 1
    assert _task_row(user["id"], t["id"])["reminder_status"] == "DUE"
    # no push / web-push / notification delivery surface exists
    src = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "scheduler.py")).read()
    for banned in ("web-push", "webpush", "PushManager", "VAPID", "firebase", "send_notification"):
        assert banned.lower() not in src.lower()
    # no endpoint claims to deliver notifications
    routes = {getattr(r, "path", "") for r in main.app.routes}
    assert not any("notification" in p or "push" in p for p in routes)


# ===================================================================== #
# 20 — migration upgrade/downgrade on a fresh disposable DB
# ===================================================================== #
def test_migration_0017_applies_and_downgrades_on_fresh_db():
    tmp = tempfile.mkdtemp(prefix="coreos_b7_")
    dbp = os.path.join(tmp, "fresh.db")
    env = dict(os.environ, COREOS_DB_PATH=dbp)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # Pinned to 0017 itself (not "head") — this test verifies 0017's own
    # upgrade/downgrade content, independent of whatever migration comes after.
    up = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "0017_task_event_conflicts"],
                        cwd=root, env=env, capture_output=True, text=True)
    assert up.returncode == 0, up.stderr
    con = sqlite3.connect(dbp)
    try:
        assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0017_task_event_conflicts"
        assert "scheduled_end" in {r[1] for r in con.execute("PRAGMA table_info(tasks)")}
        ted = {r[1] for r in con.execute("PRAGMA table_info(task_event_conflict_decisions)")}
        assert {"user_id", "fingerprint", "task_id", "event_id", "occurrence_date", "decision"} <= ted
        tmp_cols = {r[1] for r in con.execute("PRAGMA table_info(task_move_proposals)")}
        assert {"status", "candidates_json", "task_window_json", "expires_at"} <= tmp_cols
        assert con.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND tbl_name='task_event_conflict_decisions' AND sql LIKE '%UNIQUE%'").fetchone()
    finally:
        con.close()
    dn = subprocess.run([sys.executable, "-m", "alembic", "downgrade", "0016_recurring_conflict_decisions"],
                        cwd=root, env=env, capture_output=True, text=True)
    assert dn.returncode == 0, dn.stderr
    con = sqlite3.connect(dbp)
    try:
        assert "scheduled_end" not in {r[1] for r in con.execute("PRAGMA table_info(tasks)")}
        assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='task_event_conflict_decisions'").fetchone()
    finally:
        con.close()


# ===================================================================== #
# non-recurring B3 / recurring B6 event-overlap decisions unchanged
# ===================================================================== #
def test_task_conflicts_do_not_disturb_event_overlap_decisions():
    tc, user = new_authed_client("b7iso@example.com")
    d = (_now() + dt.timedelta(days=1)).replace(hour=14)
    a = _mk_event(tc, "A", d, d + dt.timedelta(hours=1))
    b = _mk_event(tc, "B", d + dt.timedelta(minutes=30), d + dt.timedelta(minutes=90))
    # also a task-window conflict on top of the same events
    _mk_task(tc, "Prep", planned=d + dt.timedelta(minutes=15), end=d + dt.timedelta(minutes=75))

    day = tc.get("/api/schedule/day?date=%s" % d.date().isoformat()).json()
    ev_conf = day["conflicts"]
    assert len(ev_conf) == 1 and ev_conf[0]["conflict_type"] == "event_overlap"
    assert len(day["task_conflicts"]) >= 1
    # resolving the event overlap does not touch task decisions and vice-versa
    assert tc.post(f"/api/schedule/conflicts/{ev_conf[0]['fingerprint']}/keep").status_code == 200
    tfp = day["task_conflicts"][0]["fingerprint"]
    assert tc.post(f"/api/schedule/task-conflicts/{tfp}/keep").status_code == 200
    c = db_layer.get_connection()
    try:
        assert c.execute("SELECT COUNT(*) FROM schedule_conflict_decisions WHERE user_id=?",
                         (user["id"],)).fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM task_event_conflict_decisions WHERE user_id=?",
                         (user["id"],)).fetchone()[0] == 1
    finally:
        c.close()


# ===================================================================== #
# B7.1 — candidate quality: reasonable hours + due-date + deterministic rank
# ===================================================================== #
_DAY_START_MIN = 7 * 60
_DAY_END_MIN = 22 * 60


def _mins(iso):
    x = dt.datetime.fromisoformat(iso)
    return x.hour * 60 + x.minute


def _alts(tc, fp):
    r = tc.post(f"/api/schedule/task-conflicts/{fp}/alternatives")
    assert r.status_code == 200, r.text
    return r.json()


def _daytime_conflict(tc, *, due_days=3, day_offset=1):
    """event 14:00-15:00, task work block 14:30-15:30 (ordinary-hours origin),
    with an optional due_date `due_days` out."""
    d = (_now() + dt.timedelta(days=day_offset)).replace(hour=14)
    ev = _mk_event(tc, "Board meeting", d, d + dt.timedelta(hours=1))
    body = {"title": "Write report", "planned_at": (d + dt.timedelta(minutes=30)).isoformat(),
            "scheduled_end": (d + dt.timedelta(minutes=90)).isoformat()}
    if due_days is not None:
        body["due_date"] = (d + dt.timedelta(days=due_days)).date().isoformat() + "T23:59:00+08:00"
    t = tc.post("/api/tasks", json=body).json()["task"]
    fp = next(c["fingerprint"] for c in _task_conflicts(tc)["conflicts"] if c["task"]["id"] == t["id"])
    return ev, t, d, fp


def test_b71_daytime_task_never_gets_a_deep_night_candidate():
    tc, _ = new_authed_client("b71day@example.com")
    _, t, d, fp = _daytime_conflict(tc)
    for c in _alts(tc, fp)["candidates"]:
        assert _mins(c["start"]) >= _DAY_START_MIN, f"candidate starts before 07:00: {c['summary']}"
        assert _mins(c["end"]) <= _DAY_END_MIN, f"candidate ends after 22:00: {c['summary']}"
        assert not (0 <= _mins(c["start"]) < _DAY_START_MIN)          # never a 2:30 AM slot


def test_b71_all_daytime_candidates_are_inside_the_documented_window():
    tc, _ = new_authed_client("b71win@example.com")
    _, t, d, fp = _daytime_conflict(tc, due_days=10)
    cands = _alts(tc, fp)["candidates"]
    assert cands
    for c in cands:
        s, e = dt.datetime.fromisoformat(c["start"]), dt.datetime.fromisoformat(c["end"])
        assert s.date() == e.date()
        assert _DAY_START_MIN <= _mins(c["start"]) and _mins(c["end"]) <= _DAY_END_MIN


def test_b71_candidate_duration_is_preserved():
    tc, _ = new_authed_client("b71dur@example.com")
    _, t, d, fp = _daytime_conflict(tc)
    want = dt.datetime.fromisoformat(t["scheduled_end"]) - dt.datetime.fromisoformat(t["planned_at"])
    for c in _alts(tc, fp)["candidates"]:
        assert dt.datetime.fromisoformat(c["end"]) - dt.datetime.fromisoformat(c["start"]) == want


def test_b71_candidate_avoids_events_recurring_occurrences_and_task_windows():
    tc, _ = new_authed_client("b71avoid@example.com")
    d = (_now() + dt.timedelta(days=1)).replace(hour=14)
    _mk_event(tc, "Board", d, d + dt.timedelta(hours=1))
    _mk_event(tc, "Call", d + dt.timedelta(hours=2), d + dt.timedelta(hours=3))
    _weekly_event(tc)                                   # recurring occurrences in the pool
    _mk_task(tc, "Other block", planned=d + dt.timedelta(hours=4), end=d + dt.timedelta(hours=5))
    t = tc.post("/api/tasks", json={
        "title": "Write report", "planned_at": (d + dt.timedelta(minutes=30)).isoformat(),
        "scheduled_end": (d + dt.timedelta(minutes=90)).isoformat()}).json()["task"]
    fp = next(c["fingerprint"] for c in _task_conflicts(tc)["conflicts"] if c["task"]["id"] == t["id"])
    busy = [(d, d + dt.timedelta(hours=1)),
            (d + dt.timedelta(hours=2), d + dt.timedelta(hours=3)),
            (d + dt.timedelta(hours=4), d + dt.timedelta(hours=5))]
    for c in _alts(tc, fp)["candidates"]:
        s, e = dt.datetime.fromisoformat(c["start"]), dt.datetime.fromisoformat(c["end"])
        for bs, be in busy:
            assert not (s < be and bs < e), f"candidate {c['summary']} collides with an event/task"
        if s.date().isoformat() in B7_SERIES_DATES:
            assert not (10 * 60 <= _mins(c["start"]) < 11 * 60), "candidate lands on a weekly occurrence"


def test_b71_candidate_end_never_exceeds_due_date():
    tc, _ = new_authed_client("b71due@example.com")
    _, t, d, fp = _daytime_conflict(tc, due_days=2)
    due = dt.datetime.fromisoformat(t["due_date"])
    cands = _alts(tc, fp)["candidates"]
    assert cands
    for c in cands:
        assert dt.datetime.fromisoformat(c["end"]) <= due


def test_b71_fully_booked_before_deadline_returns_no_proposal_and_no_mutation():
    tc, user = new_authed_client("b71noslot@example.com")
    day0 = _now().replace(hour=0)
    _mk_event(tc, "Solid", day0 + dt.timedelta(hours=1), day0 + dt.timedelta(hours=22))
    t = tc.post("/api/tasks", json={
        "title": "Cram", "planned_at": (day0 + dt.timedelta(hours=9, minutes=30)).isoformat(),
        "scheduled_end": (day0 + dt.timedelta(hours=10, minutes=30)).isoformat(),
        "due_date": day0.date().isoformat() + "T23:59:00+08:00"}).json()["task"]
    fp = next(c["fingerprint"] for c in _task_conflicts(tc)["conflicts"] if c["task"]["id"] == t["id"])
    r = _alts(tc, fp)
    assert r["proposal_id"] is None
    assert "deadline" in r["message"].lower()
    c = db_layer.get_connection()
    try:
        assert c.execute("SELECT COUNT(*) FROM task_move_proposals WHERE user_id=?", (user["id"],)).fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM task_event_conflict_decisions WHERE user_id=?", (user["id"],)).fetchone()[0] == 0
    finally:
        c.close()
    assert _task_row(user["id"], t["id"])["planned_at"] == (day0 + dt.timedelta(hours=9, minutes=30)).isoformat()


def test_b71_task_without_a_due_date_still_gets_candidates_inside_14_days():
    tc, _ = new_authed_client("b71nodue@example.com")
    _, t, d, fp = _daytime_conflict(tc, due_days=None)
    now = dt.datetime.now(MYT)
    cands = _alts(tc, fp)["candidates"]
    assert cands
    for c in cands:
        assert dt.datetime.fromisoformat(c["start"]) <= now + dt.timedelta(days=14)
        assert _DAY_START_MIN <= _mins(c["start"]) and _mins(c["end"]) <= _DAY_END_MIN


def test_b71_after_hours_original_gets_a_comparable_after_hours_candidate():
    tc, _ = new_authed_client("b71night@example.com")
    d = (_now() + dt.timedelta(days=1)).replace(hour=23)         # 23:00 start = after-hours origin
    _mk_event(tc, "Night op", d, d + dt.timedelta(hours=1))
    t = tc.post("/api/tasks", json={
        "title": "Night task", "planned_at": (d + dt.timedelta(minutes=30)).isoformat(),
        "scheduled_end": (d + dt.timedelta(minutes=90)).isoformat()}).json()["task"]
    fp = next(c["fingerprint"] for c in _task_conflicts(tc)["conflicts"] if c["task"]["id"] == t["id"])
    cands = _alts(tc, fp)["candidates"]
    assert cands, "after-hours origin got no candidate"
    for c in cands:
        assert _mins(c["start"]) >= _DAY_END_MIN or _mins(c["end"]) <= _DAY_START_MIN


def test_b71_candidate_ordering_is_deterministic_and_ranked_by_closeness():
    tc, _ = new_authed_client("b71rank@example.com")
    _, t, d, fp = _daytime_conflict(tc, due_days=10)
    a1 = [c["start"] for c in _alts(tc, fp)["candidates"]]
    a2 = [c["start"] for c in _alts(tc, fp)["candidates"]]
    assert a1 == a2 and a1                                          # stable + non-empty
    orig = dt.datetime.fromisoformat(t["planned_at"])
    dists = [abs((dt.datetime.fromisoformat(s) - orig).total_seconds()) for s in a1]
    assert dists == sorted(dists)                                   # closest-first


def test_b71_confirm_rechecks_deadline_and_rejects_a_now_invalid_candidate():
    tc, user = new_authed_client("b71recheck@example.com")
    _, t, d, fp = _daytime_conflict(tc, due_days=5)
    alt = _alts(tc, fp)
    pid, chosen = alt["proposal_id"], alt["candidates"][0]
    new_due = (dt.datetime.fromisoformat(chosen["start"]) - dt.timedelta(hours=1)).isoformat()
    assert tc.patch(f"/api/tasks/{t['id']}", json={"due_date": new_due}).status_code == 200
    r = tc.post(f"/api/schedule/task-conflicts/alternatives/{pid}/confirm", json={"candidate_index": 0})
    assert r.status_code == 409
    row = _task_row(user["id"], t["id"])
    assert row["planned_at"] == t["planned_at"]
    assert row["due_date"] == dt.datetime.fromisoformat(new_due).isoformat()
    assert [x for x in _decisions(user["id"]) if x["decision"] == "moved"] == []


def test_b71_manual_deadline_change_after_search_makes_the_proposal_stale():
    tc, user = new_authed_client("b71staledue@example.com")
    _, t, d, fp = _daytime_conflict(tc, due_days=6)
    pid = _alts(tc, fp)["proposal_id"]
    assert tc.patch(f"/api/tasks/{t['id']}", json={
        "due_date": (_now() - dt.timedelta(days=1)).date().isoformat() + "T23:59:00+08:00"}).status_code == 200
    r = tc.post(f"/api/schedule/task-conflicts/alternatives/{pid}/confirm", json={"candidate_index": 0})
    assert r.status_code == 409
    assert [x for x in _decisions(user["id"]) if x["decision"] == "moved"] == []


def test_b71_keep_confirm_and_ownership_unchanged_under_the_new_policy():
    tc, user = new_authed_client("b71core@example.com")
    _, t, d, fp = _daytime_conflict(tc, due_days=4)
    assert tc.post(f"/api/schedule/task-conflicts/{fp}/keep").status_code == 200
    assert _task_conflicts(tc)["conflicts"][0]["decision"] == "kept"

    d2 = (_now() + dt.timedelta(days=1)).replace(hour=9)
    _mk_event(tc, "AM", d2, d2 + dt.timedelta(hours=1))
    t2 = tc.post("/api/tasks", json={"title": "Draft", "planned_at": (d2 + dt.timedelta(minutes=30)).isoformat(),
                 "scheduled_end": (d2 + dt.timedelta(minutes=90)).isoformat(),
                 "due_date": (d2 + dt.timedelta(days=6)).date().isoformat() + "T23:59:00+08:00"}).json()["task"]
    fp2 = next(c["fingerprint"] for c in _task_conflicts(tc)["conflicts"] if c["task"]["id"] == t2["id"])
    pid = _alts(tc, fp2)["proposal_id"]
    r1 = tc.post(f"/api/schedule/task-conflicts/alternatives/{pid}/confirm", json={"candidate_index": 0})
    assert r1.status_code == 200 and r1.json()["already"] is False
    for _ in range(3):
        rr = tc.post(f"/api/schedule/task-conflicts/alternatives/{pid}/confirm", json={"candidate_index": 0})
        assert rr.status_code == 200 and rr.json()["already"] is True
    assert _task_row(user["id"], t2["id"])["due_date"] == t2["due_date"]     # deadline unchanged
    assert len([x for x in _decisions(user["id"]) if x["decision"] == "moved"]) == 1

    tb, ub = new_authed_client("b71intruder@example.com")
    assert tb.post(f"/api/schedule/task-conflicts/{fp}/keep").status_code == 409
    assert tb.post(f"/api/schedule/task-conflicts/alternatives/{pid}/confirm", json={"candidate_index": 0}).status_code == 404
