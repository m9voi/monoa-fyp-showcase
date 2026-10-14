"""Phase 5F — Weather Intelligence Foundation.

The provider adapter is tested against MOCKED Open-Meteo (httpx.MockTransport):
this proves parsing / timezone conversion / error mapping / the contract. It
does NOT prove meteorological accuracy.

The engine (weather_core) is pure and fully unit-tested here: WMO normalisation,
window validation, deterministic aggregation, context primitives (probability is
NEVER turned into certainty), freshness, and the small bounded cache.
"""
import asyncio
import datetime as dt
import json
import os
import pathlib
import time

import httpx
import pytest

import db as db_layer
import main
import weather_core as wc
import weather_provider as wp

ROOT = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UTC = dt.timezone.utc

# KLCC — a public landmark, NOT the user's location.
KLCC = (3.1578, 101.7117)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clear_weather_cache():
    wp.cache_clear()
    yield
    wp.cache_clear()


# --------------------------------------------------------------------------- #
# synthetic Open-Meteo payload
# --------------------------------------------------------------------------- #
def _payload(*, tz="Asia/Kuala_Lumpur", offset=28800, start_utc=None, n=48,
             code=1, temp=30.0, app_temp=33.0, pp=10, precip=0.0, snow=0.0,
             wind=8.0, gust=15.0, vis=20000.0, current=True, overrides=None):
    """Build a well-formed Open-Meteo response. `start_utc` is the UTC time of the
    first hourly row; times are emitted in LOCAL naive form (start_utc + offset)."""
    start_utc = start_utc or dt.datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    local0 = start_utc + dt.timedelta(seconds=offset)
    times = [(local0 + dt.timedelta(hours=i)).replace(tzinfo=None).isoformat(timespec="minutes")
             for i in range(n)]
    hourly = {
        "time": times,
        "temperature_2m": [temp] * n,
        "apparent_temperature": [app_temp] * n,
        "precipitation_probability": [pp] * n,
        "precipitation": [precip] * n,
        "rain": [precip] * n,
        "snowfall": [snow] * n,
        "weather_code": [code] * n,
        "wind_speed_10m": [wind] * n,
        "wind_gusts_10m": [gust] * n,
        "visibility": [vis] * n,
    }
    if overrides:
        for k, arr in overrides.items():
            for i, v in arr.items():
                hourly[k][i] = v
    p = {
        "timezone": tz, "timezone_abbreviation": "+08", "utc_offset_seconds": offset,
        "hourly": hourly,
    }
    if current:
        cur_local = (start_utc + dt.timedelta(seconds=offset)).replace(tzinfo=None)
        p["current"] = {
            "time": cur_local.isoformat(timespec="minutes"),
            "temperature_2m": temp, "apparent_temperature": app_temp, "weather_code": code,
            "precipitation": precip, "rain": precip, "wind_speed_10m": wind,
            "wind_gusts_10m": gust, "is_day": 1,
        }
    return p


def _mock_provider(payload=None, *, status=200, handler=None):
    def _h(_req):
        if handler:
            return handler(_req)
        return httpx.Response(status, json=payload if payload is not None else _payload())
    return wp.OpenMeteoWeatherProvider(
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(_h), timeout=5.0)
    )


# ===================== WMO condition normalisation =====================
@pytest.mark.parametrize("code,expected", [
    (0, "clear"), (1, "mostly_clear"), (2, "partly_cloudy"), (3, "cloudy"),
    (45, "fog"), (48, "fog"), (51, "drizzle"), (55, "drizzle"),
    (56, "freezing_rain"), (61, "rain"), (63, "rain"), (65, "heavy_rain"),
    (66, "freezing_rain"), (71, "snow"), (75, "heavy_snow"), (77, "snow"),
    (80, "rain"), (82, "heavy_rain"), (85, "snow"), (86, "heavy_snow"),
    (95, "thunderstorm"), (96, "thunderstorm"), (99, "thunderstorm"),
])
def test_wmo_known_codes(code, expected):
    assert wc.wmo_to_condition(code) == expected


@pytest.mark.parametrize("code", [4, 7, 100, 999, -1, 3.5, None, True, "3", ""])
def test_wmo_unknown_code_is_unknown_never_guessed(code):
    assert wc.wmo_to_condition(code) == "unknown"


def test_condition_vocabulary_is_global_and_bounded():
    assert set(wc._WMO.values()) <= set(wc.CONDITIONS)
    assert "unknown" in wc.CONDITIONS
    for c in ("snow", "heavy_snow", "freezing_rain", "thunderstorm", "fog"):
        assert c in wc.CONDITIONS


