"""B5 — contextual weather-aware schedule conflicts.

DETERMINISTIC tests only: the real Open-Meteo provider is replaced with a
provider-shaped normalised forecast (main.weather_provider.get_forecast is
monkeypatched). This proves the eligibility rules, the material-hazard policy,
the stable hazard identity + escalation, Keep/alternatives/Confirm exactly-once,
stale protection, ownership, rollback and provider-failure safety — NOT
meteorological accuracy (that is a separate real-provider smoke, done outside
pytest).
"""
import datetime as dt
import json
import sqlite3

import pytest

import main
import db as db_layer
import weather_core as wc
import weather_provider as wp
import weather_conflict as wxc
from conftest import new_authed_client

UTC = dt.timezone.utc
MYT = dt.timezone(dt.timedelta(hours=8))
KLCC = {"provider": "google", "provider_place_id": "ChIJ_klcc_park_b5",
        "name": "KLCC Park", "latitude": 3.1578, "longitude": 101.7117,
        "address": "Kuala Lumpur"}


@pytest.fixture(autouse=True)
def _clear_cache():
    wp.cache_clear()
    yield
    wp.cache_clear()


# --------------------------------------------------------------------------- #
# a provider-shaped NORMALISED forecast (the shape OpenMeteoWeatherProvider._parse
# returns) with per-hour control
# --------------------------------------------------------------------------- #
_BENIGN = dict(temp=30.0, app_temp=31.0, pp=10, precip=0.0, snow=0.0,
               code=1, wind=8.0, gust=15.0, vis=20000.0)
_HAZARDS = {
    "thunderstorm": dict(code=95, pp=80, precip=6.0, app_temp=31.0, gust=20.0),
    "heavy_rain":   dict(code=65, pp=85, precip=14.0, app_temp=29.0, gust=20.0),
    "heat":         dict(code=1, pp=5, precip=0.0, app_temp=37.0, gust=10.0),
    "wind":         dict(code=2, pp=10, precip=0.0, app_temp=30.0, gust=68.0),
}


def _forecast(hazard=None, *, hazard_hours=range(0, 48), fetched_minutes_ago=0, n=48):
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


@pytest.fixture()
def wxmock(monkeypatch):
    """Set the forecast every /api/schedule/weather-conflicts* call will see."""
    box = {"forecast": _forecast("thunderstorm"), "error": None}

    async def fake_get_forecast(provider, latitude, longitude, **kw):
        if box["error"] is not None:
            raise wc.WeatherProviderError(box["error"])
        return box["forecast"]

    monkeypatch.setattr(main.weather_provider, "get_forecast", fake_get_forecast)

    def _set(forecast=None, *, error=None):
        box["forecast"] = forecast if forecast is not None else box["forecast"]
        box["error"] = error
    return _set


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _iso_myt(dt_):
    return dt_.astimezone(MYT).isoformat()


def _mk_event(tc, *, start_h, dur_h=1, environment="outdoor", place=KLCC, title="Morning run"):
    now = dt.datetime.now(MYT).replace(minute=0, second=0, microsecond=0)
    s = now + dt.timedelta(hours=start_h)
    e = s + dt.timedelta(hours=dur_h)
    body = {"title": title, "start": s.isoformat(), "end": e.isoformat()}
    if environment is not None:
        body["environment"] = environment
    if place is not None:
        body["place"] = place
    r = tc.post("/api/events", json=body)
    assert r.status_code == 201, r.text
    return r.json()["event"]


def _conflicts(tc):
    r = tc.get("/api/schedule/weather-conflicts")
    assert r.status_code == 200, r.text
    return r.json()


