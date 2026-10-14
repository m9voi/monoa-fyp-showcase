"""B3 — Plan conflict Accept / Keep + persisted decisions.

Temp DB (conftest sets COREOS_DB_PATH before importing main), no paid calls.
Covers the required acceptance matrix A–G plus fingerprint determinism /
invalidation, exactly-once, ownership isolation, transaction rollback, and
"a resolved unchanged conflict never resurfaces".
"""
import datetime
import sqlite3

import pytest

import main
import db as db_layer
import schedule_conflict as sconf
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
    r = tc.post("/api/events", json=body)
    assert r.status_code == 201, r.text
    return r.json()["event"]


def _day(tc, when=None):
    d = (when or _now()).date().isoformat()
    return tc.get(f"/api/schedule/day?date={d}").json()


def _only_conflict(tc, when=None):
    data = _day(tc, when)
    conf = data["conflicts"]
    assert len(conf) == 1, conf
    return conf[0], data


def _event_row(user_id, ev_id):
    c = db_layer.get_connection()
    try:
        r = c.execute(
            "SELECT start_time, end_time, title FROM schedule_events WHERE id=? AND user_id=?",
            (ev_id, user_id),
        ).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


def _decisions(user_id):
    c = db_layer.get_connection()
    try:
        return [dict(r) for r in c.execute(
            "SELECT * FROM schedule_conflict_decisions WHERE user_id=?", (user_id,)
        ).fetchall()]
    finally:
        c.close()


def _overlap_pair(tc, base_hour=14):
    """A: base..base+1h   B: base+30m..base+90m  -> deterministic overlap, B is the mover."""
    d = _now().replace(hour=base_hour, minute=0)
    a = _mk_event(tc, "Design Review", d, d + datetime.timedelta(hours=1))
    b = _mk_event(tc, "Team Sync", d + datetime.timedelta(minutes=30),
                  d + datetime.timedelta(minutes=90))
    return a, b, d


# ===================================================================== #
# fingerprint determinism / invalidation
# ===================================================================== #
def test_fingerprint_is_deterministic_for_unchanged_data():
    tc, _ = new_authed_client("fp1@example.com")
    _overlap_pair(tc)
    c1, _ = _only_conflict(tc)
    c2, _ = _only_conflict(tc)
    assert c1["fingerprint"] and c1["fingerprint"] == c2["fingerprint"]
    assert c1["decidable"] is True
    assert c1["conflict_type"] == "event_overlap"


def test_fingerprint_changes_when_a_relevant_time_is_edited():
    tc, _ = new_authed_client("fp2@example.com")
    a, b, d = _overlap_pair(tc)
    fp1 = _only_conflict(tc)[0]["fingerprint"]
    # still overlapping, but B now starts 15 min earlier -> decision-critical data changed
    tc.patch(f"/api/events/{b['id']}", json={
        "start": _iso(d + datetime.timedelta(minutes=15)),
        "end": _iso(d + datetime.timedelta(minutes=75)),
    })
    fp2 = _only_conflict(tc)[0]["fingerprint"]
    assert fp2 and fp2 != fp1


# ===================================================================== #
# A — Keep persistence
# ===================================================================== #
def test_keep_persists_and_unchanged_conflict_does_not_resurface():
    tc, user = new_authed_client("keep1@example.com")
    a, b, _ = _overlap_pair(tc)
    conf, _ = _only_conflict(tc)
    fp = conf["fingerprint"]
    a_before, b_before = _event_row(user["id"], a["id"]), _event_row(user["id"], b["id"])

    r = tc.post(f"/api/schedule/conflicts/{fp}/keep")
    assert r.status_code == 200 and r.json()["decision"] == "kept" and r.json()["already"] is False

    # neither item changed
    assert _event_row(user["id"], a["id"]) == a_before
    assert _event_row(user["id"], b["id"]) == b_before

    # still DETECTED, but marked resolved and absent from unresolved
    data = _day(tc)
    assert len(data["conflicts"]) == 1
    assert data["conflicts"][0]["decision"] == "kept"
    assert data["unresolved_conflicts"] == []

    # logout / login -> the decision still applies for the same user
    tc2 = _relogin("keep1@example.com")
    data2 = tc2.get(f"/api/schedule/day?date={_now().date().isoformat()}").json()
    assert data2["conflicts"][0]["decision"] == "kept"
    assert data2["unresolved_conflicts"] == []


def _relogin(email, password="password123"):
    from fastapi.testclient import TestClient
    tc = TestClient(main.app)
    r = tc.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return tc