# ===================== time helpers =====================
def test_parse_aware_iso_accepts_z_and_offset_rejects_naive():
    assert wc.parse_aware_iso("2026-09-05T12:00:00Z") == dt.datetime(2026, 9, 5, 12, tzinfo=UTC)
    assert wc.parse_aware_iso("2026-09-05T20:00:00+08:00") == dt.datetime(2026, 9, 5, 12, tzinfo=UTC)
    for bad in ("2026-09-05T20:00:00", "not-a-date", "", 123, None):
        with pytest.raises(wc.WeatherWindowError):
            wc.parse_aware_iso(bad)


def test_local_naive_to_utc_uses_provider_offset_not_server_tz():
    # 19:00 local at +08:00 -> 11:00Z, regardless of the machine's timezone
    got = wc._local_naive_to_utc("2026-09-05T19:00", 28800, "Asia/Kuala_Lumpur")
    assert got == dt.datetime(2026, 9, 5, 11, tzinfo=UTC)
    # a non-DST zone: the ZoneInfo path and the fixed-offset fallback agree
    assert wc._local_naive_to_utc("2026-09-05T19:00", 28800, None) == got


def test_dst_transition_conversion_is_correct_not_a_single_fixed_offset():
    """Open-Meteo emits a CONTINUOUS local wall-clock sequence; across a DST
    transition inside the horizon the true offset changes mid-array. The IANA
    timezone path must apply the correct offset PER INSTANT."""
    tz = "America/Santiago"          # springs forward 2026-09-06 (00:00->01:00), -04:00 -> -03:00
    off = -14400                     # the offset at request time (pre-transition)
    # before the transition: both paths agree
    assert wc._local_naive_to_utc("2026-09-05T23:00", off, tz) == dt.datetime(2026, 9, 6, 3, tzinfo=UTC)
    # AFTER the transition: DST-correct is -03:00 -> one hour earlier in UTC than
    # the fixed-offset (which would wrongly still use -04:00).
    assert wc._local_naive_to_utc("2026-09-06T12:00", off, tz) == dt.datetime(2026, 9, 6, 15, tzinfo=UTC)
    assert wc._local_naive_to_utc("2026-09-07T12:00", off, tz) == dt.datetime(2026, 9, 7, 15, tzinfo=UTC)
    # prove the OLD single-offset behaviour would have been wrong here
    assert wc._local_naive_to_utc("2026-09-06T12:00", off, None) == dt.datetime(2026, 9, 6, 16, tzinfo=UTC)


def test_forecast_window_spanning_a_dst_transition_selects_the_right_hours():
    tz, off = "America/Santiago", -14400
    times = [(dt.datetime(2026, 9, 5, 22) + dt.timedelta(hours=i)).isoformat(timespec="minutes")
             for i in range(16)]                       # continuous local 22:00 .. next-day 13:00
    payload = {
        "timezone": tz, "timezone_abbreviation": "-04", "utc_offset_seconds": off,
        "hourly": {"time": times, "temperature_2m": [15.0] * 16, "apparent_temperature": [14.0] * 16,
                   "precipitation_probability": [10] * 16, "precipitation": [0.0] * 16, "rain": [0.0] * 16,
                   "snowfall": [0] * 16, "weather_code": [1] * 16, "wind_speed_10m": [8] * 16,
                   "wind_gusts_10m": [12] * 16, "visibility": [20000] * 16},
    }
    f = wp.OpenMeteoWeatherProvider()._parse(payload)
    by_local = {r["time_local"]: r["time_utc"] for r in f["hourly"]}
    assert by_local["2026-09-05T23:00"] == dt.datetime(2026, 9, 6, 3, tzinfo=UTC)     # -04
    assert by_local["2026-09-06T12:00"] == dt.datetime(2026, 9, 6, 15, tzinfo=UTC)    # -03 (DST)
    # a window given in UTC (as the API always is) selects the correct local hours
    sel = wc.select_window_hours(f["hourly"],
                                 dt.datetime(2026, 9, 6, 14, tzinfo=UTC),
                                 dt.datetime(2026, 9, 6, 17, tzinfo=UTC))
    assert [r["time_local"] for r in sel] == ["2026-09-06T11:00", "2026-09-06T12:00", "2026-09-06T13:00"]


