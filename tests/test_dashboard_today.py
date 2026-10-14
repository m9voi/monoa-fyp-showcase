"""B11.2 — Today / dashboard truthfulness and functional closure.

Every value the Today page shows must come from the signed-in user's real data
(the authoritative finance snapshot + the authoritative schedule engine) or be
an honest empty / loading / error state. No hardcoded location, no fabricated
"daily brief", no automatic geolocation, no second competing data path.
"""

import datetime
import os
import re

import pytest

import main
from conftest import new_authed_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), "r", encoding="utf-8") as fh:
        return fh.read()


_MAIN = _read("main.py")
_TODAY = _read("app", "page.tsx")
_TODAY_HIGHLIGHTS = _read("app", "lib", "todayHighlights.ts")
_APPCHROME = _read("app", "components", "AppChrome.tsx")
_HOOK = _read("app", "lib", "useDeviceOrigin.ts")

# The dashboard endpoint body only (so unrelated main.py text can't mask a regression).
_dash_start = _MAIN.index('@app.get("/api/dashboard-data")')
_DASH = _MAIN[_dash_start:_MAIN.index("\nimport json", _dash_start)]


# --------------------------------------------------------------------------- #
# 1. No hardcoded / fabricated location or context anywhere on the Today path #
# --------------------------------------------------------------------------- #
def test_dashboard_endpoint_has_no_hardcoded_location_or_fabricated_brief():
    lowered = _DASH.lower()
    assert "cyberjaya" not in lowered
    # no bare lat/lng literals seeded into the brief
    assert not re.search(r"\b3\.1\d{3}\b", _DASH)
    assert not re.search(r"\b101\.6\d{3}\b", _DASH)
    # the old fabricated sentence fragments must be gone
    assert "early start in" not in lowered
    assert "clear window to focus" not in lowered


def test_today_page_source_has_no_hardcoded_location_or_weather():
    lowered = _TODAY.lower()
    for banned in ("cyberjaya", "selangor", "kuala lumpur", "26°c", "clear sky",
                   "perfect conditions", "early start in"):
        assert banned not in lowered, banned


# --------------------------------------------------------------------------- #
# 2. Today never triggers geolocation and has no second data path             #
# --------------------------------------------------------------------------- #
def test_today_page_does_not_touch_geolocation():
    for banned in ("navigator.geolocation", "getCurrentLocation", "useDeviceOrigin",
                   "acquireDevice", "watchPosition"):
        assert banned not in _TODAY, banned


def test_today_page_uses_only_the_single_dashboard_endpoint():
    fetches = re.findall(r'useApiData<[^>]*>\("([^"]+)"', _TODAY)
    assert fetches == ["/api/dashboard-data"], fetches
    # no ad-hoc fetch/apiFetch to a second endpoint from the Today page
    assert "apiFetch(" not in _TODAY
    assert _TODAY.count("useApiData<") == 1  # exactly one data load on the page


def test_dashboard_endpoint_reads_the_canonical_engines_only():
    # the ONE finance snapshot and the ONE schedule loader — not ad-hoc SQL
    assert "fin.get_snapshot(user_id)" in _DASH
    assert "_load_schedule(user_id" in _DASH
    assert "conn.execute" not in _DASH and "SELECT " not in _DASH


# --------------------------------------------------------------------------- #
# 3. Honest loading / error / empty states on the Today page                  #
# --------------------------------------------------------------------------- #
def test_today_page_renders_a_truthful_error_state_with_retry():
    assert "error ?" in _TODAY
    assert "Couldn&apos;t load your day." in _TODAY
    assert "Try again" in _TODAY
    assert "onClick={reload}" in _TODAY
    # spend must not render a fake RM 0.00 before real data arrives, and must
    # not render a real-looking value when the finance source itself failed
    # (Batch 04: independent per-source degradation; Phase 07A: restyled but
    # the same truthfulness contract).
    assert "data.total_spending != null" in _TODAY
    assert "rm(data.total_spending)" in _TODAY
    assert 'data && data.finance_status === "error"' in _TODAY


