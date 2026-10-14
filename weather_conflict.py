"""Contextual weather-aware schedule conflicts — pure policy core (B5).

Sits on top of ``weather_core`` (deterministic forecast aggregation) and turns a
real forecast over a real *outdoor* event window into either:

  * ONE material hazard  (type + severity tier + concise headline + minimal
    evidence), or
  * nothing               (immaterial weather, or missing/insufficient data —
                            an honest no-conflict, never a "safe" claim).

No DB, no HTTP, no AI, no randomness. ``weather_core`` and ``schedule_core`` are
the only imports (both pure).

------------------------------------------------------------------------------
MATERIAL HAZARD POLICY  (conservative; activity-relevant; documented thresholds)
------------------------------------------------------------------------------
Evaluated against the aggregated forecast for the event's real interval. A
single, highest-priority hazard is returned so the UI shows one concise warning.

thunderstorm
  storm_risk == "likely"   -> tier 3 if precip-probability >= 70 %, else tier 2
  storm_risk == "possible"  -> tier 1
  Rationale: lightning risk during an outdoor activity is not proportional to
  probability — "possible" already warrants a heads-up. (weather_core sets
  storm_risk from WMO thunderstorm codes 95/96/99.)

heavy_rain / heavy_snow  (severe precipitation)
  weather_core precipitation_risk == "high"  (== probability >= 70 %  OR
  >= 8 mm over the window  OR dominant condition heavy_rain/heavy_snow)
    -> tier 3 when >= 20 mm over the window, or probability >= 90 % with >= 12 mm
    -> tier 2 otherwise
  "moderate" precipitation (probability >= 40 %, light rain, drizzle) is
  DELIBERATELY NOT a conflict — that is the "light rain is possible" case the
  spec excludes.

heat  (dangerous / unusually high apparent heat)
  apparent-temperature maximum over the window
    >= 40 C -> tier 3      ("very high heat")
    >= 35 C -> tier 2      ("high heat")
  Rationale: sustained outdoor exertion risk (heat exhaustion / heat stroke)
  rises sharply above ~35 C apparent; 40 C is extreme. "warm" (30-35 C) is not a
  conflict. No medical guarantee is made.

wind  (high wind / gusts)
  wind-gust maximum (falls back to sustained speed)
    >= 75 km/h -> tier 3
    >= 60 km/h -> tier 2   (~ near gale: loss of balance / wind-blown debris)
  "windy" (40-60 km/h) is not a conflict.

cold  (global completeness; will not trigger in Asia/Kuala_Lumpur)
  apparent minimum <= 0 C or weather_core "freezing" -> tier 2

Priority when several apply: thunderstorm (any tier) first, then by tier
descending, then heavy_rain > heavy_snow > heat > wind > cold.
"""
from __future__ import annotations

import datetime as _dt
import math
from typing import Optional

import weather_core as wc
import schedule_core as sched
from idempotency import request_fingerprint

ENVIRONMENTS = ("indoor", "outdoor", "uncertain")
DEFAULT_ENVIRONMENT = "uncertain"

HAZARD_TYPES = ("thunderstorm", "heavy_rain", "heavy_snow", "heat", "wind", "cold")
SEVERITY_TIERS = (1, 2, 3)

# soonest a candidate reschedule may start (a small buffer from "now")
_MOVE_LEAD_MINUTES = 60
_MYT = _dt.timezone(_dt.timedelta(hours=8))