# ===================== window validation =====================
def test_validate_window_ok():
    s, e = wc.validate_window("2026-09-05T19:00:00+08:00", "2026-09-05T21:00:00+08:00")
    assert s == dt.datetime(2026, 9, 5, 11, tzinfo=UTC)
    assert e == dt.datetime(2026, 9, 5, 13, tzinfo=UTC)


@pytest.mark.parametrize("start,end,kind", [
    ("2026-09-05T19:00", "2026-09-05T21:00+08:00", wc.WeatherWindowError.WINDOW_NOT_TZ_AWARE),
    ("2026-09-05T21:00:00Z", "2026-09-05T19:00:00Z", wc.WeatherWindowError.WINDOW_INVERTED),
    ("2026-09-05T19:00:00Z", "2026-09-05T19:00:00Z", wc.WeatherWindowError.WINDOW_INVERTED),
    ("2026-09-05T00:00:00Z", "2026-09-07T06:00:00Z", wc.WeatherWindowError.WINDOW_TOO_LONG),
    ("garbage", "2026-09-05T19:00:00Z", wc.WeatherWindowError.WINDOW_MALFORMED),
])
def test_validate_window_rejections(start, end, kind):
    with pytest.raises(wc.WeatherWindowError) as ei:
        wc.validate_window(start, end)
    assert ei.value.kind == kind


# ===================== provider adapter (mocked Open-Meteo) =====================
def test_provider_parses_timezone_current_and_hourly():
    start = dt.datetime(2026, 9, 5, 10, tzinfo=UTC)     # 18:00 local at +08:00
    f = _run(_mock_provider(_payload(start_utc=start, n=6, code=2, temp=31.0, app_temp=34.0)).fetch(*KLCC))
    assert f["provider"] == "open-meteo"
    assert f["timezone"] == "Asia/Kuala_Lumpur" and f["utc_offset_seconds"] == 28800
    assert f["current"]["condition"] == "partly_cloudy" and f["current"]["temperature_c"] == 31.0
    assert len(f["hourly"]) == 6
    assert f["hourly"][0]["time_utc"] == start        # local 18:00 +08 -> 10:00Z
    assert f["hourly"][0]["time_local"] == "2026-09-05T18:00"
    # freshness metadata present + tz-aware
    assert wc.parse_aware_iso(f["fetched_at"]).tzinfo is not None


@pytest.mark.parametrize("payload,kind", [
    ({"error": True, "reason": "invalid coord"}, wc.WEATHER_UNAVAILABLE),
    ({"timezone": "UTC", "utc_offset_seconds": 0}, wc.WEATHER_BAD_RESPONSE),          # no hourly
    ({"timezone": "UTC", "utc_offset_seconds": 0, "hourly": {"temperature_2m": []}}, wc.WEATHER_BAD_RESPONSE),  # no hourly.time
    ({"timezone": "UTC", "utc_offset_seconds": 0, "hourly": {"time": []}}, wc.WEATHER_NO_FORECAST),  # empty
])
def test_provider_payload_errors(payload, kind):
    with pytest.raises(wc.WeatherProviderError) as ei:
        _run(_mock_provider(payload).fetch(*KLCC))
    assert ei.value.kind == kind


@pytest.mark.parametrize("status,kind", [
    (429, wc.WEATHER_RATE_LIMITED), (500, wc.WEATHER_UNAVAILABLE), (503, wc.WEATHER_UNAVAILABLE),
])
def test_provider_http_status_errors(status, kind):
    prov = _mock_provider(handler=lambda r: httpx.Response(status, text="x"))
    with pytest.raises(wc.WeatherProviderError) as ei:
        _run(prov.fetch(*KLCC))
    assert ei.value.kind == kind


def test_provider_timeout_and_network_and_badjson():
    def timeout_h(_r):
        raise httpx.ReadTimeout("slow")
    with pytest.raises(wc.WeatherProviderError) as ei:
        _run(_mock_provider(handler=timeout_h).fetch(*KLCC))
    assert ei.value.kind == wc.WEATHER_TIMEOUT

    def net_h(_r):
        raise httpx.ConnectError("no route")
    with pytest.raises(wc.WeatherProviderError) as ei:
        _run(_mock_provider(handler=net_h).fetch(*KLCC))
    assert ei.value.kind == wc.WEATHER_NETWORK

    prov = _mock_provider(handler=lambda r: httpx.Response(200, text="<<not json>>"))
    with pytest.raises(wc.WeatherProviderError) as ei:
        _run(prov.fetch(*KLCC))
    assert ei.value.kind == wc.WEATHER_BAD_RESPONSE