def test_keep_is_idempotent():
    tc, user = new_authed_client("keep2@example.com")
    _overlap_pair(tc)
    fp = _only_conflict(tc)[0]["fingerprint"]
    r1 = tc.post(f"/api/schedule/conflicts/{fp}/keep")
    r2 = tc.post(f"/api/schedule/conflicts/{fp}/keep")
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["already"] is False and r2.json()["already"] is True
    assert r1.json()["decision_id"] == r2.json()["decision_id"]
    assert len(_decisions(user["id"])) == 1


# ===================================================================== #
# B — Accept persistence
# ===================================================================== #
def test_accept_applies_exactly_one_canonical_update_and_clears_the_conflict():
    tc, user = new_authed_client("acc1@example.com")
    a, b, d = _overlap_pair(tc)
    conf, _ = _only_conflict(tc)
    fp = conf["fingerprint"]
    assert conf["proposal"]["event_id"] == b["id"]          # later-starting event moves
    assert conf["proposal"]["start"] == a["end_time"]            # to when the earlier one ends

    a_before = _event_row(user["id"], a["id"])
    b_before = _event_row(user["id"], b["id"])
    b_dur = (datetime.datetime.fromisoformat(b_before["end_time"])
             - datetime.datetime.fromisoformat(b_before["start_time"]))

    r = tc.post(f"/api/schedule/conflicts/{fp}/accept")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decision"] == "accepted" and body["already"] is False
    assert body["resolution"]["start"] == a["end_time"]

    # A untouched; B moved to A.end, SAME duration
    assert _event_row(user["id"], a["id"]) == a_before
    b_after = _event_row(user["id"], b["id"])
    assert b_after["start_time"] == a["end_time"]
    assert (datetime.datetime.fromisoformat(b_after["end_time"])
            - datetime.datetime.fromisoformat(b_after["start_time"])) == b_dur

    # resolved conflict disappears + stays gone across a re-login
    assert _day(tc)["conflicts"] == []
    tc2 = _relogin("acc1@example.com")
    d2 = tc2.get(f"/api/schedule/day?date={_now().date().isoformat()}").json()
    assert d2["conflicts"] == []
    assert _event_row(user["id"], b["id"])["start_time"] == a["end_time"]


def test_accept_with_no_safe_reschedule_is_422_and_keep_still_works():
    tc, user = new_authed_client("acc2@example.com")
    d = _now().replace(hour=14, minute=0)
    _mk_event(tc, "A", d, d + datetime.timedelta(hours=1))                     # 14:00-15:00
    b = _mk_event(tc, "B", d + datetime.timedelta(minutes=30),
                  d + datetime.timedelta(minutes=90))                          # 14:30-15:30  (overlaps A only)
    _mk_event(tc, "C", d + datetime.timedelta(minutes=90),
              d + datetime.timedelta(minutes=150))                            # 15:30-16:30  (touches B; blocks B's shift to 15:00-16:00)
    conf, _ = _only_conflict(tc)
    assert conf["proposal"] is None
    fp = conf["fingerprint"]

    r = tc.post(f"/api/schedule/conflicts/{fp}/accept")
    assert r.status_code == 422
    assert _decisions(user["id"]) == []
    assert _day(tc)["unresolved_conflicts"]                                   # still unresolved

    assert tc.post(f"/api/schedule/conflicts/{fp}/keep").status_code == 200
    assert _day(tc)["unresolved_conflicts"] == []


# ===================================================================== #
# C — duplicate / retry
# ===================================================================== #
def test_triple_accept_moves_the_event_exactly_once():
    tc, user = new_authed_client("dup1@example.com")
    a, b, _ = _overlap_pair(tc)
    fp = _only_conflict(tc)[0]["fingerprint"]

    r1 = tc.post(f"/api/schedule/conflicts/{fp}/accept")
    r2 = tc.post(f"/api/schedule/conflicts/{fp}/accept")
    r3 = tc.post(f"/api/schedule/conflicts/{fp}/accept")
    assert [x.status_code for x in (r1, r2, r3)] == [200, 200, 200]
    assert r1.json()["already"] is False
    assert r2.json()["already"] is True and r3.json()["already"] is True
    assert r1.json()["decision_id"] == r2.json()["decision_id"] == r3.json()["decision_id"]

    b_after = _event_row(user["id"], b["id"])
    assert b_after["start_time"] == a["end_time"]                     # moved once, not thrice
    assert len(_decisions(user["id"])) == 1


def test_accept_then_keep_same_conflict_returns_the_established_accept():
    tc, user = new_authed_client("dup2@example.com")
    _overlap_pair(tc)
    fp = _only_conflict(tc)[0]["fingerprint"]
    tc.post(f"/api/schedule/conflicts/{fp}/accept")
    r = tc.post(f"/api/schedule/conflicts/{fp}/keep")           # safe replay, not a second mutation
    assert r.status_code == 200 and r.json()["decision"] == "accepted" and r.json()["already"] is True
    assert len(_decisions(user["id"])) == 1


