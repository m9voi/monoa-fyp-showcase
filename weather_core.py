"""CoreOS — weather core (Phase 5F: Weather Intelligence Foundation).

Provider-neutral, deterministic, timezone-correct primitives that turn a raw
forecast into a *structured weather context* CoreOS Intelligence can consume.

    weather provider  ->  weather_provider.py  (HTTP, provider-specific)
                             |
                             v   raw hourly/current rows + tz metadata
    weather_core.py   ->  normalise conditions, validate windows, aggregate a
                          time window, derive deterministic context primitives,
                          track forecast freshness.

Design rules
------------
* Nothing here invents a weather value. Missing provider data stays missing;
  an unrecognised provider code becomes ``"unknown"`` — never a guess.
* Probability is preserved as probability. This module NEVER converts a
  precipitation probability into "it will rain".
* CURRENT conditions and a future FORECAST window are distinct concepts and are
  never conflated.
* All time reasoning is explicit UTC internally; the target location's timezone
  metadata is preserved so a later Schedule integration can present local time.
* No I/O, no randomness, no LLM. Every function is unit-testable.
"""
from __future__ import annotations

import datetime as _dt
import math
from typing import Any, Optional

try:                                    # stdlib (Python 3.9+); needs the OS tz db or `tzdata`
    from zoneinfo import ZoneInfo
    _HAS_ZONEINFO = True
except Exception:                       # pragma: no cover - extremely old / stripped runtime
    ZoneInfo = None                     # type: ignore
    _HAS_ZONEINFO = False

_ZONE_CACHE: "dict[str, Any]" = {}


def _zone(tz_name: Optional[str]):
    """Resolve an IANA timezone name to a tzinfo, or None. Cached; never raises."""
    if not tz_name or not _HAS_ZONEINFO:
        return None
    if tz_name in _ZONE_CACHE:
        return _ZONE_CACHE[tz_name]
    try:
        z = ZoneInfo(tz_name)
    except Exception:
        z = None
    _ZONE_CACHE[tz_name] = z
    return z

# --------------------------------------------------------------------------- #
# policy constants  (documented; deterministic; testable)
# --------------------------------------------------------------------------- #
WEATHER_TIMEOUT_S = 10.0
FORECAST_HORIZON_HOURS = 48          # how far ahead we request / support
MAX_WINDOW_HOURS = 24               # a single "event window" longer than this is rejected
WEATHER_MAX_AGE_S = 30 * 60         # a fetched forecast older than this is "stale"
CACHE_TTL_S = 10 * 60              # server-side in-memory forecast cache TTL
CACHE_MAX_ENTRIES = 64
CACHE_COORD_DECIMALS = 2           # ~1.1 km buckets — privacy-preserving + cache-friendly

# --------------------------------------------------------------------------- #
# provider failure taxonomy
# --------------------------------------------------------------------------- #
WEATHER_TIMEOUT = "WEATHER_TIMEOUT"
WEATHER_NETWORK = "WEATHER_NETWORK"
WEATHER_UNAVAILABLE = "WEATHER_UNAVAILABLE"
WEATHER_RATE_LIMITED = "WEATHER_RATE_LIMITED"
WEATHER_BAD_RESPONSE = "WEATHER_BAD_RESPONSE"
WEATHER_NO_FORECAST = "WEATHER_NO_FORECAST"

WEATHER_ERROR_KINDS = frozenset({
    WEATHER_TIMEOUT, WEATHER_NETWORK, WEATHER_UNAVAILABLE,
    WEATHER_RATE_LIMITED, WEATHER_BAD_RESPONSE, WEATHER_NO_FORECAST,
})


class WeatherProviderError(Exception):
    """Raised by a weather provider adapter. ``kind`` is one of
    WEATHER_ERROR_KINDS. The message is for server logs only and must never
    contain coordinates."""

    def __init__(self, kind: str, detail: str = "") -> None:
        if kind not in WEATHER_ERROR_KINDS:
            kind = WEATHER_UNAVAILABLE
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind


class WeatherWindowError(Exception):
    """The requested forecast window is malformed or unreasonable (client error).
    ``kind`` is one of the WINDOW_* strings below. Never contains coordinates."""

    WINDOW_MALFORMED = "WINDOW_MALFORMED"
    WINDOW_INVERTED = "WINDOW_INVERTED"
    WINDOW_TOO_LONG = "WINDOW_TOO_LONG"
    WINDOW_NOT_TZ_AWARE = "WINDOW_NOT_TZ_AWARE"

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind


# --------------------------------------------------------------------------- #
# normalised weather-condition vocabulary  (provider-neutral, global)
# --------------------------------------------------------------------------- #
CONDITIONS = (
    "clear", "mostly_clear", "partly_cloudy", "cloudy", "fog",
    "drizzle", "rain", "heavy_rain", "freezing_rain",
    "snow", "heavy_snow", "thunderstorm", "unknown",
)

# severity order for choosing a "dominant" condition in a window
_SEVERITY = {c: i for i, c in enumerate((
    "clear", "mostly_clear", "partly_cloudy", "cloudy", "fog",
    "drizzle", "freezing_rain", "rain", "snow", "heavy_rain", "heavy_snow",
    "thunderstorm", "unknown",
))}

# WMO 4677 code -> CoreOS condition. Codes Open-Meteo can emit; anything else
# becomes "unknown" (the raw code is preserved separately).
_WMO: dict[int, str] = {
    0: "clear",
    1: "mostly_clear", 2: "partly_cloudy", 3: "cloudy",
    45: "fog", 48: "fog",
    51: "drizzle", 53: "drizzle", 55: "drizzle",
    56: "freezing_rain", 57: "freezing_rain",
    61: "rain", 63: "rain", 65: "heavy_rain",
    66: "freezing_rain", 67: "freezing_rain",
    71: "snow", 73: "snow", 75: "heavy_snow", 77: "snow",
    80: "rain", 81: "rain", 82: "heavy_rain",
    85: "snow", 86: "heavy_snow",
    95: "thunderstorm", 96: "thunderstorm", 99: "thunderstorm",
}

_FREEZING_CODES = frozenset({56, 57, 66, 67})
_SNOW_CODES = frozenset({71, 73, 75, 77, 85, 86})
_THUNDER_CODES = frozenset({95, 96, 99})


def wmo_to_condition(code: Any) -> str:
    """Map a raw WMO weather code to a CoreOS condition. WMO codes are integers;
    anything else (a non-integer float, a string, None, a bool) or an
    unrecognised integer -> ``"unknown"`` (never a guessed condition)."""
    if isinstance(code, bool):
        return "unknown"
    if isinstance(code, float):
        if not code.is_integer():
            return "unknown"
        code = int(code)
    if not isinstance(code, int):
        return "unknown"
    return _WMO.get(code, "unknown")


# --------------------------------------------------------------------------- #
# time helpers  (explicit UTC; no naive assumptions)
# --------------------------------------------------------------------------- #
def parse_aware_iso(value: Any) -> _dt.datetime:
    """Parse an ISO-8601 timestamp that MUST carry a timezone (offset or 'Z').
    Returns a tz-aware UTC datetime. Raises WeatherWindowError otherwise."""
    if not isinstance(value, str) or not value.strip():
        raise WeatherWindowError(WeatherWindowError.WINDOW_MALFORMED, "not a string")
    s = value.strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    try:
        dt = _dt.datetime.fromisoformat(s)
    except ValueError:
        raise WeatherWindowError(WeatherWindowError.WINDOW_MALFORMED, "unparseable") from None
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise WeatherWindowError(WeatherWindowError.WINDOW_NOT_TZ_AWARE, "no offset")
    return dt.astimezone(_dt.timezone.utc)