def test_provider_400_with_reason_maps_to_unavailable():
    prov = _mock_provider(handler=lambda r: httpx.Response(400, json={"error": True, "reason": "Latitude must be in range"}))
    with pytest.raises(wc.WeatherProviderError) as ei:
        _run(prov.fetch(*KLCC))
    assert ei.value.kind == wc.WEATHER_UNAVAILABLE


# ===================== aggregation =====================
def _rows(start_utc, specs):
    """specs: list of per-hour dicts to merge onto a baseline row."""
    out = []
    for i, s in enumerate(specs):
        base = {
            "time_utc": start_utc + dt.timedelta(hours=i), "time_local": f"h{i}",
            "temperature_c": 30.0, "apparent_temperature_c": 33.0,
            "precipitation_probability": 0.0, "precipitation_mm": 0.0, "rain_mm": 0.0,
            "snowfall_mm": 0.0, "wmo_code": 1, "wind_speed_kmh": 8.0,
            "wind_gusts_kmh": 15.0, "visibility_m": 20000.0,
        }
        base.update(s)
        out.append(base)
    return out


def test_aggregate_cross_hour_min_max_and_probability_preserved():
    s = dt.datetime(2026, 9, 5, 11, tzinfo=UTC)
    rows = _rows(s, [
        {"temperature_c": 31, "apparent_temperature_c": 38, "precipitation_probability": 20, "wmo_code": 3},
        {"temperature_c": 28, "apparent_temperature_c": 34, "precipitation_probability": 85,
         "precipitation_mm": 4.0, "wmo_code": 95, "wind_gusts_kmh": 50},
        {"temperature_c": 27, "apparent_temperature_c": 33, "precipitation_probability": 60,
         "precipitation_mm": 1.0, "wmo_code": 63},
    ])
    agg = wc.aggregate_window(rows, s, s + dt.timedelta(hours=3))
    assert agg["hours_covered"] == 3 and agg["hours_expected"] == 3 and agg["partial"] is False
    assert agg["temperature_min_c"] == 27.0 and agg["temperature_max_c"] == 31.0
    assert agg["apparent_max_c"] == 38.0
    assert agg["precipitation_probability_max"] == 85          # probability, preserved as-is
    assert agg["precipitation_mm_sum"] == 5.0
    assert agg["thunderstorm"] is True
    assert agg["dominant_condition"] == "thunderstorm"
    assert set(agg["conditions"]) == {"cloudy", "thunderstorm", "rain"}
    assert agg["wind_gusts_max_kmh"] == 50.0


def test_aggregate_partial_coverage_is_flagged_never_padded():
    s = dt.datetime(2026, 9, 5, 11, tzinfo=UTC)
    rows = _rows(s, [{}])            # only 1 hour available for a 3-hour window
    agg = wc.aggregate_window(rows, s, s + dt.timedelta(hours=3))
    assert agg["hours_covered"] == 1 and agg["hours_expected"] == 3 and agg["partial"] is True


def test_aggregate_empty_window_returns_none():
    s = dt.datetime(2026, 9, 5, 11, tzinfo=UTC)
    assert wc.aggregate_window([], s, s + dt.timedelta(hours=2)) is None


def test_select_window_hours_never_substitutes_unrelated_hours():
    s = dt.datetime(2026, 9, 5, 11, tzinfo=UTC)
    rows = _rows(s, [{}, {}, {}, {}, {}])          # h0..h4
    sel = wc.select_window_hours(rows, s + dt.timedelta(hours=1), s + dt.timedelta(hours=3))
    assert [r["time_utc"] for r in sel] == [s + dt.timedelta(hours=1), s + dt.timedelta(hours=2)]


def test_freezing_detected_from_apparent_temp_even_without_a_freezing_code():
    s = dt.datetime(2026, 1, 5, 11, tzinfo=UTC)
    rows = _rows(s, [{"apparent_temperature_c": -3, "wmo_code": 71},
                     {"apparent_temperature_c": -5, "wmo_code": 73}])
    agg = wc.aggregate_window(rows, s, s + dt.timedelta(hours=2))
    assert agg["freezing"] is True and agg["snow"] is True


