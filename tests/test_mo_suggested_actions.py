"""B8 — Mo mutating SUGGESTED ACTIONS.

After a real GET_SCHEDULE_SUMMARY answer, Mo may surface a tiny curated set of
mutating recommendations:

  A. "Block <N> min for <real task>"  -> schedules the task WORK BLOCK only
     (planned_at + scheduled_end) via the canonical B7/B7.1 task-update path.
     due_date never changes; no event is created/moved.
  B. "Mark <real task> complete"      -> the same canonical completion path as
     the manual Complete action.

Nothing mutates on display or on click; only Confirm mutates. Each suggestion is
server-computed, owner-bound and carried only as an opaque HMAC-signed token.

DETERMINISTIC — temp DB via conftest; the AI boundary is mock_ai; the reminder
scheduler is disabled (conftest sets COREOS_SCHEDULER_ENABLED=0).
"""
import datetime as dt
import os
import subprocess
import sys

import pytest

import main
import db as db_layer
import mo_proposals as mo
import security_config as sec
import task_conflict as tec
from conftest import new_authed_client, mo_confirm, mo_cancel, mo_pending

MYT = dt.timezone(dt.timedelta(hours=8))


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _now():
    return dt.datetime.now(MYT).replace(minute=0, second=0, microsecond=0)


def _mk_task(tc, title, *, planned=None, end=None, due=None):
    body = {"title": title}
    if planned is not None:
        body["planned_at"] = planned.isoformat()
    if end is not None:
        body["scheduled_end"] = end.isoformat()
    if due is not None:
        body["due_date"] = due if isinstance(due, str) else due.isoformat()
    r = tc.post("/api/tasks", json=body)
    assert r.status_code == 201, r.text
    return r.json()["task"]


def _mk_event(tc, title, start, end):
    r = tc.post("/api/events", json={"title": title, "start": start.isoformat(), "end": end.isoformat()})
    assert r.status_code == 201, r.text
    return r.json()["event"]