def _event_row(uid, eid):
    c = db_layer.get_connection()
    try:
        r = c.execute("SELECT start_time, end_time, environment FROM schedule_events "
                      "WHERE id=? AND user_id=?", (eid, uid)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


def _wx_rows(uid, table="weather_decisions"):
    c = db_layer.get_connection()
    try:
        return [dict(r) for r in c.execute(f"SELECT * FROM {table} WHERE user_id=?", (uid,)).fetchall()]
    finally:
        c.close()


def _relogin(email, password="password123"):
    from fastapi.testclient import TestClient
    tc = TestClient(main.app)
    assert tc.post("/api/auth/login", json={"email": email, "password": password}).status_code == 200
    return tc


# ===================================================================== #
# structured environment
# ===================================================================== #
@pytest.mark.parametrize("env", ["indoor", "outdoor", "uncertain"])
def test_environment_valid_values_persist(env):
    tc, _ = new_authed_client(f"env_{env}@example.com")
    ev = _mk_event(tc, start_h=3, environment=env)
    assert ev["environment"] == env


def test_environment_defaults_to_uncertain_not_outdoor():
    tc, _ = new_authed_client("env_def@example.com")
    ev = _mk_event(tc, start_h=3, environment=None)
    assert ev["environment"] == "uncertain"


@pytest.mark.parametrize("bad", ["outside", "1", "yes", "both", "n/a"])
def test_environment_invalid_is_422(bad):
    tc, _ = new_authed_client("env_bad@example.com")
    now = dt.datetime.now(MYT).replace(minute=0, second=0, microsecond=0)
    r = tc.post("/api/events", json={"title": "x", "start": (now + dt.timedelta(hours=3)).isoformat(),
                                     "end": (now + dt.timedelta(hours=4)).isoformat(),
                                     "environment": bad})
    assert r.status_code == 422


@pytest.mark.parametrize("raw,want", [("OUTDOOR", "outdoor"), (" Indoor ", "indoor"), ("", "uncertain")])
def test_environment_is_case_and_space_tolerant(raw, want):
    tc, _ = new_authed_client("env_tol@example.com")
    ev = _mk_event(tc, start_h=3, environment=raw)
    assert ev["environment"] == want


# ===================================================================== #
# A — eligible outdoor event + material hazard
# ===================================================================== #
def test_A_material_outdoor_conflict_appears_and_does_not_mutate(wxmock):
    tc, u = new_authed_client("wxA@example.com")
    ev = _mk_event(tc, start_h=5, dur_h=1)
    before = _event_row(u["id"], ev["id"])

    data = _conflicts(tc)
    assert len(data["conflicts"]) == 1
    c = data["conflicts"][0]
    assert c["event_id"] == ev["id"]
    assert c["hazard_type"] == "thunderstorm"
    assert c["severity_tier"] in (2, 3)
    assert "thunderstorm" in c["headline"].lower() and "around" in c["headline"].lower()
    assert c["decision"] is None
    assert c["forecast"]["provider"] == "open-meteo" and c["forecast"]["stale"] is False
    assert data["provider_unavailable"] is False
    # detection never touches the event
    assert _event_row(u["id"], ev["id"]) == before
    assert _wx_rows(u["id"]) == []


# ===================================================================== #
# B — no conflict for indoor / uncertain / no-location / beyond-horizon / benign
# ===================================================================== #
def test_B_indoor_event_never_conflicts(wxmock):
    tc, _ = new_authed_client("wxB1@example.com")
    _mk_event(tc, start_h=5, environment="indoor")
    assert _conflicts(tc)["conflicts"] == []


def test_B_uncertain_event_never_conflicts(wxmock):
    tc, _ = new_authed_client("wxB2@example.com")
    _mk_event(tc, start_h=5, environment="uncertain")
    assert _conflicts(tc)["conflicts"] == []


def test_B_outdoor_without_location_never_conflicts(wxmock):
    tc, _ = new_authed_client("wxB3@example.com")
    _mk_event(tc, start_h=5, place=None)          # no linked place -> no coords
    assert _conflicts(tc)["conflicts"] == []


def test_B_event_beyond_forecast_horizon_never_conflicts(wxmock):
    tc, _ = new_authed_client("wxB4@example.com")
    _mk_event(tc, start_h=60, dur_h=1)            # 48h horizon
    assert _conflicts(tc)["conflicts"] == []


def test_B_benign_forecast_produces_no_conflict(wxmock):
    wxmock(_forecast(None))
    tc, _ = new_authed_client("wxB5@example.com")
    _mk_event(tc, start_h=5)
    assert _conflicts(tc)["conflicts"] == []


def test_B_moderate_rain_is_not_material(wxmock):
    # pp 45, 1mm over the window -> weather_core precipitation_risk "moderate" -> NOT a conflict
    f = _forecast(None)
    for row in f["hourly"]:
        row["precipitation_probability"] = 45
        row["precipitation_mm"] = 1.0
        row["wmo_code"] = 61
    wxmock(f)
    tc, _ = new_authed_client("wxB6@example.com")
    _mk_event(tc, start_h=5)
    assert _conflicts(tc)["conflicts"] == []


def test_B_all_day_event_is_not_weather_evaluated(wxmock):
    tc, _ = new_authed_client("wxB7@example.com")
    day = (dt.datetime.now(MYT) + dt.timedelta(days=1)).date()
    tc.post("/api/events", json={"title": "Sports day", "all_day": True,
                                 "start": f"{day}T00:00:00+08:00", "end": f"{day}T00:00:00+08:00",
                                 "environment": "outdoor", "place": KLCC})
    assert _conflicts(tc)["conflicts"] == []


# ===================================================================== #
# C — Keep persistence + immaterial suppression + escalation
# ===================================================================== #
def test_C_keep_persists_and_immaterial_refresh_does_not_resurface(wxmock):
    tc, u = new_authed_client("wxC@example.com")
    ev = _mk_event(tc, start_h=5)
    fp = _conflicts(tc)["conflicts"][0]["fingerprint"]
    before = _event_row(u["id"], ev["id"])

    r = tc.post(f"/api/schedule/weather-conflicts/{fp}/keep")
    assert r.status_code == 200 and r.json()["decision"] == "kept"
    assert _event_row(u["id"], ev["id"]) == before          # nothing changed

    d = _conflicts(tc)
    assert d["conflicts"][0]["decision"] == "kept" and d["unresolved"] == []

    # re-login -> still kept
    tc2 = _relogin("wxC@example.com")
    assert _conflicts(tc2)["unresolved"] == []

    # a forecast refresh with an immaterial numeric wobble (same tier) -> still suppressed
    f = _forecast("thunderstorm")
    for row in f["hourly"]:
        row["precipitation_probability"] = 78     # was 80; still tier 2/3 thunderstorm
    wxmock(f)
    assert _conflicts(tc)["unresolved"] == []


def test_C_strict_escalation_produces_a_new_warning(wxmock):
    # start at tier 2 (thunderstorm, pp 60 -> "likely" tier 2)
    f2 = _forecast(None)
    for row in f2["hourly"]:
        row.update({"wmo_code": 95, "precipitation_probability": 60, "precipitation_mm": 5.0})
    wxmock(f2)
    tc, _ = new_authed_client("wxCesc@example.com")
    _mk_event(tc, start_h=5)
    c1 = _conflicts(tc)["conflicts"][0]
    assert c1["severity_tier"] == 2
    tc.post(f"/api/schedule/weather-conflicts/{c1['fingerprint']}/keep")
    assert _conflicts(tc)["unresolved"] == []

    # escalate to tier 3 (pp 95)
    f3 = _forecast(None)
    for row in f3["hourly"]:
        row.update({"wmo_code": 95, "precipitation_probability": 95, "precipitation_mm": 9.0})
    wxmock(f3)
    d = _conflicts(tc)
    assert len(d["unresolved"]) == 1 and d["unresolved"][0]["severity_tier"] == 3
    assert d["unresolved"][0]["fingerprint"] != c1["fingerprint"]


def test_C_changing_environment_to_indoor_stales_the_decision(wxmock):
    tc, u = new_authed_client("wxCenv@example.com")
    ev = _mk_event(tc, start_h=5)
    fp = _conflicts(tc)["conflicts"][0]["fingerprint"]
    tc.post(f"/api/schedule/weather-conflicts/{fp}/keep")
    tc.patch(f"/api/events/{ev['id']}", json={"environment": "indoor"})
    assert _conflicts(tc)["conflicts"] == []                 # now ineligible
    assert _wx_rows(u["id"])[0]["stale_at"] is not None


def test_C_keep_is_idempotent(wxmock):
    tc, u = new_authed_client("wxCidem@example.com")
    _mk_event(tc, start_h=5)
    fp = _conflicts(tc)["conflicts"][0]["fingerprint"]
    r1 = tc.post(f"/api/schedule/weather-conflicts/{fp}/keep")
    r2 = tc.post(f"/api/schedule/weather-conflicts/{fp}/keep")
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["already"] is False and r2.json()["already"] is True
    assert len(_wx_rows(u["id"])) == 1


# ===================================================================== #
# D — Find another time (server-generated, hazard-free, no mutation)
# ===================================================================== #
def _hazard_window_only():
    """Forecast where ONLY hours 5-6 (the event) are hazardous; the rest clear."""
    return _forecast("thunderstorm", hazard_hours={5, 6})


def test_D_alternatives_are_server_generated_and_hazard_free(wxmock):
    wxmock(_hazard_window_only())
    tc, u = new_authed_client("wxD@example.com")
    ev = _mk_event(tc, start_h=5, dur_h=1)
    before = _event_row(u["id"], ev["id"])
    fp = _conflicts(tc)["conflicts"][0]["fingerprint"]

    r = tc.post(f"/api/schedule/weather-conflicts/{fp}/alternatives")
    assert r.status_code == 200
    body = r.json()
    assert body["proposal_id"] and body["candidates"]
    dur = dt.datetime.fromisoformat(before["end_time"]) - dt.datetime.fromisoformat(before["start_time"])
    for cand in body["candidates"]:
        s = dt.datetime.fromisoformat(cand["start"]); e = dt.datetime.fromisoformat(cand["end"])
        assert e - s == dur                                  # duration preserved
        assert s > dt.datetime.now(MYT)                      # not in the past
        assert "safe" not in cand["basis"].lower()           # never "completely safe"
    # nothing mutated by searching
    assert _event_row(u["id"], ev["id"]) == before
    assert _wx_rows(u["id"]) == []


def test_D_cancel_leaves_no_mutation(wxmock):
    wxmock(_hazard_window_only())
    tc, u = new_authed_client("wxDcancel@example.com")
    ev = _mk_event(tc, start_h=5)
    before = _event_row(u["id"], ev["id"])
    fp = _conflicts(tc)["conflicts"][0]["fingerprint"]
    pid = tc.post(f"/api/schedule/weather-conflicts/{fp}/alternatives").json()["proposal_id"]
    r = tc.post(f"/api/schedule/weather-conflicts/alternatives/{pid}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert _event_row(u["id"], ev["id"]) == before
    assert _wx_rows(u["id"]) == []
    # confirming a cancelled proposal fails safe
    assert tc.post(f"/api/schedule/weather-conflicts/alternatives/{pid}/confirm",
                   json={"candidate_index": 0}).status_code == 409


def test_D_candidate_avoids_a_second_event(wxmock):
    wxmock(_hazard_window_only())
    tc, u = new_authed_client("wxDbusy@example.com")
    _mk_event(tc, start_h=5, dur_h=1, title="run")
    # block the whole next 6 hours with a wall of events
    for h in range(6, 20):
        _mk_event(tc, start_h=h, dur_h=1, environment="indoor", place=None, title=f"busy{h}")
    fp = _conflicts(tc)["conflicts"][0]["fingerprint"]
    body = tc.post(f"/api/schedule/weather-conflicts/{fp}/alternatives").json()
    c = db_layer.get_connection()
    busy = c.execute("SELECT start_time, end_time FROM schedule_events WHERE user_id=? AND title LIKE 'busy%'",
                     (u["id"],)).fetchall()
    c.close()
    for cand in body["candidates"]:
        for bs, be in busy:
            assert not (cand["start"] < be and bs < cand["end"])


# ===================================================================== #
# E — Confirm move (exactly one canonical update; persists; replay-safe)
# ===================================================================== #
def test_E_confirm_moves_once_and_persists(wxmock):
    wxmock(_hazard_window_only())
    tc, u = new_authed_client("wxE@example.com")
    ev = _mk_event(tc, start_h=5, dur_h=1)
    before = _event_row(u["id"], ev["id"])
    dur = dt.datetime.fromisoformat(before["end_time"]) - dt.datetime.fromisoformat(before["start_time"])
    fp = _conflicts(tc)["conflicts"][0]["fingerprint"]
    prop = tc.post(f"/api/schedule/weather-conflicts/{fp}/alternatives").json()
    pid, cand0 = prop["proposal_id"], prop["candidates"][0]

    r1 = tc.post(f"/api/schedule/weather-conflicts/alternatives/{pid}/confirm", json={"candidate_index": 0})
    assert r1.status_code == 200 and r1.json()["decision"] == "moved" and r1.json()["already"] is False
    after = _event_row(u["id"], ev["id"])
    assert after["start_time"] == cand0["start"] and after["end_time"] == cand0["end"]
    assert dt.datetime.fromisoformat(after["end_time"]) - dt.datetime.fromisoformat(after["start_time"]) == dur

    # triple / retry -> exactly one move, one decision row
    r2 = tc.post(f"/api/schedule/weather-conflicts/alternatives/{pid}/confirm", json={"candidate_index": 0})
    r3 = tc.post(f"/api/schedule/weather-conflicts/alternatives/{pid}/confirm", json={"candidate_index": 0})
    assert r2.json()["already"] is True and r3.json()["already"] is True
    moved = [d for d in _wx_rows(u["id"]) if d["decision"] == "moved"]
    assert len(moved) == 1
    assert _event_row(u["id"], ev["id"])["start_time"] == cand0["start"]

    # the old warning is gone and stays gone across a re-login
    assert _conflicts(tc)["unresolved"] == []
    assert _relogin("wxE@example.com").get("/api/schedule/weather-conflicts").json()["unresolved"] == []


def test_E_confirm_rejects_an_out_of_range_index(wxmock):
    wxmock(_hazard_window_only())
    tc, _ = new_authed_client("wxEidx@example.com")
    _mk_event(tc, start_h=5)
    fp = _conflicts(tc)["conflicts"][0]["fingerprint"]
    pid = tc.post(f"/api/schedule/weather-conflicts/{fp}/alternatives").json()["proposal_id"]
    assert tc.post(f"/api/schedule/weather-conflicts/alternatives/{pid}/confirm",
                   json={"candidate_index": 99}).status_code == 422


# ===================================================================== #
# F — no safe candidate
# ===================================================================== #
def test_F_no_candidate_is_honest_and_non_mutating(wxmock):
    wxmock(_forecast("thunderstorm"))                 # hazardous for ALL 48 h
    tc, u = new_authed_client("wxF@example.com")
    ev = _mk_event(tc, start_h=5)
    before = _event_row(u["id"], ev["id"])
    fp = _conflicts(tc)["conflicts"][0]["fingerprint"]
    r = tc.post(f"/api/schedule/weather-conflicts/{fp}/alternatives")
    assert r.status_code == 200
    body = r.json()
    assert body["proposal_id"] is None and body["candidates"] == []
    assert "no suitable alternative" in body["message"].lower()
    assert _event_row(u["id"], ev["id"]) == before
    assert _wx_rows(u["id"]) == [] and _wx_rows(u["id"], "weather_move_proposals") == []


# ===================================================================== #
# G — stale conditions
# ===================================================================== #
def test_G_manual_edit_after_search_makes_confirm_stale(wxmock):
    wxmock(_hazard_window_only())
    tc, u = new_authed_client("wxG1@example.com")
    ev = _mk_event(tc, start_h=5, dur_h=1)
    fp = _conflicts(tc)["conflicts"][0]["fingerprint"]
    pid = tc.post(f"/api/schedule/weather-conflicts/{fp}/alternatives").json()["proposal_id"]

    now = dt.datetime.now(MYT).replace(minute=0, second=0, microsecond=0)
    manual_start = now + dt.timedelta(hours=30)
    tc.patch(f"/api/events/{ev['id']}", json={"start": manual_start.isoformat(),
                                              "end": (manual_start + dt.timedelta(hours=1)).isoformat()})
    r = tc.post(f"/api/schedule/weather-conflicts/alternatives/{pid}/confirm", json={"candidate_index": 0})
    assert r.status_code == 409
    assert _event_row(u["id"], ev["id"])["start_time"] == manual_start.isoformat()   # manual edit preserved
    assert [d for d in _wx_rows(u["id"]) if d["decision"] == "moved"] == []


def test_G_deleted_event_makes_confirm_safe(wxmock):
    wxmock(_hazard_window_only())
    tc, u = new_authed_client("wxG2@example.com")
    ev = _mk_event(tc, start_h=5)
    fp = _conflicts(tc)["conflicts"][0]["fingerprint"]
    pid = tc.post(f"/api/schedule/weather-conflicts/{fp}/alternatives").json()["proposal_id"]
    tc.delete(f"/api/events/{ev['id']}")
    assert tc.post(f"/api/schedule/weather-conflicts/alternatives/{pid}/confirm",
                   json={"candidate_index": 0}).status_code == 409
    assert _wx_rows(u["id"]) == []


def test_G_forecast_clears_after_search_makes_confirm_stale(wxmock):
    wxmock(_hazard_window_only())
    tc, u = new_authed_client("wxG3@example.com")
    _mk_event(tc, start_h=5)
    fp = _conflicts(tc)["conflicts"][0]["fingerprint"]
    pid = tc.post(f"/api/schedule/weather-conflicts/{fp}/alternatives").json()["proposal_id"]
    wxmock(_forecast(None))                            # hazard gone
    assert tc.post(f"/api/schedule/weather-conflicts/alternatives/{pid}/confirm",
                   json={"candidate_index": 0}).status_code == 409


def test_G_expired_proposal_cannot_confirm(wxmock):
    wxmock(_hazard_window_only())
    tc, u = new_authed_client("wxG4@example.com")
    _mk_event(tc, start_h=5)
    fp = _conflicts(tc)["conflicts"][0]["fingerprint"]
    pid = tc.post(f"/api/schedule/weather-conflicts/{fp}/alternatives").json()["proposal_id"]
    c = db_layer.get_connection()
    c.execute("UPDATE weather_move_proposals SET expires_at=? WHERE id=?",
              ("2000-01-01T00:00:00+00:00", pid)); c.commit(); c.close()
    assert tc.post(f"/api/schedule/weather-conflicts/alternatives/{pid}/confirm",
                   json={"candidate_index": 0}).status_code == 409


# ===================================================================== #
# H — ownership / IDOR
# ===================================================================== #
def test_H_user_b_cannot_touch_user_a_weather_conflict(wxmock):
    ac, a = new_authed_client("wxHa@example.com")
    bc, b = new_authed_client("wxHb@example.com")
    _mk_event(ac, start_h=5)
    fp_a = _conflicts(ac)["conflicts"][0]["fingerprint"]
    prop = ac.post(f"/api/schedule/weather-conflicts/{fp_a}/alternatives")
    pid_a = prop.json().get("proposal_id")

    assert _conflicts(bc)["conflicts"] == []                       # B sees nothing of A's
    assert bc.post(f"/api/schedule/weather-conflicts/{fp_a}/keep").status_code == 409
    assert bc.post(f"/api/schedule/weather-conflicts/{fp_a}/alternatives").status_code == 409
    if pid_a:
        assert bc.post(f"/api/schedule/weather-conflicts/alternatives/{pid_a}/confirm",
                       json={"candidate_index": 0}).status_code == 404
        assert bc.post(f"/api/schedule/weather-conflicts/alternatives/{pid_a}/cancel").status_code == 404
    assert _wx_rows(b["id"]) == []


@pytest.mark.parametrize("bad", ["z" * 32, "abc", "0" * 31, "0" * 33])
def test_H_garbage_fingerprint_is_rejected(wxmock, bad):
    tc, _ = new_authed_client("wxHg@example.com")
    _mk_event(tc, start_h=5)
    assert tc.post(f"/api/schedule/weather-conflicts/{bad}/keep").status_code in (404, 409)
    assert tc.post(f"/api/schedule/weather-conflicts/{bad}/alternatives").status_code in (404, 409)


# ===================================================================== #
# I — provider failure safety
# ===================================================================== #
@pytest.mark.parametrize("kind", [wc.WEATHER_TIMEOUT, wc.WEATHER_NETWORK,
                                  wc.WEATHER_BAD_RESPONSE, wc.WEATHER_UNAVAILABLE])
def test_I_provider_failure_yields_no_fake_conflict(wxmock, kind):
    tc, u = new_authed_client(f"wxI_{kind}@example.com")
    ev = _mk_event(tc, start_h=5)
    before = _event_row(u["id"], ev["id"])
    wxmock(error=kind)
    data = _conflicts(tc)
    assert data["conflicts"] == [] and data["provider_unavailable"] is True
    assert _event_row(u["id"], ev["id"]) == before
    # keep on any fp fails safe (no current hazard to match)
    assert tc.post(f"/api/schedule/weather-conflicts/{'a'*32}/keep").status_code == 409


def test_I_stale_forecast_is_not_a_conflict(wxmock):
    wxmock(_forecast("thunderstorm", fetched_minutes_ago=45))     # > 30 min = stale
    tc, _ = new_authed_client("wxIstale@example.com")
    _mk_event(tc, start_h=5)
    assert _conflicts(tc)["conflicts"] == []


def test_I_missing_hourly_interval_is_not_a_conflict(wxmock):
    f = _forecast("thunderstorm")
    f["hourly"] = f["hourly"][:2]                     # no rows covering hour 5
    wxmock(f)
    tc, _ = new_authed_client("wxImiss@example.com")
    _mk_event(tc, start_h=5)
    assert _conflicts(tc)["conflicts"] == []


# ===================================================================== #
# privacy — no raw GPS / device coords / raw forecast dump persisted
# ===================================================================== #
def test_privacy_no_device_coords_or_raw_forecast_in_persisted_rows(wxmock):
    wxmock(_hazard_window_only())
    tc, u = new_authed_client("wxpriv@example.com")
    ev = _mk_event(tc, start_h=5)
    fp = _conflicts(tc)["conflicts"][0]["fingerprint"]
    tc.post(f"/api/schedule/weather-conflicts/{fp}/keep")
    prop = tc.post(f"/api/schedule/weather-conflicts/{fp}/alternatives").json()
    dec = _wx_rows(u["id"])[0]
    blob = json.dumps(dec)
    # the PLACE's coords are on the event (as before B5); the DECISION stores none
    assert "3.1578" not in blob and "101.7117" not in blob
    ev_json = json.loads(dec["evidence_json"])
    assert "hourly" not in ev_json and "latitude" not in ev_json and "longitude" not in ev_json
    assert set(ev_json) <= {"hazard_type", "severity_tier", "values", "window", "forecast_fetched_at"}


# ===================================================================== #
# hazard-policy unit coverage (pure)
# ===================================================================== #
def _win(**agg):
    prim = wc.derive_primitives(agg)
    return {**agg, "primitives": prim}


def test_policy_thresholds_are_conservative():
    assert wxc.material_hazard(_win(apparent_max_c=33.0)) is None            # warm, not hot
    assert wxc.material_hazard(_win(apparent_max_c=36.0))["hazard_type"] == "heat"
    assert wxc.material_hazard(_win(wind_gusts_max_kmh=45.0)) is None        # windy, not severe
    assert wxc.material_hazard(_win(wind_gusts_max_kmh=65.0))["hazard_type"] == "wind"
    assert wxc.material_hazard(_win(precipitation_probability_max=45,
                                    precipitation_mm_sum=1.0)) is None       # moderate rain
    h = wxc.material_hazard(_win(precipitation_probability_max=85, precipitation_mm_sum=15.0,
                                 dominant_condition="heavy_rain", conditions=["heavy_rain"]))
    assert h["hazard_type"] == "heavy_rain" and h["severity_tier"] in (2, 3)
    assert wxc.material_hazard(None) is None
    assert wxc.material_hazard({}) is None


def test_policy_thunderstorm_outranks_everything():
    h = wxc.material_hazard(_win(thunderstorm=True, precipitation_probability_max=80,
                                 apparent_max_c=42.0, wind_gusts_max_kmh=90.0,
                                 conditions=["thunderstorm"], dominant_condition="thunderstorm"))
    assert h["hazard_type"] == "thunderstorm"


def test_fingerprint_is_deterministic_and_tier_sensitive():
    kw = dict(event_id="e1", start_iso="2026-09-12T10:00:00+08:00",
              end_iso="2026-09-12T11:00:00+08:00", environment="outdoor",
              latitude=3.1578, longitude=101.7117, hazard_type="thunderstorm")
    a = wxc.hazard_fingerprint(**kw, severity_tier=2)
    b = wxc.hazard_fingerprint(**kw, severity_tier=2)
    c = wxc.hazard_fingerprint(**kw, severity_tier=3)
    assert a == b and a != c and len(a) == 32
    # a ~1 km coordinate wobble does not change identity (2-dp bucket)
    d = wxc.hazard_fingerprint(**{**kw, "latitude": 3.1581}, severity_tier=2)
    assert d == a