# ===================== context primitives (facts, not certainty) =====================
def test_primitives_probability_is_never_certainty():
    s = dt.datetime(2026, 9, 5, 11, tzinfo=UTC)
    agg = wc.aggregate_window(_rows(s, [{"precipitation_probability": 95, "wmo_code": 61, "precipitation_mm": 0.3}]),
                              s, s + dt.timedelta(hours=1))
    prim = wc.derive_primitives(agg)
    phrases = wc.factual_phrases(agg, prim)
    assert prim["precipitation_risk"] == "high"
    blob = " ".join(phrases).lower()
    for banned in ("it will rain", "will rain", "guaranteed", "certain", "definitely", "for sure"):
        assert banned not in blob
    assert "rain likely" in phrases            # likelihood, not certainty


@pytest.mark.parametrize("pp,precip,expected", [
    (10, 0.0, "low"), (45, 0.0, "moderate"), (80, 0.0, "high"), (20, 9.0, "high"), (None, None, "unknown"),
])
def test_precipitation_risk_tiers(pp, precip, expected):
    s = dt.datetime(2026, 9, 5, 11, tzinfo=UTC)
    row = {"precipitation_probability": pp, "precipitation_mm": precip,
           "wmo_code": 61 if (pp and pp >= 40) or (precip and precip >= 0.5) else 1}
    if pp is None:
        row = {"precipitation_probability": None, "precipitation_mm": None, "wmo_code": 999}
    agg = wc.aggregate_window(_rows(s, [row]), s, s + dt.timedelta(hours=1))
    assert wc.derive_primitives(agg)["precipitation_risk"] == expected


def test_heat_cold_wind_visibility_primitives():
    s = dt.datetime(2026, 9, 5, 11, tzinfo=UTC)
    hot = wc.derive_primitives(wc.aggregate_window(
        _rows(s, [{"apparent_temperature_c": 41}]), s, s + dt.timedelta(hours=1)))
    assert hot["heat_context"] == "extreme"
    cold = wc.derive_primitives(wc.aggregate_window(
        _rows(s, [{"apparent_temperature_c": -2}]), s, s + dt.timedelta(hours=1)))
    assert cold["cold_context"] == "freezing"
    windy = wc.derive_primitives(wc.aggregate_window(
        _rows(s, [{"wind_gusts_kmh": 65}]), s, s + dt.timedelta(hours=1)))
    assert windy["wind_context"] == "strong"
    poorvis = wc.derive_primitives(wc.aggregate_window(
        _rows(s, [{"visibility_m": 500}]), s, s + dt.timedelta(hours=1)))
    assert poorvis["visibility_context"] == "poor"


def test_outdoor_context_is_factual_not_a_safety_claim():
    s = dt.datetime(2026, 9, 5, 11, tzinfo=UTC)
    agg = wc.aggregate_window(_rows(s, [{"wmo_code": 95, "precipitation_probability": 90}]),
                              s, s + dt.timedelta(hours=1))
    prim = wc.derive_primitives(agg)
    assert prim["outdoor_context"] == "poor"
    for v in prim.values():
        assert v not in ("safe", "unsafe", "dangerous", "perfect")


def test_primitives_all_unknown_when_inputs_missing():
    s = dt.datetime(2026, 9, 5, 11, tzinfo=UTC)
    agg = wc.aggregate_window(_rows(s, [{
        "temperature_c": None, "apparent_temperature_c": None, "precipitation_probability": None,
        "precipitation_mm": None, "wmo_code": 12345, "wind_speed_kmh": None,
        "wind_gusts_kmh": None, "visibility_m": None,
    }]), s, s + dt.timedelta(hours=1))
    prim = wc.derive_primitives(agg)
    assert prim["precipitation_risk"] == "unknown" and prim["heat_context"] == "unknown"
    assert prim["wind_context"] == "unknown" and prim["outdoor_context"] == "unknown"


# ===================== freshness =====================
def test_is_stale():
    now = dt.datetime(2026, 9, 5, 12, tzinfo=UTC)
    assert wc.is_stale((now - dt.timedelta(minutes=5)).isoformat(), now=now) is False
    assert wc.is_stale((now - dt.timedelta(hours=2)).isoformat(), now=now) is True
    assert wc.is_stale("not-a-timestamp", now=now) is True         # fail safe
    assert wc.is_stale(None, now=now) is True