def test_today_page_ctas_have_real_destinations():
    # Phase 07A: the hero card's source pill navigates via the slide's own
    # (real, data-derived) route rather than a hardcoded per-button literal —
    # so the destinations themselves are asserted in todayHighlights.ts, and
    # the page is asserted to actually navigate using that value.
    assert "router.push(activeSlide.route" in _TODAY
    assert 'PLAN: "/schedule"' in _TODAY_HIGHLIGHTS
    assert 'WALLET: "/finance"' in _TODAY_HIGHLIGHTS
    assert 'ACTIVITY: "/activity"' in _TODAY_HIGHLIGHTS
    assert 'router.push("/activity")' in _TODAY
    assert os.path.isdir(os.path.join(ROOT, "app", "schedule"))
    assert "<OmniBar" in _TODAY  # the canonical Mo command surface


# --------------------------------------------------------------------------- #
# 3b. Refetches when reopened, so a backgrounded/idle app crossing midnight   #
#     never keeps showing yesterday's "today".                               #
# --------------------------------------------------------------------------- #
def test_today_page_refetches_on_visibility_and_focus():
    assert "visibilitychange" in _TODAY
    assert "reload()" in _TODAY
    assert _TODAY.count("useApiData<") == 1  # still exactly one data source


# --------------------------------------------------------------------------- #
# 4. Auth + account isolation                                                 #
# --------------------------------------------------------------------------- #
def test_dashboard_requires_authentication(client):
    assert client.get("/api/dashboard-data").status_code == 401


def test_dashboard_is_account_isolated():
    ac, _ = new_authed_client("iso_a@example.com", name="A")
    bc, _ = new_authed_client("iso_b@example.com", name="B")
    ac.post("/api/tasks", json={"title": "Alice only task"})
    a = ac.get("/api/dashboard-data").json()
    b = bc.get("/api/dashboard-data").json()
    assert a["next_task"] == "Alice only task"
    assert b["next_task"] == "Nothing scheduled"
    assert "1 open task" in a["hero_insight"]
    assert b["hero_insight"] == "You have no upcoming events today and no open tasks."


# --------------------------------------------------------------------------- #
# 5. Empty new account -> honest "nothing" state (no fabrication)             #
# --------------------------------------------------------------------------- #
def test_empty_account_dashboard_is_honestly_empty():
    tc, _ = new_authed_client("empty@example.com", name="E")
    d = tc.get("/api/dashboard-data").json()
    assert d["total_spending"] == 0
    assert d["next_task"] == "Nothing scheduled"
    assert d["hero_insight"] == "You have no upcoming events today and no open tasks."


# --------------------------------------------------------------------------- #
# 6. Real next-task selection with deterministic ordering                     #
# --------------------------------------------------------------------------- #
def test_next_task_is_the_earliest_planned_open_task_deterministically():
    now = _myt_now()
    if now.hour >= 22:
        pytest.skip("too close to MYT midnight for deterministic 'later today' tasks")
    tc, _ = new_authed_client("nexttask@example.com", name="N")
    # created out of chronological order on purpose, but both anchored to
    # TODAY (Batch 04: a task must be today-scoped to be considered "next").
    later = (now + datetime.timedelta(hours=2)).isoformat()
    sooner = (now + datetime.timedelta(hours=1)).isoformat()
    tc.post("/api/tasks", json={"title": "Later", "planned_at": later})
    tc.post("/api/tasks", json={"title": "Sooner", "planned_at": sooner})
    tc.post("/api/tasks", json={"title": "Undated"})
    first = tc.get("/api/dashboard-data").json()["next_task"]
    # stable across repeated calls
    for _ in range(4):
        assert tc.get("/api/dashboard-data").json()["next_task"] == first
    assert first == "Sooner"


def test_completed_and_deleted_tasks_are_excluded():
    now = _myt_now()
    if now.hour >= 22:
        pytest.skip("too close to MYT midnight for deterministic 'later today' tasks")
    tc, _ = new_authed_client("exclude@example.com", name="X")
    first_at = (now + datetime.timedelta(hours=1)).isoformat()
    second_at = (now + datetime.timedelta(hours=2)).isoformat()
    t1 = tc.post("/api/tasks", json={"title": "Do first",
                                     "planned_at": first_at}).json()["task"]["id"]
    t2 = tc.post("/api/tasks", json={"title": "Do second",
                                     "planned_at": second_at}).json()["task"]["id"]
    assert tc.get("/api/dashboard-data").json()["next_task"] == "Do first"
    tc.post(f"/api/tasks/{t1}/complete")
    d = tc.get("/api/dashboard-data").json()
    assert d["next_task"] == "Do second"
    assert "1 open task" in d["hero_insight"]
    tc.delete(f"/api/tasks/{t1}")
    tc.delete(f"/api/tasks/{t2}")
    empty = tc.get("/api/dashboard-data").json()
    assert empty["next_task"] == "Nothing scheduled"
    assert empty["hero_insight"] == "You have no upcoming events today and no open tasks."