def _local_naive_to_utc(
    local_iso: str, utc_offset_seconds: int, tz_name: Optional[str] = None,
) -> Optional[_dt.datetime]:
    """Open-Meteo emits ``time`` values as a CONTINUOUS local wall-clock sequence
    for the TARGET LOCATION. Across a DST transition inside the forecast horizon
    the true UTC offset changes mid-array, so the response's single
    ``utc_offset_seconds`` (the offset at request time) is NOT correct for every
    row.

    Preferred path: attach the returned IANA timezone (``tz_name``) via
    ``zoneinfo`` and convert per instant — this resolves DST correctly. An
    imaginary local time (spring-forward gap) or an ambiguous one (fall-back
    repeat) resolves deterministically with ``fold=0`` (the earlier instant).

    Fallback (no ``tz_name``, or the zone can't be loaded — e.g. no ``tzdata``):
    the single fixed offset. This is exact for non-DST zones and off by the DST
    delta only for DST zones on a stripped runtime.
    """
    try:
        naive = _dt.datetime.fromisoformat(local_iso.strip())
    except (ValueError, AttributeError):
        return None
    if naive.tzinfo is not None:
        return naive.astimezone(_dt.timezone.utc)
    zi = _zone(tz_name)
    if zi is not None:
        return naive.replace(tzinfo=zi, fold=0).astimezone(_dt.timezone.utc)
    return (naive - _dt.timedelta(seconds=int(utc_offset_seconds))).replace(tzinfo=_dt.timezone.utc)


def now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


# --------------------------------------------------------------------------- #
# window validation
# --------------------------------------------------------------------------- #
def validate_window(
    start_raw: Any, end_raw: Any, *,
    now: Optional[_dt.datetime] = None,
    horizon_hours: int = FORECAST_HORIZON_HOURS,
    max_window_hours: int = MAX_WINDOW_HOURS,
) -> tuple[_dt.datetime, _dt.datetime]:
    """Return (start_utc, end_utc) or raise WeatherWindowError.

    Rules: both timestamps tz-aware; end > start; duration <= max_window_hours.
    (Whether the window falls inside the provider's forecast horizon is a
    SEPARATE, truthful check done after the fetch — see select_window_hours.)
    """
    start = parse_aware_iso(start_raw)
    end = parse_aware_iso(end_raw)
    if end <= start:
        raise WeatherWindowError(WeatherWindowError.WINDOW_INVERTED, "end <= start")
    if (end - start) > _dt.timedelta(hours=max_window_hours):
        raise WeatherWindowError(WeatherWindowError.WINDOW_TOO_LONG,
                                 f"> {max_window_hours}h")
    return start, end


# --------------------------------------------------------------------------- #
# hourly-row selection + aggregation
# --------------------------------------------------------------------------- #
def select_window_hours(
    hourly_rows: list[dict], start_utc: _dt.datetime, end_utc: _dt.datetime,
) -> list[dict]:
    """Return the hourly rows whose timestamp falls in [start, end).
    ``hourly_rows`` items carry an already-computed ``time_utc`` datetime.
    Never fabricates a missing hour."""
    out = []
    for r in hourly_rows:
        t = r.get("time_utc")
        if isinstance(t, _dt.datetime) and start_utc <= t < end_utc:
            out.append(r)
    return out


def _nums(rows: list[dict], key: str) -> list[float]:
    vals = []
    for r in rows:
        v = r.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)):
            vals.append(float(v))
    return vals