# ===================== cache =====================
def test_cache_miss_then_hit_then_expiry_and_isolation():
    calls = {"n": 0}

    def handler(_r):
        calls["n"] += 1
        return httpx.Response(200, json=_payload(n=6))
    prov = wp.OpenMeteoWeatherProvider(
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0))

    _run(wp.get_forecast(prov, 3.1578, 101.7117))
    _run(wp.get_forecast(prov, 3.1580, 101.7119))     # same ~1.1km bucket -> HIT
    assert calls["n"] == 1
    _run(wp.get_forecast(prov, 5.0000, 100.0000))     # different location -> MISS
    assert calls["n"] == 2
    # bypass
    _run(wp.get_forecast(prov, 3.1578, 101.7117, use_cache=False))
    assert calls["n"] == 3
    # expiry
    key = wp._cache_key(prov.name, 3.1578, 101.7117)
    assert wp._cache_get(key) is not None
    assert wp._cache_get(key, now=time.time() + wc.CACHE_TTL_S + 1) is None


def test_cache_key_is_opaque_and_neither_key_nor_value_holds_coordinates():
    """In-memory only. The KEY is a hash of (provider + ~1.1 km coarsened
    coordinates) — not a coordinate, and even the coarsened bucket is not stored
    in the clear. The VALUE holds only timezone metadata + weather values: the
    normalised forecast copies through NO latitude/longitude (Open-Meteo's echoed
    coords are deliberately dropped in _parse)."""
    lat, lng = 3.15789, 101.71234
    prov = _mock_provider(_payload(n=4))
    _run(wp.get_forecast(prov, lat, lng))
    (key,) = list(wp._CACHE.keys())
    # key: opaque hex, no coordinate substring, exact coords unrecoverable
    assert isinstance(key, str) and all(c in "0123456789abcdef" for c in key)
    assert "3.15" not in key and "101.71" not in key and "3.16" not in key
    ts, forecast = wp._CACHE[key]
    assert isinstance(ts, float)
    blob = json.dumps(forecast, default=str)
    assert "latitude" not in blob and "longitude" not in blob
    assert "3.157" not in blob and "101.712" not in blob and "3.16" not in blob
    assert "user" not in blob.lower()
    # the normalised forecast top-level keys carry no coordinate field
    assert set(forecast) == {"provider", "attribution", "timezone", "timezone_abbreviation",
                             "utc_offset_seconds", "fetched_at", "current", "hourly"}
    # identical coarsened bucket -> identical key; a different bucket -> different key
    assert wp._cache_key("open-meteo", 3.1578, 101.7117) == wp._cache_key("open-meteo", 3.1601, 101.7099)
    assert wp._cache_key("open-meteo", 3.1578, 101.7117) != wp._cache_key("open-meteo", 5.0, 100.0)


# ===================== endpoint =====================
class _StubProvider:
    name = "open-meteo"
    attribution = wp.OPEN_METEO_ATTRIBUTION

    def __init__(self, payload=None, error=None):
        self._payload, self._error = payload, error

    async def fetch(self, latitude, longitude, *, horizon_hours=wc.FORECAST_HORIZON_HOURS):
        if self._error:
            raise wc.WeatherProviderError(self._error)
        return wp.OpenMeteoWeatherProvider()._parse(self._payload or _payload())


def _use(monkeypatch, stub):
    monkeypatch.setattr(main.weather_provider, "get_weather_provider", lambda *a, **k: stub)
    monkeypatch.setattr(main.weather_provider, "get_forecast",
                        lambda prov, lat, lng, **k: prov.fetch(lat, lng))


def _window(hours_from_now_start=1, duration_h=2):
    now = dt.datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    s = now + dt.timedelta(hours=hours_from_now_start)
    e = s + dt.timedelta(hours=duration_h)
    return s.isoformat(), e.isoformat()


def test_weather_context_requires_auth(client):
    assert client.post("/api/weather/context", json={"origin": {"latitude": 3.1, "longitude": 101.7}}).status_code == 401


def test_weather_context_current_only(alice, monkeypatch):
    tc, _ = alice
    start = dt.datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=1)
    _use(monkeypatch, _StubProvider(_payload(start_utc=start, code=3, temp=30.5)))
    b = tc.post("/api/weather/context", json={"origin": {"latitude": 3.1578, "longitude": 101.7117}}).json()
    assert b["status"] == "OK" and b["provider"] == "open-meteo"
    assert b["current"]["condition"] == "cloudy" and b["window"] is None
    assert b["timezone"] == "Asia/Kuala_Lumpur"                 # target location, not the server
    assert "Open-Meteo" in b["attribution"]
    assert b["fetched_at"] and b["stale"] is False