# --------------------------------------------------------------------------- #
# 7. Real next-event selection (events outrank tasks; recurring expands)      #
# --------------------------------------------------------------------------- #
def _myt_now():
    import main
    return main.sched.myt_now()


def test_next_event_today_outranks_open_tasks():
    now = _myt_now()
    if now.hour >= 23:
        pytest.skip("too close to MYT midnight for a deterministic 'later today' event")
    tc, _ = new_authed_client("nextevent@example.com", name="V")
    tc.post("/api/tasks", json={"title": "A task"})
    soon = (now + datetime.timedelta(minutes=45)).isoformat()
    later = (now + datetime.timedelta(minutes=90)).isoformat()
    tc.post("/api/events", json={"title": "Later meeting", "start": later})
    tc.post("/api/events", json={"title": "Sooner meeting", "start": soon})
    d = tc.get("/api/dashboard-data").json()
    assert d["next_task"] == "Sooner meeting"
    assert "event" in d["hero_insight"] and "open task" in d["hero_insight"]


def test_recurring_event_todays_occurrence_is_used():
    now = _myt_now()
    if now.hour >= 23:
        pytest.skip("too close to MYT midnight for a deterministic 'later today' occurrence")
    tc, _ = new_authed_client("recurring@example.com", name="R")
    start = (now + datetime.timedelta(minutes=30)).isoformat()
    tc.post("/api/events", json={"title": "Daily standup", "start": start,
                                 "recurrence": "daily"})
    d = tc.get("/api/dashboard-data").json()
    assert d["next_task"] == "Daily standup"
    assert re.search(r"\b1 event still to come today\b", d["hero_insight"])


def test_past_event_earlier_today_is_not_offered_as_next():
    now = _myt_now()
    if now.hour < 1:
        pytest.skip("too close to MYT midnight for a deterministic 'earlier today' event")
    tc, _ = new_authed_client("pastevent@example.com", name="P")
    past = (now - datetime.timedelta(minutes=30)).isoformat()
    tc.post("/api/events", json={"title": "Already happened", "start": past})
    tc.post("/api/tasks", json={"title": "Still to do"})
    d = tc.get("/api/dashboard-data").json()
    assert d["next_task"] == "Still to do"
    assert "event" not in d["hero_insight"]


# --------------------------------------------------------------------------- #
# 8. Finance value comes from the real finance data path                      #
# --------------------------------------------------------------------------- #
def test_today_spend_reflects_a_real_transaction_via_the_snapshot():
    tc, _ = new_authed_client("spend@example.com", name="S")
    assert tc.get("/api/dashboard-data").json()["total_spending"] == 0
    r = tc.post("/api/transactions", json={"amount": 12.5, "type": "expense",
                                           "category": "food",
                                           "date": _myt_now().isoformat()})
    assert r.status_code in (200, 201), r.text
    snap = tc.get("/api/finance-summary").json()
    dash = tc.get("/api/dashboard-data").json()
    assert dash["total_spending"] == snap["today_spending"] == 12.5


# --------------------------------------------------------------------------- #
# 9. Regression: shared chrome (reminder bell) + location gate still present  #
# --------------------------------------------------------------------------- #
def test_reminder_bell_still_mounted_in_app_chrome():
    assert "<RemindersBell />" in _APPCHROME
    assert "RemindersBell" in _APPCHROME


def test_location_services_gate_still_present_in_the_origin_hook():
    assert 'status: "SERVICES_OFF"' in _HOOK
    assert "locationServicesRef" in _HOOK


# --------------------------------------------------------------------------- #
# 10. Batch 04 — independent per-source degradation                           #
# --------------------------------------------------------------------------- #
def test_finance_source_failure_does_not_collapse_schedule_or_activity(monkeypatch):
    tc, _ = new_authed_client("financedown@example.com", name="F")
    tc.post("/api/tasks", json={"title": "Still visible"})

    def boom(_user_id):
        raise RuntimeError("finance boom")
    monkeypatch.setattr(main.fin, "get_snapshot", boom)

    r = tc.get("/api/dashboard-data")
    assert r.status_code == 200
    d = r.json()
    assert d["finance_status"] == "error"
    assert d["total_spending"] is None
    assert d["finance"] is None
    assert d["finance_error"]
    # schedule and activity are independent sources and still render
    assert d["schedule_status"] == "ok"
    assert d["next_task"] == "Still visible"
    assert d["activity_status"] == "ok"