def normalize_environment(value) -> Optional[str]:
    """A supplied environment string -> one of ENVIRONMENTS, or None if absent,
    or raise ValueError if present-but-invalid (the caller turns that into 422)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("environment must be a string")
    v = value.strip().lower()
    if v == "":
        return None
    if v not in ENVIRONMENTS:
        raise ValueError("environment must be indoor, outdoor or uncertain")
    return v


# --------------------------------------------------------------------------- #
# hazard policy
# --------------------------------------------------------------------------- #
_PRIORITY = {"thunderstorm": 0, "heavy_rain": 1, "heavy_snow": 2, "heat": 3, "wind": 4, "cold": 5}

_HEADLINE = {
    ("thunderstorm", 3): "Thunderstorms are likely during this outdoor event.",
    ("thunderstorm", 2): "Thunderstorms are likely during this outdoor event.",
    ("thunderstorm", 1): "Thunderstorms may affect this outdoor event.",
    ("heavy_rain", 3): "Heavy rain is expected during this outdoor event.",
    ("heavy_rain", 2): "Heavy rain is likely during this outdoor event.",
    ("heavy_snow", 3): "Heavy snow is expected during this outdoor event.",
    ("heavy_snow", 2): "Heavy snow is likely during this outdoor event.",
    ("heat", 3): "Very high heat is expected during this outdoor event.",
    ("heat", 2): "High heat is expected during this outdoor event.",
    ("wind", 3): "Severe wind is expected during this outdoor event.",
    ("wind", 2): "Strong wind is expected during this outdoor event.",
    ("cold", 2): "Freezing conditions are expected during this outdoor event.",
}


def _mk(htype: str, tier: int, values: dict) -> dict:
    return {
        "hazard_type": htype,
        "severity_tier": tier,
        "headline": _HEADLINE.get((htype, tier), _HEADLINE.get((htype, 2), "Severe weather is expected during this outdoor event.")),
        "values": {k: v for k, v in values.items() if v is not None},
    }


def material_hazard(window: Optional[dict]) -> Optional[dict]:
    """`window` is the aggregated forecast for the event interval — the dict
    weather_core.aggregate_window() returns, with weather_core.derive_primitives()
    under the "primitives" key. Returns one hazard dict or None (no material
    hazard / not enough data). Never raises."""
    if not isinstance(window, dict):
        return None
    prim = window.get("primitives") or {}
    pp = window.get("precipitation_probability_max")
    precip = window.get("precipitation_mm_sum")
    gust = window.get("wind_gusts_max_kmh")
    if gust is None:
        gust = window.get("wind_speed_max_kmh")
    amax = window.get("apparent_max_c")
    amin = window.get("apparent_min_c")
    is_snow = bool(window.get("snow"))

    found: list[dict] = []

    sr = prim.get("storm_risk")
    if sr == "likely":
        tier = 3 if (pp is not None and pp >= 70) else 2
        found.append(_mk("thunderstorm", tier, {"pp_max": pp, "precip_mm": precip}))
    elif sr == "possible":
        found.append(_mk("thunderstorm", 1, {"pp_max": pp}))

    if prim.get("precipitation_risk") == "high":
        if is_snow:
            found.append(_mk("heavy_snow", 2, {"snow_mm": window.get("snowfall_mm_sum")}))
        else:
            big = (precip is not None and precip >= 20.0) or (
                pp is not None and pp >= 90 and precip is not None and precip >= 12.0
            )
            found.append(_mk("heavy_rain", 3 if big else 2, {"pp_max": pp, "precip_mm": precip}))

    if amax is not None:
        if amax >= 40.0:
            found.append(_mk("heat", 3, {"apparent_max_c": amax}))
        elif amax >= 35.0:
            found.append(_mk("heat", 2, {"apparent_max_c": amax}))

    if gust is not None:
        if gust >= 75.0:
            found.append(_mk("wind", 3, {"gust_kmh": gust}))
        elif gust >= 60.0:
            found.append(_mk("wind", 2, {"gust_kmh": gust}))

    if (amin is not None and amin <= 0.0) or window.get("freezing"):
        found.append(_mk("cold", 2, {"apparent_min_c": amin}))

    if not found:
        return None
    found.sort(key=lambda h: (
        0 if h["hazard_type"] == "thunderstorm" else 1,
        -h["severity_tier"],
        _PRIORITY.get(h["hazard_type"], 9),
    ))
    return found[0]


# --------------------------------------------------------------------------- #
# stable hazard identity
# --------------------------------------------------------------------------- #
def hazard_fingerprint(
    *, event_id: str, start_iso: str, end_iso: str, environment: str,
    latitude: float, longitude: float, hazard_type: str, severity_tier: int,
    occurrence_date: Optional[str] = None,
) -> str:
    """Deterministic identity for a material weather hazard on one event.

    Built from decision-critical data ONLY — event id, its tz-normalized
    start/end, its environment, its place location coarsened to ~1 km (2 dp),
    the hazard type, and the severity TIER. Raw forecast decimals are never
    included, so an immaterial forecast refresh keeps the same fingerprint;
    a strictly higher tier (or a different, higher-priority hazard) produces a
    new fingerprint and a fresh warning.

    ``occurrence_date`` (B6) binds the identity to ONE occurrence of a recurring
    outdoor series. It is omitted from the hash for a non-recurring event, so
    those fingerprints are byte-for-byte identical to their pre-B6 value.
    """
    s = wc.parse_aware_iso(start_iso).isoformat()
    e = wc.parse_aware_iso(end_iso).isoformat()
    fields = {
        "t": "weather",
        "event_id": event_id,
        "start": s,
        "end": e,
        "env": environment,
        "lat": round(float(latitude), 2),
        "lng": round(float(longitude), 2),
        "hazard": hazard_type,
        "tier": int(severity_tier),
    }
    if occurrence_date:
        fields["occ"] = str(occurrence_date)
    return request_fingerprint(fields)


def tier_is_escalation(prior_tier: int, new_hazard: dict) -> bool:
    """A kept decision only stops suppressing when a later forecast raises the
    hazard to a STRICTLY higher tier (documented escalation rule)."""
    return int(new_hazard.get("severity_tier", 0)) > int(prior_tier)


# --------------------------------------------------------------------------- #
# candidate reschedule slots  ("Find another time")
# --------------------------------------------------------------------------- #
def _fmt_local(dt_utc: _dt.datetime) -> str:
    return dt_utc.astimezone(_MYT).isoformat()


_WD = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MON = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _clock(x: _dt.datetime) -> str:
    h = x.hour % 12 or 12
    ap = "am" if x.hour < 12 else "pm"
    return f"{h}:{x.minute:02d} {ap}" if x.minute else f"{h} {ap}"


def _summary(dt_utc: _dt.datetime, end_utc: _dt.datetime) -> str:
    a = dt_utc.astimezone(_MYT)
    b = end_utc.astimezone(_MYT)
    return f"{_WD[a.weekday()]} {a.day} {_MON[a.month - 1]}, {_clock(a)} – {_clock(b)}"


def propose_slots(
    *,
    duration: _dt.timedelta,
    hourly_rows: list[dict],
    busy_intervals: list[tuple[str, str]],
    now_utc: _dt.datetime,
    horizon_end_utc: _dt.datetime,
    max_slots: int = 3,
) -> list[dict]:
    """Real, forecast-checked candidate windows that:
      * start at least _MOVE_LEAD_MINUTES from now and are not in the past;
      * end within the provider forecast horizon;
      * preserve the event's exact duration;
      * do NOT overlap any interval in `busy_intervals` (the owner's other timed
        events);
      * carry NO material hazard under the SAME policy as `material_hazard`.
    Returns up to `max_slots`, soonest first. Empty list -> no safe alternative.
    Never fabricates a slot and never describes one as fully safe.
    """
    if duration <= _dt.timedelta(0):
        return []
    earliest = now_utc + _dt.timedelta(minutes=_MOVE_LEAD_MINUTES)
    # align to the next half hour
    minute = 0 if earliest.minute == 0 else (30 if earliest.minute <= 30 else 60)
    start = earliest.replace(minute=0, second=0, microsecond=0) + _dt.timedelta(minutes=minute)

    def _overlaps_busy(s: _dt.datetime, e: _dt.datetime) -> bool:
        si, ei = s.isoformat(), e.isoformat()
        for bs, be in busy_intervals:
            if sched.intervals_overlap(si, ei, bs, be):
                return True
        return False

    out: list[dict] = []
    step = _dt.timedelta(minutes=30)
    cur = start
    guard = 0
    while cur + duration <= horizon_end_utc and len(out) < max_slots and guard < 240:
        guard += 1
        s_utc, e_utc = cur, cur + duration
        cur = cur + step
        if s_utc < earliest:
            continue
        if _overlaps_busy(s_utc, e_utc):
            continue
        sel = wc.select_window_hours(hourly_rows, s_utc, e_utc)
        agg = wc.aggregate_window(sel, s_utc, e_utc)
        if agg is None:
            continue  # no forecast for that window -> cannot verify -> skip
        prim = wc.derive_primitives(agg)
        if material_hazard({**agg, "primitives": prim}) is not None:
            continue
        out.append({
            "start": _fmt_local(s_utc),
            "end": _fmt_local(e_utc),
            "summary": _summary(s_utc, e_utc),
            "basis": "Lower weather risk based on the current forecast.",
        })
    return out