def test_weather_context_window_ok(alice, monkeypatch):
    tc, _ = alice
    start = dt.datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=1)
    payload = _payload(start_utc=start, n=48, code=95, pp=88, precip=3.0, gust=55.0)
    _use(monkeypatch, _StubProvider(payload))
    s, e = _window(1, 2)
    b = tc.post("/api/weather/context", json={
        "origin": {"latitude": 3.1578, "longitude": 101.7117}, "start_time": s, "end_time": e,
    }).json()
    assert b["status"] == "OK" and b["window"] is not None
    w = b["window"]
    assert w["precipitation_probability_max"] == 88            # probability preserved
    assert w["thunderstorm"] is True and w["dominant_condition"] == "thunderstorm"
    assert w["primitives"]["storm_risk"] == "likely"
    assert "thunderstorms likely" in w["phrases"]
    assert w["hours_covered"] >= 1 and w["hours_expected"] == 2
    # never a certainty phrase
    assert not any("will rain" in p for p in w["phrases"])


@pytest.mark.parametrize("body,code", [
    ({"origin": {"latitude": 200, "longitude": 101.7}}, 422),
    ({"origin": {"latitude": 3.1, "longitude": 999}}, 422),
    ({"origin": {"longitude": 101.7}}, 422),
    ({"origin": {"latitude": 3.1, "longitude": 101.7},
      "start_time": "2026-09-05T19:00", "end_time": "2026-09-05T21:00+08:00"}, 422),   # naive
    ({"origin": {"latitude": 3.1, "longitude": 101.7},
      "start_time": "2026-09-05T21:00:00Z", "end_time": "2026-09-05T19:00:00Z"}, 422),  # inverted
])
def test_weather_context_validation_422(alice, monkeypatch, body, code):
    tc, _ = alice
    _use(monkeypatch, _StubProvider())
    assert tc.post("/api/weather/context", json=body).status_code == code


def test_weather_context_window_out_of_horizon_is_truthful(alice, monkeypatch):
    tc, _ = alice
    start = dt.datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=1)
    _use(monkeypatch, _StubProvider(_payload(start_utc=start, n=48)))
    far = dt.datetime.now(UTC) + dt.timedelta(days=6)
    b = tc.post("/api/weather/context", json={
        "origin": {"latitude": 3.1578, "longitude": 101.7117},
        "start_time": far.isoformat(), "end_time": (far + dt.timedelta(hours=2)).isoformat(),
    }).json()
    assert b["status"] == "OK"                       # current still returned
    assert b["window"] is None and b["window_error"] == "WEATHER_NO_FORECAST"


def test_weather_context_window_in_the_past_is_unavailable_not_faked(alice, monkeypatch):
    tc, _ = alice
    start = dt.datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=1)
    _use(monkeypatch, _StubProvider(_payload(start_utc=start, n=48)))
    past = dt.datetime.now(UTC) - dt.timedelta(hours=6)
    b = tc.post("/api/weather/context", json={
        "origin": {"latitude": 3.1578, "longitude": 101.7117},
        "start_time": past.isoformat(), "end_time": (past + dt.timedelta(hours=2)).isoformat(),
    }).json()
    assert b["window"] is None and b["window_error"] == "WEATHER_NO_FORECAST"


@pytest.mark.parametrize("kind", sorted(wc.WEATHER_ERROR_KINDS))
def test_weather_context_provider_error_is_normalised(alice, monkeypatch, kind):
    tc, _ = alice
    _use(monkeypatch, _StubProvider(error=kind))
    b = tc.post("/api/weather/context", json={"origin": {"latitude": 3.1, "longitude": 101.7}}).json()
    assert b["status"] == "PROVIDER_ERROR" and b["error"] == kind and b["current"] is None


def test_weather_context_does_not_log_coordinates(alice, monkeypatch, capfd):
    tc, _ = alice
    _use(monkeypatch, _StubProvider(error=wc.WEATHER_TIMEOUT))
    lat, lng = 12.3456789, -98.7654321
    tc.post("/api/weather/context", json={"origin": {"latitude": lat, "longitude": lng}})
    out = capfd.readouterr()
    combined = out.out + out.err
    assert "12.3456789" not in combined and "98.7654321" not in combined
    assert "WEATHER_TIMEOUT" in combined                       # KIND only