def test_schedule_source_failure_does_not_collapse_finance_or_activity(monkeypatch):
    tc, _ = new_authed_client("scheduledown@example.com", name="S")
    tc.post("/api/transactions", json={"amount": 5, "type": "expense", "category": "food",
                                       "date": main.sched.myt_now().isoformat()})

    def boom(*_a, **_k):
        raise RuntimeError("schedule boom")
    monkeypatch.setattr(main, "_load_schedule", boom)

    r = tc.get("/api/dashboard-data")
    assert r.status_code == 200
    d = r.json()
    assert d["schedule_status"] == "error"
    assert d["schedule_error"]
    # an honest fallback, never a fabricated number
    assert d["next_task"] == "Nothing scheduled"
    assert d["open_tasks_today"] is None
    assert d["events_remaining_today"] is None
    assert d["unresolved_conflicts_today"] is None
    # finance and activity are independent sources and still render
    assert d["finance_status"] == "ok"
    assert d["total_spending"] == 5.0
    assert d["activity_status"] == "ok"


def test_activity_source_failure_does_not_collapse_schedule_or_finance(monkeypatch):
    tc, _ = new_authed_client("activitydown@example.com", name="Ac")

    def boom(_user_id):
        raise RuntimeError("activity boom")
    monkeypatch.setattr(main, "_get_activity_recommendations", boom)

    r = tc.get("/api/dashboard-data")
    assert r.status_code == 200
    d = r.json()
    assert d["activity_status"] == "error"
    assert d["activity"] is None
    assert d["activity_error"]
    assert d["schedule_status"] == "ok"
    assert d["finance_status"] == "ok"


# --------------------------------------------------------------------------- #
# 11. Batch 04 — richer real sections: finance, activity, honest empty states  #
# --------------------------------------------------------------------------- #
def test_finance_section_reflects_real_budget_state_with_truthful_empty_state():
    tc, _ = new_authed_client("financesection@example.com", name="FB")
    empty = tc.get("/api/dashboard-data").json()
    assert empty["finance_status"] == "ok"
    assert empty["finance"]["daily_budget"] == 0
    assert empty["finance"]["daily_over_budget"] is False

    r = tc.post("/api/finance/settings", json={"daily_budget": 10})
    assert r.status_code == 200, r.text
    tc.post("/api/transactions", json={"amount": 9, "type": "expense", "category": "food",
                                       "date": main.sched.myt_now().isoformat()})
    d = tc.get("/api/dashboard-data").json()
    assert d["finance"]["daily_budget"] == 10
    assert d["finance"]["daily_used_pct"] == 90.0
    assert d["finance"]["daily_over_budget"] is False


def test_activity_section_reflects_real_recommendations_with_truthful_empty_state():
    tc, user = new_authed_client("activitysection@example.com", name="AS")
    empty = tc.get("/api/dashboard-data").json()
    assert empty["activity_status"] == "ok"
    assert empty["activity"]["active_recommendations"] == []

    import db as db_layer
    conn = db_layer.get_connection()
    conn.execute(
        "INSERT INTO ai_recommendations (id, user_id, module, type, message, status) "
        "VALUES ('rec1', ?, 'ACTIVITY', 'suggestion', 'Try the new park nearby', 'PENDING')",
        (user["id"],),
    )
    conn.commit()
    conn.close()

    d = tc.get("/api/dashboard-data").json()
    assert d["activity_status"] == "ok"
    assert len(d["activity"]["active_recommendations"]) == 1
    assert d["activity"]["active_recommendations"][0]["message"] == "Try the new park nearby"


# --------------------------------------------------------------------------- #
# 12. Batch 04 — today-scoping correctness (the fixed bug)                    #
# --------------------------------------------------------------------------- #
def test_a_task_dated_for_a_different_day_is_not_treated_as_todays():
    tc, _ = new_authed_client("futuretask@example.com", name="Fut")
    tomorrow = (main.sched.myt_now() + datetime.timedelta(days=1)).isoformat()
    tc.post("/api/tasks", json={"title": "Not today", "planned_at": tomorrow})
    d = tc.get("/api/dashboard-data").json()
    # a task dated for a different real day must not appear as Today's next
    # item, and must not be counted in Today's open-task total
    assert d["next_task"] == "Nothing scheduled"
    assert d["open_tasks_today"] == 0
    assert d["hero_insight"] == "You have no upcoming events today and no open tasks."