def _ask_schedule(tc, mock_ai, token=None):
    mock_ai("GET_SCHEDULE_SUMMARY")
    body = {"user_input": "how does my day look"}
    if token:
        body["client_token"] = token
    r = tc.post("/api/process-input", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _prepare(tc, token):
    return tc.post(f"/api/process-input/suggested/{token}")


def _task_row(uid, tid):
    c = db_layer.get_connection()
    try:
        r = c.execute("SELECT title, is_completed, planned_at, scheduled_end, due_date, completed_at "
                      "FROM tasks WHERE id=? AND user_id=?", (tid, uid)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


def _count(table, uid=None):
    c = db_layer.get_connection()
    try:
        if uid is None:
            return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return c.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id=?", (uid,)).fetchone()[0]
    finally:
        c.close()


def _uid(tc):
    return tc.get("/api/auth/me").json()["user"]["id"]


def _relogin(email, password="password123"):
    from fastapi.testclient import TestClient
    tc = TestClient(main.app)
    assert tc.post("/api/auth/login", json={"email": email, "password": password}).status_code == 200
    return tc


def _sug(answer, kind):
    return next((s for s in answer.get("mutating_suggestions", []) if s["kind"] == kind), None)


# --------------------------------------------------------------------------- #
# 1-2. a real schedule-summary answer returns a real, owner-derived suggestion
# --------------------------------------------------------------------------- #
def test_1_2_answer_returns_a_real_block_suggestion_titled_from_the_users_task(alice, mock_ai):
    tc, _ = alice
    n = _now()
    t = _mk_task(tc, "Write the lit review", due=(n + dt.timedelta(days=3)))
    ans = _ask_schedule(tc, mock_ai)

    assert ans["kind"] == "answer"
    s = _sug(ans, "schedule_task_block")
    assert s is not None, ans
    assert s["label"] == "Block 45 min for Write the lit review"      # (2) real task title
    assert s["token"] and "." in s["token"]                            # opaque signed blob
    assert s["preview"]["title"] == "Write the lit review"


def test_1_complete_suggestion_only_appears_for_a_genuinely_overdue_task(alice, mock_ai):
    tc, _ = alice
    n = _now()
    _mk_task(tc, "File the tax form", due=(n - dt.timedelta(days=1)))   # overdue
    ans = _ask_schedule(tc, mock_ai)
    s = _sug(ans, "complete_task")
    assert s is not None and s["label"] == "Mark File the tax form complete"


# --------------------------------------------------------------------------- #
# 3. another user's task is never suggested / never reachable
# --------------------------------------------------------------------------- #
def test_3_cross_user_task_is_never_suggested_or_prepared(alice, bob, mock_ai):
    tc_a, _ = alice
    tc_b, _ = bob
    n = _now()
    t_b = _mk_task(tc_b, "Bob private task", due=(n + dt.timedelta(days=2)))

    ans_a = _ask_schedule(tc_a, mock_ai)
    for s in ans_a.get("mutating_suggestions", []):
        assert "Bob private task" not in s["label"]

    # forge a token for bob's task signed with the real secret, try as alice
    tok, _ = mo.sign_suggestion(kind="complete_task", user_id=_uid(tc_b), task_id=t_b["id"],
                                now_iso=main._mo_now(), secret=sec.SESSION_SECRET)
    r = _prepare(tc_a, tok)
    assert r.status_code == 409
    assert _count("mo_proposals", _uid(tc_a)) == 0


# --------------------------------------------------------------------------- #
# 4-5. displaying / clicking a suggestion mutates NOTHING
# --------------------------------------------------------------------------- #
def test_4_displaying_a_suggestion_mutates_nothing(alice, mock_ai):
    tc, _ = alice
    n = _now()
    t = _mk_task(tc, "Draft slides", due=(n + dt.timedelta(days=3)))
    _ask_schedule(tc, mock_ai)
    row = _task_row(_uid(tc), t["id"])
    assert row["is_completed"] == 0 and row["planned_at"] is None and row["scheduled_end"] is None
    assert _count("mo_proposals") == 0


def test_5_clicking_a_suggestion_creates_only_a_proposal_no_domain_mutation(alice, mock_ai):
    tc, _ = alice
    n = _now()
    t = _mk_task(tc, "Draft slides", due=(n + dt.timedelta(days=3)))
    s = _sug(_ask_schedule(tc, mock_ai), "schedule_task_block")

    r = _prepare(tc, s["token"])
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["kind"] == "proposal_required"
    assert j["proposal"]["action_type"] == "schedule_task_block"
    assert j["proposal"]["status"] == "pending"

    row = _task_row(_uid(tc), t["id"])
    assert row["is_completed"] == 0 and row["planned_at"] is None      # still not scheduled
    assert _count("mo_proposals", _uid(tc)) == 1
    assert _count("schedule_events", _uid(tc)) == 0                    # no event was created


# --------------------------------------------------------------------------- #
# 6-8. the block suggestion uses a valid B7/B7.1 server slot
# --------------------------------------------------------------------------- #
def test_6_7_8_block_slot_is_reasonable_hours_before_due_and_collision_free(alice, mock_ai):
    tc, uid = alice[0], _uid(alice[0])
    n = _now()
    due = n + dt.timedelta(days=4)
    # occupy things the candidate must dodge
    ev = _mk_event(tc, "Standup", n + dt.timedelta(days=1, hours=1), n + dt.timedelta(days=1, hours=2))
    other = _mk_task(tc, "Other scheduled work",
                     planned=n + dt.timedelta(days=1, hours=3),
                     end=n + dt.timedelta(days=1, hours=4),
                     due=due)
    t = _mk_task(tc, "Unscheduled report", due=due)

    s = _sug(_ask_schedule(tc, mock_ai), "schedule_task_block")
    cand_s = dt.datetime.fromisoformat(s["preview"]["start"])
    cand_e = dt.datetime.fromisoformat(s["preview"]["end"])

    assert (cand_e - cand_s) == dt.timedelta(minutes=45)              # (duration preserved)
    assert cand_e <= dt.datetime.fromisoformat(t["due_date"])        # (7) before the deadline
    lm = cand_s.astimezone(MYT).hour * 60 + cand_s.astimezone(MYT).minute
    em = cand_e.astimezone(MYT).hour * 60 + cand_e.astimezone(MYT).minute
    assert tec.DAY_START_MIN <= lm and em <= tec.DAY_END_MIN         # (6) reasonable hours
    # (8) avoids the event + the other task window
    for a, b in [(ev["start_time"], ev["end_time"]),
                 (other["planned_at"], other["scheduled_end"])]:
        assert not (cand_s < dt.datetime.fromisoformat(b) and dt.datetime.fromisoformat(a) < cand_e)


# --------------------------------------------------------------------------- #
# 9-10. Confirm schedules ONLY the work block; due_date is untouched
# --------------------------------------------------------------------------- #
def test_9_10_confirm_schedules_only_the_block_and_never_moves_the_deadline(alice, mock_ai):
    tc, _ = alice
    uid = _uid(tc)
    n = _now()
    t = _mk_task(tc, "Prep interview", due=(n + dt.timedelta(days=3)))
    due_before = _task_row(uid, t["id"])["due_date"]
    ev_before = _count("schedule_events", uid)

    s = _sug(_ask_schedule(tc, mock_ai), "schedule_task_block")
    pid = _prepare(tc, s["token"]).json()["proposal"]["proposal_id"]
    c = mo_confirm(tc, pid).json()
    assert c["kind"] == "executed"

    row = _task_row(uid, t["id"])
    assert row["planned_at"] == s["preview"]["start"]
    assert row["scheduled_end"] == s["preview"]["end"]
    assert row["due_date"] == due_before                              # (10) deadline unchanged
    assert row["is_completed"] == 0
    assert _count("schedule_events", uid) == ev_before                # no event created/moved


# --------------------------------------------------------------------------- #
# 11-12. Complete suggestion targets exactly one real incomplete task
# --------------------------------------------------------------------------- #
def test_11_12_complete_suggestion_confirms_exactly_that_task(alice, mock_ai):
    tc, _ = alice
    uid = _uid(tc)
    n = _now()
    target = _mk_task(tc, "Send the invoice", due=(n - dt.timedelta(days=2)))
    bystander = _mk_task(tc, "Unrelated", due=(n + dt.timedelta(days=5)))

    s = _sug(_ask_schedule(tc, mock_ai), "complete_task")
    assert s["label"] == "Mark Send the invoice complete"
    j = _prepare(tc, s["token"]).json()
    assert j["proposal"]["action_type"] == "complete_task"
    assert _task_row(uid, target["id"])["is_completed"] == 0          # nothing before Confirm

    assert mo_confirm(tc, j["proposal"]["proposal_id"]).json()["kind"] == "executed"
    assert _task_row(uid, target["id"])["is_completed"] == 1          # (12) exactly that task
    assert _task_row(uid, bystander["id"])["is_completed"] == 0


# --------------------------------------------------------------------------- #
# 13-14. idempotency: one active proposal per click; Confirm executes once
# --------------------------------------------------------------------------- #
def test_13_repeated_clicks_of_one_suggestion_yield_one_proposal(alice, mock_ai):
    tc, _ = alice
    n = _now()
    _mk_task(tc, "Repeatable", due=(n + dt.timedelta(days=3)))
    s = _sug(_ask_schedule(tc, mock_ai), "schedule_task_block")

    ids = {_prepare(tc, s["token"]).json()["proposal"]["proposal_id"] for _ in range(4)}
    assert len(ids) == 1
    assert _count("mo_proposals", _uid(tc)) == 1


def test_14_repeated_confirm_executes_once(alice, mock_ai):
    tc, _ = alice
    uid = _uid(tc)
    n = _now()
    t = _mk_task(tc, "Once only", due=(n - dt.timedelta(days=1)))
    s = _sug(_ask_schedule(tc, mock_ai), "complete_task")
    pid = _prepare(tc, s["token"]).json()["proposal"]["proposal_id"]

    for _ in range(4):
        assert mo_confirm(tc, pid).json()["kind"] == "executed"
    assert _task_row(uid, t["id"])["is_completed"] == 1
    assert _count("mo_proposals", uid) == 1
    assert mo_pending(tc).json()["proposal"]["status"] == "executed"


# --------------------------------------------------------------------------- #
# 15-16. Cancel / expiry change nothing
# --------------------------------------------------------------------------- #
def test_15_cancel_produces_zero_mutation(alice, mock_ai):
    tc, _ = alice
    uid = _uid(tc)
    n = _now()
    t = _mk_task(tc, "Cancel me", due=(n + dt.timedelta(days=3)))
    s = _sug(_ask_schedule(tc, mock_ai), "schedule_task_block")
    pid = _prepare(tc, s["token"]).json()["proposal"]["proposal_id"]

    assert mo_cancel(tc, pid).json()["kind"] == "cancelled"
    row = _task_row(uid, t["id"])
    assert row["planned_at"] is None and row["is_completed"] == 0
    assert mo_confirm(tc, pid).status_code == 409       # a cancelled proposal can never execute


def test_16_expired_suggestion_token_is_rejected_with_no_mutation(alice, mock_ai):
    tc, _ = alice
    uid = _uid(tc)
    n = _now()
    t = _mk_task(tc, "Too late", due=(n + dt.timedelta(days=3)))
    # forge a token that was already expired when minted
    tok, _ = mo.sign_suggestion(kind="schedule_task_block", user_id=uid, task_id=t["id"],
                                now_iso="2020-01-01T00:00:00+08:00", secret=sec.SESSION_SECRET,
                                block_minutes=45)
    r = _prepare(tc, tok)
    assert r.status_code == 409
    assert _count("mo_proposals", uid) == 0
    assert _task_row(uid, t["id"])["planned_at"] is None


def test_16b_expired_proposal_cannot_execute(alice, mock_ai):
    tc, _ = alice
    uid = _uid(tc)
    n = _now()
    _mk_task(tc, "Stale prop", due=(n - dt.timedelta(days=1)))
    s = _sug(_ask_schedule(tc, mock_ai), "complete_task")
    pid = _prepare(tc, s["token"]).json()["proposal"]["proposal_id"]
    with db_layer.transaction() as c:
        c.execute("UPDATE mo_proposals SET expires_at=? WHERE id=?",
                  ("2020-01-01T00:00:00+08:00", pid))
    r = mo_confirm(tc, pid)
    assert r.status_code == 409


# --------------------------------------------------------------------------- #
# 17. manual edit / completion / deletion before Confirm -> stale rejection
# --------------------------------------------------------------------------- #
def test_17a_manual_completion_before_confirm_makes_a_block_proposal_stale(alice, mock_ai):
    tc, _ = alice
    uid = _uid(tc)
    n = _now()
    t = _mk_task(tc, "Beat me to it", due=(n + dt.timedelta(days=3)))
    s = _sug(_ask_schedule(tc, mock_ai), "schedule_task_block")
    pid = _prepare(tc, s["token"]).json()["proposal"]["proposal_id"]

    assert tc.post(f"/api/tasks/{t['id']}/complete").status_code == 200
    r = mo_confirm(tc, pid).json()
    assert r["kind"] == "stale"
    row = _task_row(uid, t["id"])
    assert row["is_completed"] == 1 and row["planned_at"] is None      # manual state preserved


def test_17b_manual_edit_before_confirm_makes_a_block_proposal_stale(alice, mock_ai):
    tc, _ = alice
    uid = _uid(tc)
    n = _now()
    t = _mk_task(tc, "Edited under me", due=(n + dt.timedelta(days=4)))
    s = _sug(_ask_schedule(tc, mock_ai), "schedule_task_block")
    pid = _prepare(tc, s["token"]).json()["proposal"]["proposal_id"]

    # user manually gives the task its own window
    man_s = (n + dt.timedelta(days=2, hours=10)).isoformat()
    man_e = (n + dt.timedelta(days=2, hours=11)).isoformat()
    assert tc.patch(f"/api/tasks/{t['id']}", json={"planned_at": man_s, "scheduled_end": man_e}).status_code == 200

    assert mo_confirm(tc, pid).json()["kind"] == "stale"
    row = _task_row(uid, t["id"])
    assert row["planned_at"] == man_s and row["scheduled_end"] == man_e   # manual edit kept


def test_17c_deletion_before_confirm_makes_the_proposal_stale(alice, mock_ai):
    tc, _ = alice
    uid = _uid(tc)
    n = _now()
    t = _mk_task(tc, "Gone", due=(n - dt.timedelta(days=1)))
    s = _sug(_ask_schedule(tc, mock_ai), "complete_task")
    pid = _prepare(tc, s["token"]).json()["proposal"]["proposal_id"]

    assert tc.delete(f"/api/tasks/{t['id']}").status_code == 200
    assert mo_confirm(tc, pid).json()["kind"] == "stale"
    assert _task_row(uid, t["id"]) is None


# --------------------------------------------------------------------------- #
# 18-19. pending recovery + persistence across logout/login
# --------------------------------------------------------------------------- #
def test_18_pending_proposal_recovers_after_refresh(alice, mock_ai):
    tc, _ = alice
    n = _now()
    _mk_task(tc, "Recover me", due=(n + dt.timedelta(days=3)))
    s = _sug(_ask_schedule(tc, mock_ai), "schedule_task_block")
    pid = _prepare(tc, s["token"]).json()["proposal"]["proposal_id"]

    p = mo_pending(tc).json()["proposal"]
    assert p and p["proposal_id"] == pid and p["status"] == "pending"
    assert p["action_type"] == "schedule_task_block"


def test_19_scheduled_and_completed_state_persist_across_logout_login(mock_ai):
    tc, _ = new_authed_client("b8persist@example.com")
    uid = _uid(tc)
    n = _now()
    blk = _mk_task(tc, "Persisted block", due=(n + dt.timedelta(days=3)))
    done = _mk_task(tc, "Persisted done", due=(n - dt.timedelta(days=1)))

    ans = _ask_schedule(tc, mock_ai)
    sb = _sug(ans, "schedule_task_block")
    pid_b = _prepare(tc, sb["token"]).json()["proposal"]["proposal_id"]
    assert mo_confirm(tc, pid_b).json()["kind"] == "executed"

    ans2 = _ask_schedule(tc, mock_ai)
    sc = _sug(ans2, "complete_task")
    pid_c = _prepare(tc, sc["token"]).json()["proposal"]["proposal_id"]
    assert mo_confirm(tc, pid_c).json()["kind"] == "executed"

    tc.post("/api/auth/logout")
    tc2 = _relogin("b8persist@example.com")
    rb = _task_row(uid, blk["id"])
    rc = _task_row(uid, done["id"])
    assert rb["planned_at"] == sb["preview"]["start"] and rb["scheduled_end"] == sb["preview"]["end"]
    assert rc["is_completed"] == 1


# --------------------------------------------------------------------------- #
# 20. cross-user proposal access is rejected
# --------------------------------------------------------------------------- #
def test_20_cross_user_cannot_touch_another_users_suggestion_proposal(alice, bob, mock_ai):
    tc_a, _ = alice
    tc_b, _ = bob
    n = _now()
    _mk_task(tc_a, "Alice only", due=(n - dt.timedelta(days=1)))
    s = _sug(_ask_schedule(tc_a, mock_ai), "complete_task")
    pid = _prepare(tc_a, s["token"]).json()["proposal"]["proposal_id"]

    assert mo_confirm(tc_b, pid).status_code == 404
    assert mo_cancel(tc_b, pid).status_code == 404
    assert tc_b.get("/api/process-input/pending").json()["proposal"] is None


# --------------------------------------------------------------------------- #
# 21. an answer with nothing safely applicable shows no fake mutation
# --------------------------------------------------------------------------- #
def test_21_no_applicable_action_means_no_mutating_suggestion(alice, mock_ai):
    tc, _ = alice
    # user with NO tasks
    ans = _ask_schedule(tc, mock_ai)
    assert ans.get("mutating_suggestions", []) == []

    # a task that already has a window and is not overdue -> neither A nor B
    n = _now()
    _mk_task(tc, "Already planned",
             planned=n + dt.timedelta(days=1, hours=9),
             end=n + dt.timedelta(days=1, hours=10),
             due=(n + dt.timedelta(days=5)))
    ans2 = _ask_schedule(tc, mock_ai)
    assert ans2.get("mutating_suggestions", []) == []


def test_21b_error_answer_never_carries_a_mutating_suggestion(alice, mock_ai, monkeypatch):
    tc, _ = alice
    n = _now()
    _mk_task(tc, "Would-be", due=(n - dt.timedelta(days=1)))

    from main import AppServices
    monkeypatch.setattr(AppServices, "get_schedule_summary",
                        staticmethod(lambda uid, date=None: main.ActionOutcome(
                            status="DATABASE_ERROR", message="I couldn't read your schedule.")))
    ans = _ask_schedule(tc, mock_ai)
    assert ans["kind"] == "answer"
    assert ans.get("mutating_suggestions", []) == []


# --------------------------------------------------------------------------- #
# 22-23. navigation suggestions + existing B2 direct flow unchanged
# --------------------------------------------------------------------------- #
def test_22_navigation_suggestions_still_ride_along_with_the_answer(alice, mock_ai):
    tc, _ = alice
    n = _now()
    _mk_task(tc, "Nav check", due=(n + dt.timedelta(days=3)))
    ans = _ask_schedule(tc, mock_ai)
    assert [x["key"] for x in ans["suggested_actions"]] == ["open_plan"]
    assert ans["suggested_actions"][0]["route"] == "/schedule"


def test_23_existing_b2_create_transaction_flow_is_unchanged(alice, mock_ai):
    tc, _ = alice
    mock_ai("CREATE_TRANSACTION", parameters={
        "amount": 12, "type": "expense", "category": "Food",
        "description": "lunch", "date": "2026-09-05T12:00:00+08:00"})
    pr = tc.post("/api/process-input", json={"user_input": "spent RM12 on lunch", "client_token": "b8-b2"})
    j = pr.json()
    assert j["kind"] == "proposal_required" and j["proposal"]["action_type"] == "create_transaction"
    assert "mutating_suggestions" not in j                            # only answers carry them
    assert mo_confirm(tc, j["proposal"]["proposal_id"]).json()["kind"] == "executed"
    assert _count("transactions", _uid(tc)) == 1


# --------------------------------------------------------------------------- #
# 25. exactly one alembic head (0018_mo_context + 0019_mo_context_entity added
#     Mo's short-term conversational context, 0020_user_interests added Who Am
#     I's persistent per-user interests — see alembic/versions/ — but must
#     never leave a branch / a second head behind)
# --------------------------------------------------------------------------- #
def test_25_single_alembic_head_still_0020(tmp_path):
    env = {**os.environ, "COREOS_DB_PATH": str(tmp_path / "heads.db")}
    out = subprocess.run([sys.executable, "-m", "alembic", "heads"],
                         capture_output=True, text=True, env=env,
                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    assert out.returncode == 0, out.stderr
    assert "0020_user_interests" in out.stdout
    # exactly one head line
    assert len([ln for ln in out.stdout.splitlines() if "(head)" in ln]) == 1


# ========================================================================== #
# B8.1 — DISTINCT STALE PROPOSAL SEMANTICS
#
# `stale` (task changed / completed / deleted / slot taken / past deadline) is a
# terminal status DISTINCT from `cancelled`. Confirm on a stale proposal returns
# kind:"stale" every time (HTTP 200), never executes, never builds a replacement,
# and never reports "you cancelled this". A recently-stale proposal is surfaced
# by GET /pending only as a bounded truthful acknowledgement, never actionable.
# ========================================================================== #
_CANCEL_WORDS = ("cancel", "cancelled", "you cancelled")


def _prop_status(uid, pid):
    c = db_layer.get_connection()
    try:
        r = c.execute("SELECT status, failure_reason FROM mo_proposals WHERE id=? AND user_id=?",
                      (pid, uid)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


def _no_cancel_language(*blobs):
    hay = " ".join(str(b or "").lower() for b in blobs)
    return not any(w in hay for w in _CANCEL_WORDS)


def _prep_block(tc, mock_ai, *, title, due_days=3):
    n = _now()
    t = _mk_task(tc, title, due=(n + dt.timedelta(days=due_days)))
    s = _sug(_ask_schedule(tc, mock_ai), "schedule_task_block")
    assert s is not None
    pid = _prepare(tc, s["token"]).json()["proposal"]["proposal_id"]
    return t, s, pid


# --- explicit user Cancel stays `cancelled` and never reads as stale --------- #
def test_b81_explicit_cancel_stays_cancelled(alice, mock_ai):
    tc, _ = alice
    uid = _uid(tc)
    t, s, pid = _prep_block(tc, mock_ai, title="Cancel me cleanly")

    j = mo_cancel(tc, pid).json()
    assert j["kind"] == "cancelled"
    assert _prop_status(uid, pid)["status"] == "cancelled"
    # a later Confirm still reports the cancelled meaning (409), not stale
    r = mo_confirm(tc, pid)
    assert r.status_code == 409
    assert "stale" not in r.text.lower() and "no longer current" not in r.text.lower()
    assert _task_row(uid, t["id"])["planned_at"] is None


def json_dumps(o):
    import json as _j
    return _j.dumps(o)


def test_b81_cancel_never_reports_stale_even_for_a_valid_proposal(alice, mock_ai):
    tc, _ = alice
    uid = _uid(tc)
    _, _, pid = _prep_block(tc, mock_ai, title="Still valid")
    j = mo_cancel(tc, pid).json()
    assert j["kind"] == "cancelled"                         # an explicit cancel IS cancelled
    blob = json_dumps(j).lower()
    assert "stale" not in blob and "no longer current" not in blob
    assert _prop_status(uid, pid)["status"] == "cancelled"


# --- each stale trigger -> status 'stale', kind 'stale', truthful copy ------- #
def _assert_stale(resp_json, uid, pid):
    assert resp_json["kind"] == "stale", resp_json
    assert resp_json.get("retryable") is False
    row = _prop_status(uid, pid)
    assert row["status"] == "stale", row                    # NOT 'cancelled'
    assert "no longer current" in resp_json["message"].lower()
    assert "nothing was changed" in resp_json["message"].lower()
    assert _no_cancel_language(resp_json["message"], row["failure_reason"])


def test_b81_manual_edit_before_confirm_is_stale(alice, mock_ai):
    tc, _ = alice
    uid = _uid(tc)
    n = _now()
    t, s, pid = _prep_block(tc, mock_ai, title="Edit under me", due_days=5)
    man_s = (n + dt.timedelta(days=2, hours=10)).isoformat()
    man_e = (n + dt.timedelta(days=2, hours=11)).isoformat()
    assert tc.patch(f"/api/tasks/{t['id']}", json={"planned_at": man_s, "scheduled_end": man_e}).status_code == 200
    _assert_stale(mo_confirm(tc, pid).json(), uid, pid)
    row = _task_row(uid, t["id"])
    assert row["planned_at"] == man_s and row["scheduled_end"] == man_e   # manual edit preserved


def test_b81_manual_completion_before_confirm_is_stale(alice, mock_ai):
    tc, _ = alice
    uid = _uid(tc)
    t, s, pid = _prep_block(tc, mock_ai, title="Beat me to done")
    assert tc.post(f"/api/tasks/{t['id']}/complete").status_code == 200
    _assert_stale(mo_confirm(tc, pid).json(), uid, pid)
    row = _task_row(uid, t["id"])
    assert row["is_completed"] == 1 and row["planned_at"] is None


def test_b81_deletion_before_confirm_is_stale(alice, mock_ai):
    tc, _ = alice
    uid = _uid(tc)
    n = _now()
    over = _mk_task(tc, "Delete me", due=(n - dt.timedelta(days=1)))
    s = _sug(_ask_schedule(tc, mock_ai), "complete_task")
    pid = _prepare(tc, s["token"]).json()["proposal"]["proposal_id"]
    assert tc.delete(f"/api/tasks/{over['id']}").status_code == 200
    _assert_stale(mo_confirm(tc, pid).json(), uid, pid)
    assert _task_row(uid, over["id"]) is None


def test_b81_occupied_slot_before_confirm_is_stale(alice, mock_ai):
    tc, _ = alice
    uid = _uid(tc)
    t, s, pid = _prep_block(tc, mock_ai, title="Slot stealer", due_days=6)
    # drop an event exactly onto the proposed candidate window
    cs, ce = s["preview"]["start"], s["preview"]["end"]
    assert tc.post("/api/events", json={"title": "Grabbed it", "start": cs, "end": ce}).status_code == 201
    _assert_stale(mo_confirm(tc, pid).json(), uid, pid)
    row = _task_row(uid, t["id"])
    assert row["planned_at"] is None                         # nothing scheduled


# --- first stale Confirm mutates nothing ------------------------------------ #
def test_b81_first_stale_confirm_mutates_nothing(alice, mock_ai):
    tc, _ = alice
    uid = _uid(tc)
    t, s, pid = _prep_block(tc, mock_ai, title="No mutation on stale")
    ev_before = _count("schedule_events", uid)
    assert tc.post(f"/api/tasks/{t['id']}/complete").status_code == 200
    _assert_stale(mo_confirm(tc, pid).json(), uid, pid)
    assert _count("schedule_events", uid) == ev_before
    row = _task_row(uid, t["id"])
    assert row["planned_at"] is None and row["scheduled_end"] is None


# --- repeated stale Confirm keeps reporting stale, never cancelled --------- #
def test_b81_repeated_stale_confirm_stays_stale_and_never_falls_through(alice, mock_ai):
    tc, _ = alice
    uid = _uid(tc)
    t, s, pid = _prep_block(tc, mock_ai, title="Repeat stale")
    assert tc.delete(f"/api/tasks/{t['id']}").status_code == 200

    seen = []
    for _ in range(5):
        r = mo_confirm(tc, pid)
        assert r.status_code == 200                          # not a 409 / not a network error
        j = r.json()
        seen.append(j)
        assert j["kind"] == "stale"
        assert _prop_status(uid, pid)["status"] == "stale"
        assert _no_cancel_language(j["message"])
    # no execution ever happened, and no replacement proposal was created
    assert _count("mo_proposals", uid) == 1
    assert _prop_status(uid, pid)["status"] == "stale"


def test_b81_stale_never_reports_user_cancellation(alice, mock_ai):
    tc, _ = alice
    uid = _uid(tc)
    t, s, pid = _prep_block(tc, mock_ai, title="Draft the memo")   # no 'cancel' in the title
    assert tc.post(f"/api/tasks/{t['id']}/complete").status_code == 200
    for _ in range(3):
        j = mo_confirm(tc, pid).json()
        assert _no_cancel_language(j["message"], j.get("proposal", {}).get("failure_reason"))
        assert "cancel" not in json_dumps(j).lower()


# --- pending / recovery -------------------------------------------------------- #
def test_b81_stale_is_not_an_actionable_pending_proposal(alice, mock_ai):
    tc, _ = alice
    uid = _uid(tc)
    t, s, pid = _prep_block(tc, mock_ai, title="Recover stale ack")
    assert tc.delete(f"/api/tasks/{t['id']}").status_code == 200
    assert mo_confirm(tc, pid).json()["kind"] == "stale"

    p = mo_pending(tc).json()["proposal"]
    assert p is not None                                     # the truthful ack is restorable
    assert p["proposal_id"] == pid
    assert p["status"] == "stale"                            # NOT pending / executing
    assert p["status"] not in ("pending", "executing")
    # confirming the "restored" thing still just reports stale — never actionable
    assert mo_confirm(tc, pid).json()["kind"] == "stale"


def test_b81_stale_ack_disappears_after_its_bounded_window(alice, mock_ai, monkeypatch):
    tc, _ = alice
    uid = _uid(tc)
    t, s, pid = _prep_block(tc, mock_ai, title="Bounded stale window")
    assert tc.delete(f"/api/tasks/{t['id']}").status_code == 200
    assert mo_confirm(tc, pid).json()["kind"] == "stale"
    assert mo_pending(tc).json()["proposal"] is not None     # inside the window

    # push the stale instant outside STALE_VISIBLE_SECONDS
    with db_layer.transaction() as c:
        c.execute("UPDATE mo_proposals SET confirmed_at=? WHERE id=?",
                  ("2020-01-01T00:00:00+08:00", pid))
    assert mo_pending(tc).json()["proposal"] is None         # bounded policy: it disappears
    # …but the row itself is still truthfully `stale`, not `cancelled`
    assert _prop_status(uid, pid)["status"] == "stale"


# --- regressions ----------------------------------------------------------------#
def test_b81_b2_create_flow_cancel_and_expiry_unchanged(alice, mock_ai):
    tc, _ = alice
    mock_ai("CREATE_TRANSACTION", parameters={
        "amount": 9, "type": "expense", "category": "Food",
        "description": "snack", "date": "2026-09-05T10:00:00+08:00"})
    pid = tc.post("/api/process-input",
                  json={"user_input": "spent RM9 on a snack", "client_token": "b81-b2c"}
                  ).json()["proposal"]["proposal_id"]
    assert mo_cancel(tc, pid).json()["kind"] == "cancelled"
    r = mo_confirm(tc, pid)
    assert r.status_code == 409 and "cancel" in r.text.lower()      # B2 wording intact

    mock_ai("CREATE_TRANSACTION", parameters={
        "amount": 4, "type": "expense", "category": "Food",
        "description": "gum", "date": "2026-09-05T10:00:00+08:00"})
    pid2 = tc.post("/api/process-input",
                   json={"user_input": "spent RM4 on gum", "client_token": "b81-b2e"}
                   ).json()["proposal"]["proposal_id"]
    with db_layer.transaction() as c:
        c.execute("UPDATE mo_proposals SET expires_at=? WHERE id=?", ("2020-01-01T00:00:00+08:00", pid2))
    r2 = mo_confirm(tc, pid2)
    assert r2.status_code == 409 and "expired" in r2.text.lower()


def test_b81_valid_block_still_executes_exactly_once(alice, mock_ai):
    tc, _ = alice
    uid = _uid(tc)
    t, s, pid = _prep_block(tc, mock_ai, title="Exactly once still")
    for _ in range(4):
        assert mo_confirm(tc, pid).json()["kind"] == "executed"
    assert _prop_status(uid, pid)["status"] == "executed"
    row = _task_row(uid, t["id"])
    assert row["planned_at"] == s["preview"]["start"] and row["scheduled_end"] == s["preview"]["end"]
    assert _count("schedule_events", uid) == 0


def test_b81_cross_user_stale_proposal_is_not_reachable(alice, bob, mock_ai):
    tc_a, _ = alice
    tc_b, _ = bob
    uid_a = _uid(tc_a)
    t, s, pid = _prep_block(tc_a, mock_ai, title="Alice stale only")
    assert tc_a.delete(f"/api/tasks/{t['id']}").status_code == 200
    assert mo_confirm(tc_a, pid).json()["kind"] == "stale"

    assert mo_confirm(tc_b, pid).status_code == 404          # no existence oracle
    assert mo_cancel(tc_b, pid).status_code == 404
    assert tc_b.get("/api/process-input/pending").json()["proposal"] is None