def test_weather_context_is_stateless_and_touches_no_ai(alice, monkeypatch):
    tc, _ = alice
    _use(monkeypatch, _StubProvider())
    called = {"n": 0}
    monkeypatch.setattr(main.AIService, "interpret",
                        staticmethod(lambda *_a, **_k: called.__setitem__("n", called["n"] + 1)))
    c = db_layer.get_connection()
    head = c.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    tables_before = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    c.close()
    tc.post("/api/weather/context", json={"origin": {"latitude": 3.1, "longitude": 101.7}})
    c = db_layer.get_connection()
    assert c.execute("SELECT version_num FROM alembic_version").fetchone()[0] == head
    assert {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")} == tables_before
    c.close()
    assert called["n"] == 0


def test_weather_context_response_carries_no_key_or_provider_internals(alice, monkeypatch):
    tc, _ = alice
    _use(monkeypatch, _StubProvider())
    r = tc.post("/api/weather/context", json={"origin": {"latitude": 3.1, "longitude": 101.7}})
    low = r.text.lower()
    assert "aiza" not in low and "x-goog" not in low and "api_key" not in low


# ===================== frontend source guards =====================
_WL = (ROOT / "app" / "activity" / "WeatherLine.tsx").read_text(encoding="utf-8")
_ACT = (ROOT / "app" / "activity" / "page.tsx").read_text(encoding="utf-8")
_TYPES = (ROOT / "app" / "lib" / "types.ts").read_text(encoding="utf-8")
_ENV = (ROOT / ".env.example").read_text(encoding="utf-8")


def test_weather_line_has_no_fake_weather_values():
    for banned in ("26°C", "26\\u00b0", "Clear Sky", "Perfect conditions", "sunny day",
                   "It will rain", "guaranteed", "5-day forecast"):
        assert banned not in _WL
    # no hardcoded temperature literal like `31°` in JSX text
    import re
    assert not re.search(r">\s*\d{1,2}°", _WL)
    assert "2.9213" not in _WL and "101.6559" not in _WL


def test_weather_line_calls_the_real_endpoint_body_only():
    assert '"/api/weather/context"' in _WL
    assert 'method: "POST"' in _WL
    assert "start_time" in _WL and "end_time" in _WL
    assert "toISOString()" in _WL                         # tz-aware timestamps
    assert "lat=${" not in _WL and "&lat=" not in _WL and "?lat" not in _WL


def test_weather_line_truthful_states():
    assert "Checking conditions" in _WL                   # loading
    assert "Weather unavailable" in _WL                   # provider/network failure
    assert "Conditions unknown" in _WL                    # unknown WMO code
    # a stale-response / unmount guard
    assert "mounted" in _WL


def test_weather_line_shows_only_forecast_window_values_not_a_current_forecast_mix():
    # The line is labelled "next 2 hrs" -> it must render ONLY when the engine
    # returned an aggregated window, and every value must be window-derived.
    assert 'res.status === "OK" && res.window' in _WL     # gated on the window, not `current`
    assert "next 2 hrs" in _WL
    assert "state.res.window" in _WL and "w.dominant_condition" in _WL
    assert "w.temperature_c" in _WL and "w.precipitation_probability_max" in _WL
    # no current-conditions fallback for displayed values
    assert "current?.temperature_c" not in _WL and "current?.condition" not in _WL
    assert "current?.dominant_condition" not in _WL


def test_weather_line_does_not_influence_activity_ranking():
    # WeatherLine is rendered read-only, not inside the candidate list, and gets
    # only the origin — never the candidates / search state.
    assert "<WeatherLine" in _ACT
    assert "candidates" not in _WL and "activity_relevance" not in _WL
    assert "<WeatherLine" in _ACT and "origin={origin}" in _ACT
    # it sits in the results area but before/outside the `.candidates.map`
    assert _ACT.index("<WeatherLine") < _ACT.index("search.res.candidates.map")


def test_weather_is_not_a_navigation_item_or_page():
    # no /weather route, no weather nav link anywhere in app/
    for f in list((ROOT / "app").rglob("*.tsx")):
        t = f.read_text(encoding="utf-8")
        assert 'href="/weather"' not in t
    assert not (ROOT / "app" / "weather").exists()


def test_weather_types_and_env_present():
    for token in ("WeatherCondition", "WeatherContextResponse", "WeatherWindow",
                  "WeatherPrimitives", "precipitation_probability_max"):
        assert token in _TYPES
    assert "COREOS_WEATHER_PROVIDER" in _ENV and "COREOS_OPEN_METEO_URL" in _ENV
    import re
    assert not re.search(r"AIza[0-9A-Za-z_\-]{10,}", _ENV)