def test_an_undated_task_still_counts_as_todays_per_the_schedule_pages_own_rule():
    tc, _ = new_authed_client("undatedtoday@example.com", name="Und")
    tc.post("/api/tasks", json={"title": "No date at all"})
    d = tc.get("/api/dashboard-data").json()
    # matches app/schedule/page.tsx's taskAnchorKey rule: an undated open task
    # buckets into today
    assert d["next_task"] == "No date at all"
    assert d["open_tasks_today"] == 1


def test_a_task_overdue_from_a_past_day_is_not_treated_as_todays():
    tc, _ = new_authed_client("pasttask@example.com", name="Past")
    yesterday = (main.sched.myt_now() - datetime.timedelta(days=1)).isoformat()
    tc.post("/api/tasks", json={"title": "Overdue", "due_date": yesterday})
    d = tc.get("/api/dashboard-data").json()
    assert d["next_task"] == "Nothing scheduled"
    assert d["open_tasks_today"] == 0


# --------------------------------------------------------------------------- #
# 13. Batch 04 — deterministic priority / smart-stack ordering                #
# --------------------------------------------------------------------------- #
def test_priority_stack_is_empty_with_nothing_urgent():
    tc, _ = new_authed_client("noprio@example.com", name="NP")
    d = tc.get("/api/dashboard-data").json()
    assert d["priority"] == []


def test_priority_ranks_schedule_conflicts_above_a_budget_warning():
    tc, _ = new_authed_client("priorank@example.com", name="PR")
    d0 = main.sched.myt_now().replace(hour=14, minute=0, second=0, microsecond=0)
    # a genuine, real overlapping pair of events -> a real, deterministically
    # detected schedule conflict, not a fabricated one
    tc.post("/api/events", json={"title": "Design Review", "start": d0.isoformat(),
                                 "end": (d0 + datetime.timedelta(hours=1)).isoformat()})
    tc.post("/api/events", json={"title": "Team Sync",
                                 "start": (d0 + datetime.timedelta(minutes=30)).isoformat(),
                                 "end": (d0 + datetime.timedelta(minutes=90)).isoformat()})
    tc.post("/api/finance/settings", json={"daily_budget": 10})
    tc.post("/api/transactions", json={"amount": 12, "type": "expense", "category": "food",
                                       "date": main.sched.myt_now().isoformat()})
    d = tc.get("/api/dashboard-data").json()
    assert d["unresolved_conflicts_today"] >= 1
    kinds = [p["kind"] for p in d["priority"]]
    assert "conflict" in kinds and "budget_over" in kinds
    assert d["priority"][0]["kind"] == "conflict"


def test_priority_flags_a_real_budget_threshold_crossed():
    tc, _ = new_authed_client("budgetprio@example.com", name="BP")
    tc.post("/api/finance/settings", json={"daily_budget": 10})
    tc.post("/api/transactions", json={"amount": 12, "type": "expense", "category": "food",
                                       "date": main.sched.myt_now().isoformat()})
    d = tc.get("/api/dashboard-data").json()
    kinds = [p["kind"] for p in d["priority"]]
    assert "budget_over" in kinds


def test_priority_never_fires_without_a_real_budget_set():
    tc, _ = new_authed_client("nobudgetprio@example.com", name="NB")
    tc.post("/api/transactions", json={"amount": 500, "type": "expense", "category": "food",
                                       "date": main.sched.myt_now().isoformat()})
    d = tc.get("/api/dashboard-data").json()
    kinds = [p["kind"] for p in d["priority"]]
    assert "budget_over" not in kinds and "budget_near" not in kinds


# --------------------------------------------------------------------------- #
# 14. Batch 04 — refresh/data-update behavior (backend side of it)            #
# --------------------------------------------------------------------------- #
def test_dashboard_reflects_a_mutation_immediately_no_caching():
    tc, _ = new_authed_client("liverefresh@example.com", name="LR")
    before = tc.get("/api/dashboard-data").json()
    assert before["open_tasks_today"] == 0
    tc.post("/api/tasks", json={"title": "Just added"})
    after = tc.get("/api/dashboard-data").json()
    assert after["open_tasks_today"] == 1
    assert after["next_task"] == "Just added"