# ===================================================================== #
# D — stale proposal
# ===================================================================== #
def test_manual_edit_after_load_makes_the_old_proposal_stale():
    tc, user = new_authed_client("stale1@example.com")
    a, b, d = _overlap_pair(tc)
    fp = _only_conflict(tc)[0]["fingerprint"]

    # user edits B through the canonical manual endpoint -> no longer overlaps A
    moved_start = d + datetime.timedelta(hours=3)
    r = tc.patch(f"/api/events/{b['id']}", json={
        "start": _iso(moved_start), "end": _iso(moved_start + datetime.timedelta(hours=1)),
    })
    assert r.status_code == 200

    # accepting the OLD proposal must fail safe and not touch the newer edit
    r = tc.post(f"/api/schedule/conflicts/{fp}/accept")
    assert r.status_code == 409
    assert _event_row(user["id"], b["id"])["start_time"] == moved_start.isoformat()
    assert _decisions(user["id"]) == []

    # reload recomputes truthfully: no conflict now
    assert _day(tc)["conflicts"] == []


def test_keep_on_a_stale_fingerprint_is_409():
    tc, user = new_authed_client("stale2@example.com")
    a, b, d = _overlap_pair(tc)
    fp = _only_conflict(tc)[0]["fingerprint"]
    tc.patch(f"/api/events/{a['id']}", json={"start": _iso(d + datetime.timedelta(minutes=5))})
    r = tc.post(f"/api/schedule/conflicts/{fp}/keep")
    assert r.status_code == 409
    assert _decisions(user["id"]) == []


def test_stale_decision_row_is_marked_and_stops_suppressing():
    tc, user = new_authed_client("stale3@example.com")
    a, b, d = _overlap_pair(tc)
    fp = _only_conflict(tc)[0]["fingerprint"]
    tc.post(f"/api/schedule/conflicts/{fp}/keep")
    assert _day(tc)["unresolved_conflicts"] == []

    # a later relevant edit to A: still overlaps B, but the kept fingerprint no longer matches
    tc.patch(f"/api/events/{a['id']}", json={
        "start": _iso(d + datetime.timedelta(minutes=10)),
        "end": _iso(d + datetime.timedelta(minutes=70)),
    })
    data = _day(tc)
    assert len(data["unresolved_conflicts"]) == 1                 # the (different) overlap is back
    dec = _decisions(user["id"])[0]
    assert dec["stale_at"] is not None


# ===================================================================== #
# E — ownership / IDOR
# ===================================================================== #
def test_user_b_cannot_read_accept_or_keep_user_a_conflict():
    ac, a_user = new_authed_client("own_a@example.com")
    bc, b_user = new_authed_client("own_b@example.com")
    _overlap_pair(ac)
    fp_a = _only_conflict(ac)[0]["fingerprint"]

    # B sees nothing of A's
    assert bc.get(f"/api/schedule/day?date={_now().date().isoformat()}").json()["conflicts"] == []
    # B acting on A's fingerprint -> same shape as an unknown fingerprint
    assert bc.post(f"/api/schedule/conflicts/{fp_a}/accept").status_code == 409
    assert bc.post(f"/api/schedule/conflicts/{fp_a}/keep").status_code == 409
    assert _decisions(b_user["id"]) == []
    assert _decisions(a_user["id"]) == []
    # A still owns an actionable, unresolved conflict
    assert ac.get(f"/api/schedule/day?date={_now().date().isoformat()}").json()["unresolved_conflicts"]


def test_one_users_keep_does_not_resolve_another_users_identical_overlap():
    ac, _ = new_authed_client("iso_a@example.com")
    bc, _ = new_authed_client("iso_b@example.com")
    _overlap_pair(ac)
    _overlap_pair(bc)
    fp_b = _only_conflict(bc)[0]["fingerprint"]
    bc.post(f"/api/schedule/conflicts/{fp_b}/keep")
    assert _day(bc)["unresolved_conflicts"] == []
    assert _day(ac)["unresolved_conflicts"]                      # A untouched


@pytest.mark.parametrize("bad", ["z" * 32, "abc", "", "../../etc", "0" * 31, "0" * 33])
def test_malformed_or_unknown_fingerprint_is_rejected_without_an_oracle(bad):
    tc, user = new_authed_client("badfp@example.com")
    _overlap_pair(tc)
    r_acc = tc.post(f"/api/schedule/conflicts/{bad}/accept")
    r_keep = tc.post(f"/api/schedule/conflicts/{bad}/keep")
    assert r_acc.status_code in (404, 409)
    assert r_keep.status_code in (404, 409)
    assert _decisions(user["id"]) == []


