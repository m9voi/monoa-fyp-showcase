"""B6 — per-occurrence conflict decisions for recurring events.

Covers both surfaces:
  * B3 recurring event↔event overlaps (Keep / Accept scoped to one occurrence)
  * B5 recurring outdoor weather conflicts (Keep / Find another time / Confirm
    move scoped to one occurrence)

All decisions are per-occurrence: a decision on occurrence A never affects
occurrence B, the recurrence rule and remaining occurrences stay intact, moves
are written as additive occurrence-override rows (never a series edit), and every
guarantee (exactly-once, stale protection, IDOR, no raw GPS/forecast leakage,
migration on a fresh DB) is asserted. DETERMINISTIC: the real weather provider is
replaced with a provider-shaped normalised forecast.
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
import weather_core as wc
import weather_provider as wp
from conftest import new_authed_client

UTC = dt.timezone.utc
MYT = dt.timezone(dt.timedelta(hours=8))
KLCC = {"provider": "google", "provider_place_id": "ChIJ_klcc_recurring_b6",
        "name": "KLCC Park", "latitude": 3.1578, "longitude": 101.7117,
        "address": "Kuala Lumpur"}


# --------------------------------------------------------------------------- #
# provider-shaped normalised forecast (mirrors test_weather_conflict.py)
# --------------------------------------------------------------------------- #
_BENIGN = dict(temp=30.0, app_temp=31.0, pp=10, precip=0.0, snow=0.0,
               code=1, wind=8.0, gust=15.0, vis=20000.0)
_HAZARDS = {
    "thunderstorm": dict(code=95, pp=80, precip=6.0, app_temp=31.0, gust=20.0),
    "heavy_rain": dict(code=65, pp=85, precip=14.0, app_temp=29.0, gust=20.0),
}


def _forecast(hazard=None, *, hazard_hours=range(0, 72), fetched_minutes_ago=0, n=72):
    base_utc = dt.datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    rows = []
    for i in range(n):
        spec = dict(_BENIGN)
        if hazard and i in hazard_hours:
            spec.update(_HAZARDS[hazard])
        t = base_utc + dt.timedelta(hours=i)
        rows.append({
            "time_utc": t, "time_local": t.astimezone(MYT).replace(tzinfo=None).isoformat(),
            "temperature_c": spec["temp"], "apparent_temperature_c": spec["app_temp"],
            "precipitation_probability": spec["pp"], "precipitation_mm": spec["precip"],
            "rain_mm": spec["precip"], "snowfall_mm": spec["snow"],
            "wmo_code": spec["code"], "wind_speed_kmh": spec["wind"],
            "wind_gusts_kmh": spec["gust"], "visibility_m": spec["vis"],
        })
    fetched = dt.datetime.now(UTC) - dt.timedelta(minutes=fetched_minutes_ago)
    return {
        "provider": "open-meteo", "attribution": wp.OPEN_METEO_ATTRIBUTION,
        "timezone": "Asia/Kuala_Lumpur", "timezone_abbreviation": "+08",
        "utc_offset_seconds": 28800, "fetched_at": fetched.isoformat(),
        "current": None, "hourly": rows,
    }


@pytest.fixture(autouse=True)
def _clear_cache():
    wp.cache_clear()
    yield
    wp.cache_clear()


@pytest.fixture()
def wxmock(monkeypatch):
    box = {"forecast": _forecast(None), "error": None}

    async def fake_get_forecast(provider, latitude, longitude, **kw):
        if box["error"] is not None:
            raise wc.WeatherProviderError(box["error"])
        return box["forecast"]

    monkeypatch.setattr(main.weather_provider, "get_forecast", fake_get_forecast)

    def _set(forecast=None, *, error=None):
        if forecast is not None:
            box["forecast"] = forecast
        box["error"] = error
    return _set


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _mk(tc, title, start, end=None, **kw):
    body = {"title": title, "start": start.isoformat()}
    if end is not None:
        body["end"] = end.isoformat()
    body.update(kw)
    r = tc.post("/api/events", json=body)
    assert r.status_code == 201, r.text
    return r.json()["event"]


def _day(tc, date_str):
    r = tc.get(f"/api/schedule/day?date={date_str}")
    assert r.status_code == 200, r.text
    return r.json()


def _conf_on(tc, date_str):
    return _day(tc, date_str)["conflicts"]


def _event_row(uid, eid):
    c = db_layer.get_connection()
    try:
        r = c.execute("SELECT start_time, end_time, recurrence, recurrence_until "
                      "FROM schedule_events WHERE id=? AND user_id=?", (eid, uid)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


def _overrides(uid, series_id=None):
    c = db_layer.get_connection()
    try:
        q = "SELECT * FROM schedule_occurrence_overrides WHERE user_id=?"
        args = [uid]
        if series_id:
            q += " AND series_event_id=?"
            args.append(series_id)
        return [dict(r) for r in c.execute(q, args).fetchall()]
    finally:
        c.close()


def _wx_conflicts(tc):
    r = tc.get("/api/schedule/weather-conflicts")
    assert r.status_code == 200, r.text
    return r.json()


def _relogin(email, password="password123"):
    from fastapi.testclient import TestClient
    tc = TestClient(main.app)
    r = tc.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return tc


# Mondays: computed relative to whenever this suite actually runs (never a
# fixed calendar date) so these fixtures can't go stale as real time passes —
# B3 conflict recomputation only expands occurrences within a bounded window
# (now - _SC_RECUR_WINDOW_BACK_DAYS .. now + _SC_RECUR_WINDOW_FWD_DAYS, see
# main.py), so a hardcoded date eventually falls outside it and every conflict
# lookup here 404s/409s. OCC_A's Monday is always the NEXT Monday strictly
# after "now" (full forward headroom); the series starts one week before that
# (safely inside the back-window), weekly, 5 Mondays total.
def _next_monday(base: dt.datetime) -> dt.date:
    days_ahead = (7 - base.weekday()) % 7 or 7      # 0 would be "today"; never that
    return (base + dt.timedelta(days=days_ahead)).date()


_ANCHOR = _next_monday(dt.datetime.now(MYT))
SERIES_START = dt.datetime(_ANCHOR.year, _ANCHOR.month, _ANCHOR.day, 10, 0, tzinfo=MYT) - dt.timedelta(days=7)
OCC_A_DATE = _ANCHOR.isoformat()                                    # 2nd Monday
OCC_B_DATE = (_ANCHOR + dt.timedelta(days=7)).isoformat()           # 3rd Monday
SERIES_DATES = [(SERIES_START.date() + dt.timedelta(days=7 * k)).isoformat() for k in range(5)]
SERIES_RECURRENCE_UNTIL = SERIES_DATES[-1]
SERIES_WIDE_QUERY_TO = (dt.date.fromisoformat(SERIES_DATES[-1]) + dt.timedelta(days=7)).isoformat()

# "Review A"/"Review B" — one-off items that overlap the series' own 10:00
# occurrence on OCC_A_DATE/OCC_B_DATE (10:30-11:15, same relationship as before).
OCC_A_START = dt.datetime.fromisoformat(OCC_A_DATE).replace(hour=10, minute=30, tzinfo=MYT)
OCC_A_END = OCC_A_START + dt.timedelta(minutes=45)
OCC_B_START = dt.datetime.fromisoformat(OCC_B_DATE).replace(hour=10, minute=30, tzinfo=MYT)
OCC_B_END = OCC_B_START + dt.timedelta(minutes=45)
# the series' OWN (undisturbed) occurrence start at A / B, for before/after comparisons
SERIES_OCC_A_START = SERIES_START + dt.timedelta(days=7)
SERIES_OCC_B_START = SERIES_START + dt.timedelta(days=14)


def _weekly_series(tc, title="Team Standup", dur_h=1, **kw):
    return _mk(tc, title, SERIES_START, SERIES_START + dt.timedelta(hours=dur_h),
              recurrence="weekly", recurrence_until=SERIES_RECURRENCE_UNTIL, **kw)


def _overlap_before(tc, occ_date_str, title="Review"):
    """A one-off event that STARTS 30 min before the 10:00 series occurrence on
    ``occ_date_str`` and runs 45 min — so it overlaps, and the recurring
    occurrence (later start) is the one a shift proposal moves."""
    d = dt.date.fromisoformat(occ_date_str)
    s = dt.datetime(d.year, d.month, d.day, 9, 30, tzinfo=MYT)
    return _mk(tc, title, s, s + dt.timedelta(minutes=45))


# ========================================================================= #
# 1 / 2 / 3 / 4 — occurrence identity is stable + independent + non-destructive
# ========================================================================= #
def test_two_occurrences_have_different_stable_identities():
    tc, _ = new_authed_client("b6ident@example.com")
    _weekly_series(tc)
    _mk(tc, "Review A", OCC_A_START, OCC_A_END)
    _mk(tc, "Review B", OCC_B_START, OCC_B_END)

    ca = _conf_on(tc, OCC_A_DATE)
    cb = _conf_on(tc, OCC_B_DATE)
    assert len(ca) == 1 and len(cb) == 1
    fa, fb = ca[0]["fingerprint"], cb[0]["fingerprint"]
    assert fa and fb and fa != fb
    for c in (ca[0], cb[0]):
        assert c["conflict_type"] == "recurring_overlap"
        assert c["scope"] == "occurrence"
        assert c["decidable"] is True
    assert ca[0]["occurrence_date"] == OCC_A_DATE
    assert cb[0]["occurrence_date"] == OCC_B_DATE
    # identity is deterministic run-to-run
    assert _conf_on(tc, OCC_A_DATE)[0]["fingerprint"] == fa


def test_keeping_occurrence_A_does_not_suppress_occurrence_B():
    tc, _ = new_authed_client("b6keepAB@example.com")
    _weekly_series(tc)
    _mk(tc, "Review A", OCC_A_START, OCC_A_END)
    _mk(tc, "Review B", OCC_B_START, OCC_B_END)
    fa = _conf_on(tc, OCC_A_DATE)[0]["fingerprint"]

    r = tc.post(f"/api/schedule/conflicts/{fa}/keep")
    assert r.status_code == 200 and r.json()["decision"] == "kept"
    assert r.json()["scope"] == "occurrence" and r.json()["occurrence_date"] == OCC_A_DATE

    # A is settled...
    assert _conf_on(tc, OCC_A_DATE)[0]["decision"] == "kept"
    # ...B is still unresolved
    cb = _conf_on(tc, OCC_B_DATE)
    assert len(cb) == 1 and cb[0]["decision"] is None


def test_resolving_occurrence_A_changes_only_A_and_leaves_series_intact():
    tc, user = new_authed_client("b6moveA@example.com")
    series = _weekly_series(tc)
    _overlap_before(tc, OCC_A_DATE)
    conf = _conf_on(tc, OCC_A_DATE)[0]
    assert conf["proposal"] and conf["proposal"]["is_recurring_instance"] is True
    fa = conf["fingerprint"]

    r = tc.post(f"/api/schedule/conflicts/{fa}/accept")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decision"] == "accepted" and body["scope"] == "occurrence"
    assert body["occurrence_date"] == OCC_A_DATE

    # the SERIES row is byte-for-byte unchanged (rule + base time intact)
    row = _event_row(user["id"], series["id"])
    assert row["start_time"] == SERIES_START.isoformat()
    assert row["recurrence"] == "weekly" and row["recurrence_until"]

    # exactly one occurrence override, for OCC_A only
    ov = _overrides(user["id"], series["id"])
    assert len(ov) == 1 and ov[0]["occurrence_date"] == OCC_A_DATE
    assert ov[0]["status"] == "moved" and ov[0]["origin"] == "overlap"

    # the A occurrence now shows at its moved time; B is untouched at 10:00
    wide = tc.get(f"/api/schedule?date_from={SERIES_DATES[0]}&date_to={SERIES_WIDE_QUERY_TO}").json()
    occ = {e["occurrence_date"]: e for e in wide["events"] if e["id"] == series["id"]}
    assert occ[SERIES_DATES[0]]["start"] == SERIES_START.isoformat()
    assert occ[OCC_B_DATE]["start"] == SERIES_OCC_B_START.isoformat()
    assert occ[OCC_A_DATE]["start"] != SERIES_OCC_A_START.isoformat()
    assert occ[OCC_A_DATE].get("overridden") is True
    assert sorted(occ) == SERIES_DATES                         # count + rule intact
    # and A's overlap no longer surfaces
    assert _conf_on(tc, OCC_A_DATE) == [] or _conf_on(tc, OCC_A_DATE)[0]["decision"] == "accepted"


# ========================================================================= #
# 5 — persistence across refresh + logout/login
# ========================================================================= #
def test_occurrence_decisions_persist_across_relogin():
    email = "b6persist@example.com"
    tc, _ = new_authed_client(email)
    series = _weekly_series(tc)
    _mk(tc, "Review A", OCC_A_START, OCC_A_END)
    _mk(tc, "Review B", OCC_B_START, OCC_B_END)
    fa = _conf_on(tc, OCC_A_DATE)[0]["fingerprint"]
    assert tc.post(f"/api/schedule/conflicts/{fa}/keep").status_code == 200

    tc2 = _relogin(email)
    assert _conf_on(tc2, OCC_A_DATE)[0]["decision"] == "kept"
    assert _conf_on(tc2, OCC_B_DATE)[0]["decision"] is None
    # refresh again — still stable
    assert _conf_on(tc2, OCC_A_DATE)[0]["decision"] == "kept"


# ========================================================================= #
# 6 — repeated confirmation mutates exactly once
# ========================================================================= #
def test_repeated_accept_of_one_occurrence_moves_it_exactly_once():
    tc, user = new_authed_client("b6once@example.com")
    series = _weekly_series(tc)
    _overlap_before(tc, OCC_A_DATE)
    fa = _conf_on(tc, OCC_A_DATE)[0]["fingerprint"]

    first = tc.post(f"/api/schedule/conflicts/{fa}/accept")
    assert first.status_code == 200 and first.json()["already"] is False
    for _ in range(3):
        again = tc.post(f"/api/schedule/conflicts/{fa}/accept")
        assert again.status_code == 200 and again.json()["already"] is True
    assert len(_overrides(user["id"], series["id"])) == 1
    c = db_layer.get_connection()
    try:
        n = c.execute("SELECT COUNT(*) FROM schedule_conflict_decisions WHERE user_id=?",
                      (user["id"],)).fetchone()[0]
    finally:
        c.close()
    assert n == 1


# ========================================================================= #
# 7 — a manual edit / recurrence-rule change stales an old proposal
# ========================================================================= #
def test_series_rule_change_stales_prior_occurrence_decision_and_drops_overrides():
    tc, user = new_authed_client("b6rule@example.com")
    series = _weekly_series(tc)
    _mk(tc, "Review A", OCC_A_START, OCC_A_END)
    fa = _conf_on(tc, OCC_A_DATE)[0]["fingerprint"]
    assert tc.post(f"/api/schedule/conflicts/{fa}/keep").status_code == 200

    # shift the series base time by 15 min -> every occurrence identity changes
    shifted_start = SERIES_START + dt.timedelta(minutes=15)
    r = tc.patch(f"/api/events/{series['id']}", json={
        "start": shifted_start.isoformat(), "end": (shifted_start + dt.timedelta(hours=1)).isoformat()})
    assert r.status_code == 200
    assert _overrides(user["id"], series["id"]) == []          # exceptions dropped

    # the old keep no longer matches -> a fresh (now different) overlap surfaces
    again = _conf_on(tc, OCC_A_DATE)
    if again:
        assert again[0]["fingerprint"] != fa
        assert again[0]["decision"] is None
    # the stale row is stamped
    c = db_layer.get_connection()
    try:
        row = c.execute("SELECT stale_at FROM schedule_conflict_decisions "
                        "WHERE user_id=? AND fingerprint=?", (user["id"], fa)).fetchone()
    finally:
        c.close()
    assert row and row["stale_at"] is not None


def test_stale_accept_after_manual_occurrence_shift_is_409():
    tc, user = new_authed_client("b6staleacc@example.com")
    series = _weekly_series(tc)
    a = _mk(tc, "Review A", OCC_A_START, OCC_A_END)
    fa = _conf_on(tc, OCC_A_DATE)[0]["fingerprint"]
    # the one-off side moves away (no more overlap) -> accept must 409, no mutation
    moved_start = OCC_A_START.replace(hour=14, minute=0)
    assert tc.patch(f"/api/events/{a['id']}", json={
        "start": moved_start.isoformat(), "end": (moved_start + dt.timedelta(hours=1)).isoformat()}).status_code == 200
    r = tc.post(f"/api/schedule/conflicts/{fa}/accept")
    assert r.status_code == 409
    assert _overrides(user["id"], series["id"]) == []


# ========================================================================= #
# 8 — deleted series / occurrence rejects confirmation
# ========================================================================= #
def test_deleted_series_rejects_occurrence_accept_and_cascades_overrides():
    tc, user = new_authed_client("b6del@example.com")
    series = _weekly_series(tc)
    _overlap_before(tc, OCC_A_DATE)
    fa = _conf_on(tc, OCC_A_DATE)[0]["fingerprint"]
    assert tc.post(f"/api/schedule/conflicts/{fa}/accept").status_code == 200
    assert len(_overrides(user["id"], series["id"])) == 1

    assert tc.delete(f"/api/events/{series['id']}").status_code == 200
    assert _overrides(user["id"], series["id"]) == []           # cascaded
    # a replay attempt is a benign replay of the recorded decision or a 409 —
    # never a resurrected mutation
    r = tc.post(f"/api/schedule/conflicts/{fa}/accept")
    assert r.status_code in (200, 409)


# ========================================================================= #
# 9 — cross-user access is rejected
# ========================================================================= #
def test_cross_user_cannot_act_on_another_users_occurrence_conflict():
    ta, ua = new_authed_client("b6owner@example.com")
    _weekly_series(ta)
    _mk(ta, "Review A", OCC_A_START, OCC_A_END)
    fa = _conf_on(ta, OCC_A_DATE)[0]["fingerprint"]

    tb, ub = new_authed_client("b6intruder@example.com")
    assert tb.post(f"/api/schedule/conflicts/{fa}/keep").status_code == 409
    assert tb.post(f"/api/schedule/conflicts/{fa}/accept").status_code == 409
    # intruder recorded nothing
    c = db_layer.get_connection()
    try:
        assert c.execute("SELECT COUNT(*) FROM schedule_conflict_decisions WHERE user_id=?",
                         (ub["id"],)).fetchone()[0] == 0
    finally:
        c.close()
    # owner's conflict is still actionable
    assert ta.post(f"/api/schedule/conflicts/{fa}/keep").status_code == 200


# ========================================================================= #
# 10 — non-recurring B3 / B5 behaviour is unchanged
# ========================================================================= #
def test_non_recurring_overlap_fingerprint_and_flow_unchanged():
    tc, user = new_authed_client("b6nonrec@example.com")
    # 2 days out: comfortably inside the recompute window either way time passes.
    d = (dt.datetime.now(MYT) + dt.timedelta(days=2)).replace(hour=14, minute=0, second=0, microsecond=0)
    _mk(tc, "Design Review", d, d + dt.timedelta(hours=1))
    _mk(tc, "Team Sync", d + dt.timedelta(minutes=30), d + dt.timedelta(minutes=90))
    conf = _conf_on(tc, d.date().isoformat())[0]
    assert conf["conflict_type"] == "event_overlap"
    assert conf["scope"] == "series" and conf["occurrence_date"] is None
    r = tc.post(f"/api/schedule/conflicts/{conf['fingerprint']}/accept")
    assert r.status_code == 200 and r.json()["scope"] == "series"
    # no occurrence overrides created for a one-off move
    assert _overrides(user["id"]) == []


# ========================================================================= #
# 11-15 — recurring OUTDOOR weather conflicts, per occurrence
# ========================================================================= #
# a DAILY outdoor series whose next two occurrences fall inside the 48h horizon
def _daily_outdoor(tc, dur_h=1, **kw):
    now = dt.datetime.now(MYT).replace(minute=0, second=0, microsecond=0)
    s = now + dt.timedelta(hours=3)                 # ~3h out
    body = {"title": "Evening run", "start": s.isoformat(),
            "end": (s + dt.timedelta(hours=dur_h)).isoformat(),
            "environment": "outdoor", "place": KLCC,
            "recurrence": "daily", "recurrence_until": (now + dt.timedelta(days=6)).isoformat()}
    body.update(kw)
    r = tc.post("/api/events", json=body)
    assert r.status_code == 201, r.text
    return r.json()["event"]


def test_recurring_indoor_or_uncertain_occurrence_has_no_weather_conflict(wxmock):
    wxmock(_forecast("thunderstorm"))
    tc, _ = new_authed_client("b6wxindoor@example.com")
    _daily_outdoor(tc, environment="indoor")
    _daily_outdoor(tc, environment="uncertain", title="Maybe run")
    assert _wx_conflicts(tc)["conflicts"] == []


def test_recurring_outdoor_occurrence_beyond_horizon_has_no_weather_conflict(wxmock):
    wxmock(_forecast("thunderstorm"))
    tc, _ = new_authed_client("b6wxhorizon@example.com")
    now = dt.datetime.now(MYT).replace(minute=0, second=0, microsecond=0)
    far = now + dt.timedelta(days=20)
    tc.post("/api/events", json={
        "title": "Far run", "start": far.isoformat(),
        "end": (far + dt.timedelta(hours=1)).isoformat(),
        "environment": "outdoor", "place": KLCC,
        "recurrence": "daily", "recurrence_until": (far + dt.timedelta(days=10)).isoformat()})
    assert _wx_conflicts(tc)["conflicts"] == []


def test_recurring_outdoor_occurrence_in_horizon_with_hazard_yields_one_warning(wxmock):
    wxmock(_forecast("thunderstorm"))
    tc, _ = new_authed_client("b6wxhaz@example.com")
    ev = _daily_outdoor(tc)
    data = _wx_conflicts(tc)
    assert data["provider_unavailable"] is False
    # one contextual warning per in-horizon occurrence (>=1), each occurrence-scoped
    assert len(data["conflicts"]) >= 1
    for c in data["conflicts"]:
        assert c["event_id"] == ev["id"]
        assert c["hazard_type"] == "thunderstorm"
        assert c["scope"] == "occurrence" and c["occurrence_date"]
    # distinct occurrences -> distinct fingerprints
    fps = {c["fingerprint"] for c in data["conflicts"]}
    assert len(fps) == len(data["conflicts"])


def test_ordinary_recurring_forecast_produces_no_warning(wxmock):
    wxmock(_forecast(None))
    tc, _ = new_authed_client("b6wxbenign@example.com")
    _daily_outdoor(tc)
    assert _wx_conflicts(tc)["conflicts"] == []


def test_weather_keep_and_escalation_are_occurrence_specific(wxmock):
    wxmock(_forecast("thunderstorm"))
    tc, _ = new_authed_client("b6wxkeep@example.com")
    _daily_outdoor(tc)
    data = _wx_conflicts(tc)
    assert len(data["conflicts"]) >= 2, "need two in-horizon occurrences for this test"
    first, second = data["conflicts"][0], data["conflicts"][1]

    r = tc.post(f"/api/schedule/weather-conflicts/{first['fingerprint']}/keep")
    assert r.status_code == 200 and r.json()["scope"] == "occurrence"
    assert r.json()["occurrence_date"] == first["occurrence_date"]

    after = _wx_conflicts(tc)
    kept = [c for c in after["conflicts"] if c["fingerprint"] == first["fingerprint"]]
    other = [c for c in after["conflicts"] if c["fingerprint"] == second["fingerprint"]]
    assert kept and kept[0]["decision"] == "kept"
    assert other and other[0]["decision"] is None          # sibling still unresolved

    # immaterial refresh -> the kept occurrence stays suppressed from "unresolved"
    assert first["fingerprint"] not in {c["fingerprint"] for c in after["unresolved"]}


# ========================================================================= #
# 16 — candidate movement avoids collisions AND material weather hazards
# ========================================================================= #
def test_weather_confirm_moves_one_occurrence_only_avoiding_hazard_and_collisions(wxmock):
    # thunderstorm only for the next ~8h; benign afterwards so an alternative exists
    wxmock(_forecast("thunderstorm", hazard_hours=range(0, 8)))
    tc, user = new_authed_client("b6wxmove@example.com")
    series = _daily_outdoor(tc)
    base_row = _event_row(user["id"], series["id"])

    data = _wx_conflicts(tc)
    target = data["conflicts"][0]
    sibling = data["conflicts"][1] if len(data["conflicts"]) > 1 else None

    alts = tc.post(f"/api/schedule/weather-conflicts/{target['fingerprint']}/alternatives")
    assert alts.status_code == 200, alts.text
    ab = alts.json()
    assert ab["proposal_id"] and ab["candidates"]
    assert ab["scope"] == "occurrence" and ab["occurrence_date"] == target["occurrence_date"]
    assert all("completely safe" not in c["basis"].lower() for c in ab["candidates"])

    conf = tc.post(
        f"/api/schedule/weather-conflicts/alternatives/{ab['proposal_id']}/confirm",
        json={"candidate_index": 0})
    assert conf.status_code == 200, conf.text
    cb = conf.json()
    assert cb["decision"] == "moved" and cb["scope"] == "occurrence"

    # series row unchanged; exactly one weather-origin override for this occurrence
    assert _event_row(user["id"], series["id"]) == base_row
    ov = _overrides(user["id"], series["id"])
    assert len(ov) == 1 and ov[0]["origin"] == "weather"
    assert ov[0]["occurrence_date"] == target["occurrence_date"]

    # re-read: the moved occurrence's hazard is resolved; the sibling is not
    after = _wx_conflicts(tc)
    fps_unresolved = {c["fingerprint"] for c in after["unresolved"]}
    assert target["fingerprint"] not in fps_unresolved
    if sibling:
        assert sibling["occurrence_date"] in {c["occurrence_date"] for c in after["conflicts"]}

    # triple confirm -> still exactly one override + one decision
    for _ in range(3):
        rr = tc.post(
            f"/api/schedule/weather-conflicts/alternatives/{ab['proposal_id']}/confirm",
            json={"candidate_index": 0})
        assert rr.status_code in (200, 409)
    assert len(_overrides(user["id"], series["id"])) == 1


def test_weather_confirm_rejects_stale_after_series_rule_change(wxmock):
    wxmock(_forecast("thunderstorm", hazard_hours=range(0, 8)))
    tc, user = new_authed_client("b6wxstale@example.com")
    series = _daily_outdoor(tc)
    target = _wx_conflicts(tc)["conflicts"][0]
    ab = tc.post(f"/api/schedule/weather-conflicts/{target['fingerprint']}/alternatives").json()
    assert ab["proposal_id"]

    # change the series recurrence rule -> occurrence identity + overrides invalid
    assert tc.patch(f"/api/events/{series['id']}", json={"recurrence": "weekdays"}).status_code == 200

    r = tc.post(f"/api/schedule/weather-conflicts/alternatives/{ab['proposal_id']}/confirm",
                json={"candidate_index": 0})
    assert r.status_code == 409
    assert _overrides(user["id"], series["id"]) == []


# ========================================================================= #
# 17 — no raw forecast / GPS leakage into persisted rows or logs
# ========================================================================= #
def test_no_raw_forecast_or_coordinates_persisted_for_recurring_weather_decision(wxmock):
    wxmock(_forecast("thunderstorm", hazard_hours=range(0, 8)))
    tc, user = new_authed_client("b6wxprivacy@example.com")
    series = _daily_outdoor(tc)
    target = _wx_conflicts(tc)["conflicts"][0]
    ab = tc.post(f"/api/schedule/weather-conflicts/{target['fingerprint']}/alternatives").json()
    tc.post(f"/api/schedule/weather-conflicts/alternatives/{ab['proposal_id']}/confirm",
            json={"candidate_index": 0})

    c = db_layer.get_connection()
    try:
        wd = [dict(r) for r in c.execute(
            "SELECT * FROM weather_decisions WHERE user_id=?", (user["id"],)).fetchall()]
        ov = [dict(r) for r in c.execute(
            "SELECT * FROM schedule_occurrence_overrides WHERE user_id=?", (user["id"],)).fetchall()]
    finally:
        c.close()
    assert wd and ov
    blobs = json.dumps(wd) + json.dumps(ov)
    assert "3.1578" not in blobs and "101.7117" not in blobs      # no place coords
    assert "hourly" not in blobs and "visibility_m" not in blobs  # no raw forecast
    ej = json.loads(wd[0]["evidence_json"])
    assert set(ej) <= {"hazard_type", "severity_tier", "values", "window",
                       "forecast_fetched_at", "moved_to"}


# ========================================================================= #
# 18 — migration applies on a fresh disposable database
# ========================================================================= #
def test_migration_0016_applies_on_fresh_db():
    tmp = tempfile.mkdtemp(prefix="coreos_b6_")
    dbp = os.path.join(tmp, "fresh.db")
    env = dict(os.environ, COREOS_DB_PATH=dbp)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    up = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "0016_recurring_conflict_decisions"],
                        cwd=root, env=env, capture_output=True, text=True)
    assert up.returncode == 0, up.stderr
    con = sqlite3.connect(dbp)
    try:
        ver = con.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert ver == "0016_recurring_conflict_decisions"
        cols = {r[1] for r in con.execute("PRAGMA table_info(schedule_occurrence_overrides)")}
        assert {"user_id", "series_event_id", "occurrence_date", "start_time",
                "end_time", "status", "origin"} <= cols
        for t in ("schedule_conflict_decisions", "weather_decisions", "weather_move_proposals"):
            assert "occurrence_date" in {r[1] for r in con.execute(f"PRAGMA table_info({t})")}
        # UNIQUE(user_id, series_event_id, occurrence_date)
        idx = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND tbl_name='schedule_occurrence_overrides' AND sql LIKE '%UNIQUE%'").fetchone()
        assert idx is not None
    finally:
        con.close()
    dn = subprocess.run([sys.executable, "-m", "alembic", "downgrade", "0015_weather_conflicts"],
                        cwd=root, env=env, capture_output=True, text=True)
    assert dn.returncode == 0, dn.stderr
    con = sqlite3.connect(dbp)
    try:
        assert not con.execute("SELECT 1 FROM sqlite_master "
                               "WHERE name='schedule_occurrence_overrides'").fetchone()
    finally:
        con.close()


# ========================================================================= #
# D — series-safety: no accidental global mutation / duplication
# ========================================================================= #
def test_moving_one_occurrence_never_duplicates_or_detaches_occurrences():
    tc, user = new_authed_client("b6safety@example.com")
    series = _weekly_series(tc)
    _overlap_before(tc, OCC_A_DATE)
    fa = _conf_on(tc, OCC_A_DATE)[0]["fingerprint"]
    tc.post(f"/api/schedule/conflicts/{fa}/accept")

    wide = tc.get(f"/api/schedule?date_from={SERIES_DATES[0]}&date_to={SERIES_WIDE_QUERY_TO}").json()
    occ = [e for e in wide["events"] if e["id"] == series["id"]]
    dates = sorted(e["occurrence_date"] for e in occ)
    assert dates == SERIES_DATES
    assert len(dates) == len(set(dates))                       # no duplicate occurrence
    # still exactly one stored series row, still recurring
    c = db_layer.get_connection()
    try:
        rows = c.execute("SELECT recurrence FROM schedule_events WHERE user_id=? AND id=?",
                         (user["id"], series["id"])).fetchall()
    finally:
        c.close()
    assert len(rows) == 1 and rows[0]["recurrence"] == "weekly"


# ========================================================================= #
# Product Completion Batch 05 — BUG B regression: a genuinely still-
# unresolved conflict must not become silently unresolvable merely because
# its date recently crossed into the past (fixed via _SC_RECUR_WINDOW_BACK_DAYS,
# the ONLY thing bounding _sc_match_conflict / the /keep /accept endpoints),
# while a conflict genuinely far in the past correctly stays out of reach —
# never unbounded, never "all historical events freely mutable".
# ========================================================================= #
def _decision_count(user_id: str) -> int:
    c = db_layer.get_connection()
    try:
        return c.execute("SELECT COUNT(*) FROM schedule_conflict_decisions WHERE user_id=?",
                         (user_id,)).fetchone()[0]
    finally:
        c.close()


def test_a_recently_past_unresolved_conflict_is_still_resolvable():
    """The exact class of bug: an unresolved overlap a few days in the past
    (well within the new grace window) must still Accept cleanly."""
    tc, user = new_authed_client("b05recentpast@example.com")
    d = (main.sched.myt_now() - dt.timedelta(days=5)).replace(
        hour=14, minute=0, second=0, microsecond=0)
    _mk(tc, "Design Review", d, d + dt.timedelta(hours=1))
    _mk(tc, "Team Sync", d + dt.timedelta(minutes=30), d + dt.timedelta(minutes=90))
    conf = _conf_on(tc, d.date().isoformat())[0]
    assert conf["conflict_type"] == "event_overlap"

    r = tc.post(f"/api/schedule/conflicts/{conf['fingerprint']}/accept")
    assert r.status_code == 200 and r.json()["scope"] == "series", r.text
    assert _decision_count(user["id"]) == 1


def test_a_conflict_far_in_the_past_still_correctly_stays_unresolvable():
    """Negative control: the fix is a bounded grace period, not an unbounded
    one — a conflict from a month ago must still 409, proving historical
    conflicts did not just become freely resolvable forever."""
    tc, user = new_authed_client("b05farpast@example.com")
    d = (main.sched.myt_now() - dt.timedelta(days=30)).replace(
        hour=14, minute=0, second=0, microsecond=0)
    _mk(tc, "Old Review", d, d + dt.timedelta(hours=1))
    _mk(tc, "Old Sync", d + dt.timedelta(minutes=30), d + dt.timedelta(minutes=90))
    # still visibly shown as unresolved when reading that specific past day...
    conf = _conf_on(tc, d.date().isoformat())[0]
    # ...but genuinely out of the resolvable grace window:
    r = tc.post(f"/api/schedule/conflicts/{conf['fingerprint']}/accept")
    assert r.status_code == 409
    assert _decision_count(user["id"]) == 0

    r2 = tc.post(f"/api/schedule/conflicts/{conf['fingerprint']}/keep")
    assert r2.status_code == 409
    assert _decision_count(user["id"]) == 0


def test_recently_past_conflict_ownership_is_still_enforced():
    tc_a, user_a = new_authed_client("b05ownera@example.com")
    tc_b, user_b = new_authed_client("b05ownerb@example.com")
    d = (main.sched.myt_now() - dt.timedelta(days=5)).replace(
        hour=14, minute=0, second=0, microsecond=0)
    _mk(tc_a, "Design Review", d, d + dt.timedelta(hours=1))
    _mk(tc_a, "Team Sync", d + dt.timedelta(minutes=30), d + dt.timedelta(minutes=90))
    fp = _conf_on(tc_a, d.date().isoformat())[0]["fingerprint"]

    assert tc_b.post(f"/api/schedule/conflicts/{fp}/accept").status_code == 409
    assert _decision_count(user_b["id"]) == 0
    # the real owner can still resolve their own conflict
    assert tc_a.post(f"/api/schedule/conflicts/{fp}/accept").status_code == 200
    assert _decision_count(user_a["id"]) == 1


def test_recently_past_conflict_fingerprint_is_stable_and_correct():
    tc, _ = new_authed_client("b05fingerprint@example.com")
    d = (main.sched.myt_now() - dt.timedelta(days=5)).replace(
        hour=14, minute=0, second=0, microsecond=0)
    _mk(tc, "Design Review", d, d + dt.timedelta(hours=1))
    _mk(tc, "Team Sync", d + dt.timedelta(minutes=30), d + dt.timedelta(minutes=90))
    fp1 = _conf_on(tc, d.date().isoformat())[0]["fingerprint"]
    fp2 = _conf_on(tc, d.date().isoformat())[0]["fingerprint"]
    assert fp1 == fp2   # unchanged data -> stable fingerprint even once "past"


def test_widened_resolution_window_does_not_change_unrelated_upcoming_conflicts():
    """No unrelated historical/upcoming schedule behavior changed: a
    same-day-as-before upcoming conflict resolves exactly as it always has."""
    tc, user = new_authed_client("b05upcoming@example.com")
    d = (main.sched.myt_now() + dt.timedelta(days=2)).replace(
        hour=14, minute=0, second=0, microsecond=0)
    _mk(tc, "Design Review", d, d + dt.timedelta(hours=1))
    _mk(tc, "Team Sync", d + dt.timedelta(minutes=30), d + dt.timedelta(minutes=90))
    conf = _conf_on(tc, d.date().isoformat())[0]
    r = tc.post(f"/api/schedule/conflicts/{conf['fingerprint']}/accept")
    assert r.status_code == 200 and r.json()["scope"] == "series"
    assert _decision_count(user["id"]) == 1