def _round1(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(x, 1)


def aggregate_window(
    rows: list[dict], start_utc: _dt.datetime, end_utc: _dt.datetime,
) -> Optional[dict]:
    """Deterministic aggregation over the hourly rows in a window. Returns None
    when there is NOTHING to aggregate (caller reports an unavailable window —
    it must never substitute unrelated hours)."""
    if not rows:
        return None

    expected = max(1, math.ceil((end_utc - start_utc).total_seconds() / 3600.0))
    temps = _nums(rows, "temperature_c")
    app = _nums(rows, "apparent_temperature_c")
    pp = _nums(rows, "precipitation_probability")
    precip = _nums(rows, "precipitation_mm")
    snow = _nums(rows, "snowfall_mm")
    wind = _nums(rows, "wind_speed_kmh")
    gust = _nums(rows, "wind_gusts_kmh")
    vis = _nums(rows, "visibility_m")
    codes = [int(r["wmo_code"]) for r in rows
             if isinstance(r.get("wmo_code"), (int, float)) and not isinstance(r.get("wmo_code"), bool)]

    conds = []
    seen: set[str] = set()
    for c in codes:
        name = _WMO.get(c, "unknown")
        if name not in seen:
            seen.add(name)
            conds.append(name)
    conds.sort(key=lambda n: _SEVERITY.get(n, 99))
    dominant = max(conds, key=lambda n: _SEVERITY.get(n, 99)) if conds else "unknown"

    mean = lambda xs: (sum(xs) / len(xs)) if xs else None

    return {
        "hours_covered": len(rows),
        "hours_expected": expected,
        "partial": len(rows) < expected,
        "temperature_c": _round1(mean(temps)),
        "temperature_min_c": _round1(min(temps)) if temps else None,
        "temperature_max_c": _round1(max(temps)) if temps else None,
        "apparent_temperature_c": _round1(mean(app)),
        "apparent_min_c": _round1(min(app)) if app else None,
        "apparent_max_c": _round1(max(app)) if app else None,
        "precipitation_probability_max": (int(round(max(pp))) if pp else None),
        "precipitation_mm_sum": (_round1(sum(precip)) if precip else None),
        "snowfall_mm_sum": (_round1(sum(snow)) if snow else None),
        "wind_speed_max_kmh": (_round1(max(wind)) if wind else None),
        "wind_gusts_max_kmh": (_round1(max(gust)) if gust else None),
        "visibility_min_m": (int(round(min(vis))) if vis else None),
        "conditions": conds,
        "dominant_condition": dominant,
        "thunderstorm": any(c in _THUNDER_CODES for c in codes),
        "freezing": (
            any(c in _FREEZING_CODES for c in codes)
            or (bool(app) and min(app) <= 0.0)
        ),
        "snow": any(c in _SNOW_CODES for c in codes) or dominant in ("snow", "heavy_snow"),
    }


# --------------------------------------------------------------------------- #
# deterministic weather-context primitives  (facts, not safety/medical claims)
# --------------------------------------------------------------------------- #
# Documented global thresholds. These describe CONDITIONS, not suitability for
# any specific activity (that is Activity/Schedule Intelligence's job later).
_HEAT_WARM_C, _HEAT_HOT_C, _HEAT_EXTREME_C = 30.0, 35.0, 40.0        # apparent max
_COLD_C, _FREEZING_C = 5.0, 0.0                                     # apparent min
_WIND_BREEZY, _WIND_WINDY, _WIND_STRONG = 20.0, 40.0, 60.0         # gust km/h (fallback: speed)
_VIS_REDUCED_M, _VIS_POOR_M = 5000.0, 1000.0
_PP_MODERATE, _PP_HIGH = 40, 70                                    # precipitation probability %
_PRECIP_LIGHT_MM, _PRECIP_HEAVY_MM = 0.5, 8.0                     # over the window


def derive_primitives(agg: dict) -> dict:
    """Turn an aggregated window into deterministic, factual context primitives.
    Every primitive can be ``"unknown"`` when the inputs are missing."""
    pp = agg.get("precipitation_probability_max")
    precip = agg.get("precipitation_mm_sum")
    conds = set(agg.get("conditions") or [])
    dominant = agg.get("dominant_condition") or "unknown"

    # precipitation risk — probability AND amount; probability stays probability
    if pp is None and precip is None and dominant == "unknown":
        precipitation_risk = "unknown"
    elif (pp is not None and pp >= _PP_HIGH) or (precip is not None and precip >= _PRECIP_HEAVY_MM) \
            or dominant in ("heavy_rain", "heavy_snow"):
        precipitation_risk = "high"
    elif (pp is not None and pp >= _PP_MODERATE) or (precip is not None and precip >= _PRECIP_LIGHT_MM) \
            or dominant in ("rain", "drizzle", "snow", "freezing_rain"):
        precipitation_risk = "moderate"
    else:
        precipitation_risk = "low"

    # storm risk
    if agg.get("thunderstorm"):
        storm_risk = "likely" if (pp is not None and pp >= _PP_MODERATE) else "possible"
    elif dominant == "unknown" and pp is None:
        storm_risk = "unknown"
    else:
        storm_risk = "none"

    # heat / cold — from apparent temperature extremes
    amax = agg.get("apparent_max_c")
    amin = agg.get("apparent_min_c")
    if amax is None:
        heat_context = "unknown"
    elif amax >= _HEAT_EXTREME_C:
        heat_context = "extreme"
    elif amax >= _HEAT_HOT_C:
        heat_context = "hot"
    elif amax >= _HEAT_WARM_C:
        heat_context = "warm"
    else:
        heat_context = "normal"
    if amin is None:
        cold_context = "unknown"
    elif amin <= _FREEZING_C or agg.get("freezing"):
        cold_context = "freezing"
    elif amin <= _COLD_C:
        cold_context = "cold"
    else:
        cold_context = "normal"

    # wind — prefer gusts, fall back to sustained speed
    w = agg.get("wind_gusts_max_kmh")
    if w is None:
        w = agg.get("wind_speed_max_kmh")
    if w is None:
        wind_context = "unknown"
    elif w >= _WIND_STRONG:
        wind_context = "strong"
    elif w >= _WIND_WINDY:
        wind_context = "windy"
    elif w >= _WIND_BREEZY:
        wind_context = "breezy"
    else:
        wind_context = "calm"

    # visibility (only meaningful when the field was requested/returned)
    v = agg.get("visibility_min_m")
    if v is None:
        visibility_context = "unknown"
    elif v < _VIS_POOR_M:
        visibility_context = "poor"
    elif v < _VIS_REDUCED_M:
        visibility_context = "reduced"
    else:
        visibility_context = "normal"

    # general outdoor context — a factual roll-up. NEVER "safe"/"unsafe".
    poor = (
        storm_risk == "likely" or precipitation_risk == "high"
        or heat_context == "extreme" or cold_context == "freezing"
        or visibility_context == "poor" or "heavy_rain" in conds or "heavy_snow" in conds
    )
    caution = (
        storm_risk == "possible" or precipitation_risk == "moderate"
        or heat_context == "hot" or cold_context == "cold"
        or wind_context in ("windy", "strong") or visibility_context == "reduced"
    )
    if precipitation_risk == "unknown" and heat_context == "unknown" and wind_context == "unknown":
        outdoor_context = "unknown"
    elif poor:
        outdoor_context = "poor"
    elif caution:
        outdoor_context = "caution"
    else:
        outdoor_context = "normal"

    return {
        "precipitation_risk": precipitation_risk,
        "storm_risk": storm_risk,
        "heat_context": heat_context,
        "cold_context": cold_context,
        "wind_context": wind_context,
        "visibility_context": visibility_context,
        "outdoor_context": outdoor_context,
    }


def factual_phrases(agg: dict, prim: dict) -> list[str]:
    """Short, factual, non-alarmist context phrases. No "safe/unsafe/dangerous",
    no "perfect weather". Probability is spoken as likelihood, never certainty."""
    out: list[str] = []
    if prim["storm_risk"] == "likely":
        out.append("thunderstorms likely")
    elif prim["storm_risk"] == "possible":
        out.append("thunderstorms possible")
    if prim["precipitation_risk"] == "high":
        out.append("rain likely" if not agg.get("snow") else "snow likely")
    elif prim["precipitation_risk"] == "moderate":
        out.append("rain possible" if not agg.get("snow") else "snow possible")
    if prim["heat_context"] == "extreme":
        out.append("very high heat")
    elif prim["heat_context"] == "hot":
        out.append("high heat")
    if prim["cold_context"] == "freezing":
        out.append("freezing conditions")
    elif prim["cold_context"] == "cold":
        out.append("cold conditions")
    if prim["wind_context"] == "strong":
        out.append("strong wind")
    elif prim["wind_context"] == "windy":
        out.append("windy")
    if prim["visibility_context"] == "poor":
        out.append("poor visibility")
    elif prim["visibility_context"] == "reduced":
        out.append("reduced visibility")
    return out


# --------------------------------------------------------------------------- #
# freshness
# --------------------------------------------------------------------------- #
def is_stale(fetched_at: Any, *, now: Optional[_dt.datetime] = None,
             max_age_s: int = WEATHER_MAX_AGE_S) -> bool:
    """True when a forecast fetched at ``fetched_at`` (ISO-8601, tz-aware) is
    older than the freshness policy. A missing / unparseable timestamp is
    treated as stale (fail safe)."""
    now = now or now_utc()
    try:
        f = parse_aware_iso(fetched_at)
    except WeatherWindowError:
        return True
    return (now - f).total_seconds() > max_age_s