# ===================================================================== #
# F — failure injection
# ===================================================================== #
def test_event_update_failure_leaves_no_decision_and_no_change(monkeypatch):
    tc, user = new_authed_client("fail1@example.com")
    a, b, _ = _overlap_pair(tc)
    fp = _only_conflict(tc)[0]["fingerprint"]
    b_before = _event_row(user["id"], b["id"])

    def boom(*_a, **_kw):
        raise sqlite3.OperationalError("disk gone")
    monkeypatch.setattr(main, "_exec_event_update", boom)

    r = tc.post(f"/api/schedule/conflicts/{fp}/accept")
    assert r.status_code == 500
    monkeypatch.undo()
    assert _event_row(user["id"], b["id"]) == b_before
    assert _decisions(user["id"]) == []
    # a later real Accept still works
    assert tc.post(f"/api/schedule/conflicts/{fp}/accept").status_code == 200
    assert _event_row(user["id"], b["id"])["start_time"] == a["end_time"]


def test_decision_write_failure_rolls_back_the_event_move(monkeypatch):
    tc, user = new_authed_client("fail2@example.com")
    a, b, _ = _overlap_pair(tc)
    fp = _only_conflict(tc)[0]["fingerprint"]
    b_before = _event_row(user["id"], b["id"])

    calls = {"n": 0}
    real_now = main._sc_now

    def flaky_now():
        calls["n"] += 1
        if calls["n"] == 1:                       # first use is inside the accept txn
            raise RuntimeError("decision-side failure")
        return real_now()
    monkeypatch.setattr(main, "_sc_now", flaky_now)

    r = tc.post(f"/api/schedule/conflicts/{fp}/accept")
    assert r.status_code == 500
    monkeypatch.undo()
    assert _event_row(user["id"], b["id"]) == b_before            # move rolled back with the decision
    assert _decisions(user["id"]) == []


def test_deleted_affected_event_is_a_safe_stale_result():
    tc, user = new_authed_client("fail3@example.com")
    a, b, _ = _overlap_pair(tc)
    fp = _only_conflict(tc)[0]["fingerprint"]
    tc.delete(f"/api/events/{b['id']}")
    assert tc.post(f"/api/schedule/conflicts/{fp}/accept").status_code == 409
    assert tc.post(f"/api/schedule/conflicts/{fp}/keep").status_code == 409
    assert _decisions(user["id"]) == []


# ===================================================================== #
# G — existing unaffected flows
# ===================================================================== #
def test_manual_event_and_task_flows_still_work():
    tc, _ = new_authed_client("unaff1@example.com")
    ev = _mk_event(tc, "Plain", _now().replace(hour=9), _now().replace(hour=10))
    assert tc.patch(f"/api/events/{ev['id']}", json={"title": "Plain v2"}).status_code == 200
    tk = tc.post("/api/tasks", json={"title": "Do it"}).json()["task"]["id"]
    assert tc.post(f"/api/tasks/{tk}/complete").status_code == 200
    assert tc.post(f"/api/tasks/{tk}/uncomplete").status_code == 200
    assert tc.delete(f"/api/events/{ev['id']}").status_code == 200


def test_mo_confirmed_event_still_appears_in_plan(monkeypatch):
    tc, _ = new_authed_client("unaff2@example.com")
    from conftest import mo_run
    import main as m
    start = _iso(_now().replace(hour=15) + datetime.timedelta(days=1))

    def fake_interpret(_text, context_date=None, context_entity=None, user_interests=None):
        return m.MoInterpretation(mode="single_operation", operations=[
            m.MoOperation(intent=m.IntentEnum("CREATE_EVENT"),
                         parameters={"title": "Mo Event", "start": start, "end": None,
                                     "location": None, "all_day": False}),
        ])
    monkeypatch.setattr(m.AIService, "interpret", staticmethod(fake_interpret))
    _p, conf = mo_run(tc, "add mo event tomorrow 3pm")
    assert conf.status_code == 200 and conf.json()["kind"] == "executed"
    assert "Mo Event" in [e["title"] for e in tc.get("/api/schedule").json()["all_events"]]


def test_resolve_conflict_legacy_route_stays_removed():
    tc, _ = new_authed_client("unaff3@example.com")
    assert tc.post("/api/schedule/resolve-conflict", json={"x": 1}).status_code in (404, 405)


def test_schedule_read_shape_is_still_backward_compatible():
    tc, _ = new_authed_client("unaff4@example.com")
    _overlap_pair(tc)
    data = tc.get("/api/schedule").json()
    for k in ("range", "events", "all_events", "tasks", "conflicts", "recommendations", "timezone"):
        assert k in data
    c = data["conflicts"][0]
    assert set(c["event_ids"]) and "a" in c and "b" in c        # legacy keys intact
