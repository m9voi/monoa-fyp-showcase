"""CoreOS — weather provider abstraction (Phase 5F).

One backend boundary for real forecast data. Everything downstream operates on
the normalised rows from this module, never a raw provider payload.

Provider
--------
* OpenMeteoWeatherProvider — https://open-meteo.com. FREE, no API key for
  non-commercial use, WMO weather codes, timezone-aware responses. Attribution:
  "Weather data by Open-Meteo.com" (CC BY 4.0).

Selection: COREOS_WEATHER_PROVIDER = open-meteo (default; only provider today).

Privacy: coordinates are sent to the provider only (never logged here, never in
a raised message, never to an AI). One context request = at most one upstream
call (a small in-memory cache absorbs repeats). Bounded timeout, no retry loop.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Optional

import httpx

import weather_core as wc

_USER_AGENT = "CoreOS/5F (weather context; +https://coreos.local; contact: coreos-dev@localhost)"
OPEN_METEO_URL = os.getenv("COREOS_OPEN_METEO_URL", "https://api.open-meteo.com/v1/forecast").strip()
OPEN_METEO_ATTRIBUTION = "Weather data by Open-Meteo.com"

# Only the fields CoreOS decisions actually use.
_CURRENT_FIELDS = ",".join([
    "temperature_2m", "apparent_temperature", "weather_code", "precipitation",
    "rain", "wind_speed_10m", "wind_gusts_10m", "is_day",
])
_HOURLY_FIELDS = ",".join([
    "temperature_2m", "apparent_temperature", "precipitation_probability",
    "precipitation", "rain", "snowfall", "weather_code",
    "wind_speed_10m", "wind_gusts_10m", "visibility",
])


def _client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout),
                             headers={"User-Agent": _USER_AGENT}, follow_redirects=True)


class WeatherProvider:
    name: str = "none"
    attribution: str = ""

    async def fetch(self, latitude: float, longitude: float, *,
                    horizon_hours: int = wc.FORECAST_HORIZON_HOURS) -> dict:
        """Return a normalised forecast dict (see OpenMeteoWeatherProvider._parse
        for the shape). Raise weather_core.WeatherProviderError on any failure."""
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Open-Meteo
# --------------------------------------------------------------------------- #
class OpenMeteoWeatherProvider(WeatherProvider):
    name = "open-meteo"
    attribution = OPEN_METEO_ATTRIBUTION

    def __init__(self, *, url: str = OPEN_METEO_URL, client_factory=None) -> None:
        self._url = url
        self._client_factory = client_factory or (lambda: _client(wc.WEATHER_TIMEOUT_S))

    def is_configured(self) -> bool:
        return bool(self._url)

    # -- parsing -------------------------------------------------------------
    def _parse(self, payload: Any) -> dict:
        if not isinstance(payload, dict):
            raise wc.WeatherProviderError(wc.WEATHER_BAD_RESPONSE, "not an object")
        if payload.get("error"):
            raise wc.WeatherProviderError(wc.WEATHER_UNAVAILABLE, str(payload.get("reason") or "error"))

        tz = str(payload.get("timezone") or "UTC")
        tz_abbr = str(payload.get("timezone_abbreviation") or "")
        try:
            offset = int(payload.get("utc_offset_seconds") or 0)
        except (TypeError, ValueError):
            offset = 0

        cur_raw = payload.get("current") or {}
        current = None
        if isinstance(cur_raw, dict) and cur_raw:
            code = cur_raw.get("weather_code")
            observed_utc = wc._local_naive_to_utc(str(cur_raw.get("time") or ""), offset, tz)
            current = {
                "temperature_c": _num(cur_raw.get("temperature_2m")),
                "apparent_temperature_c": _num(cur_raw.get("apparent_temperature")),
                "wmo_code": _int_or_none(code),
                "condition": wc.wmo_to_condition(code),
                "precipitation_mm": _num(cur_raw.get("precipitation")),
                "rain_mm": _num(cur_raw.get("rain")),
                "wind_speed_kmh": _num(cur_raw.get("wind_speed_10m")),
                "wind_gusts_kmh": _num(cur_raw.get("wind_gusts_10m")),
                "is_day": (bool(cur_raw.get("is_day")) if cur_raw.get("is_day") is not None else None),
                "observed_at": (observed_utc.isoformat() if observed_utc else None),
            }

        hr = payload.get("hourly") or {}
        if not isinstance(hr, dict) or not isinstance(hr.get("time"), list):
            raise wc.WeatherProviderError(wc.WEATHER_BAD_RESPONSE, "no hourly.time")
        times = hr["time"]
        rows: list[dict] = []
        for i, t in enumerate(times):
            t_utc = wc._local_naive_to_utc(str(t), offset, tz)
            if t_utc is None:
                continue
            rows.append({
                "time_utc": t_utc,
                "time_local": str(t),
                "temperature_c": _num(_at(hr, "temperature_2m", i)),
                "apparent_temperature_c": _num(_at(hr, "apparent_temperature", i)),
                "precipitation_probability": _num(_at(hr, "precipitation_probability", i)),
                "precipitation_mm": _num(_at(hr, "precipitation", i)),
                "rain_mm": _num(_at(hr, "rain", i)),
                "snowfall_mm": _num(_at(hr, "snowfall", i)),
                "wmo_code": _int_or_none(_at(hr, "weather_code", i)),
                "wind_speed_kmh": _num(_at(hr, "wind_speed_10m", i)),
                "wind_gusts_kmh": _num(_at(hr, "wind_gusts_10m", i)),
                "visibility_m": _num(_at(hr, "visibility", i)),
            })
        if not rows:
            raise wc.WeatherProviderError(wc.WEATHER_NO_FORECAST, "empty hourly forecast")

        return {
            "provider": self.name,
            "attribution": self.attribution,
            "timezone": tz,
            "timezone_abbreviation": tz_abbr,
            "utc_offset_seconds": offset,
            "fetched_at": wc.now_utc().isoformat(),
            "current": current,
            "hourly": rows,
        }

    # -- HTTP -------------------------------------------------------------
    async def fetch(self, latitude: float, longitude: float, *,
                    horizon_hours: int = wc.FORECAST_HORIZON_HOURS) -> dict:
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "timezone": "auto",                 # provider resolves TARGET-location local time
            "current": _CURRENT_FIELDS,
            "hourly": _HOURLY_FIELDS,
            "forecast_hours": max(1, min(int(horizon_hours), 168)),
            "wind_speed_unit": "kmh",
            "precipitation_unit": "mm",
            "temperature_unit": "celsius",
        }
        try:
            async with self._client_factory() as client:
                resp = await client.get(self._url, params=params)
        except httpx.TimeoutException as e:
            raise wc.WeatherProviderError(wc.WEATHER_TIMEOUT, str(e)) from None
        except httpx.HTTPError as e:
            raise wc.WeatherProviderError(wc.WEATHER_NETWORK, type(e).__name__) from None

        if resp.status_code == 429:
            raise wc.WeatherProviderError(wc.WEATHER_RATE_LIMITED, "429")
        if resp.status_code >= 500:
            raise wc.WeatherProviderError(wc.WEATHER_UNAVAILABLE, str(resp.status_code))
        try:
            payload = resp.json()
        except (json.JSONDecodeError, ValueError):
            raise wc.WeatherProviderError(wc.WEATHER_BAD_RESPONSE, "bad json") from None
        if resp.status_code >= 400:
            # Open-Meteo returns 400 + {"error": true, "reason": "..."} for bad input
            reason = payload.get("reason") if isinstance(payload, dict) else None
            raise wc.WeatherProviderError(wc.WEATHER_UNAVAILABLE, str(reason or resp.status_code))
        return self._parse(payload)


# --------------------------------------------------------------------------- #
# small bounded in-memory forecast cache
# --------------------------------------------------------------------------- #
# What is retained IN PROCESS MEMORY ONLY (never DB, never disk, never logs):
#   * key   -> a short opaque string: a hash of (provider + coordinates coarsened
#              to ~1.1 km). It is NOT a coordinate: the exact request coordinates
#              cannot be recovered from it, and even the ~1.1 km bucket is not
#              stored in the clear.
#   * value -> (fetch epoch seconds, normalised forecast). The normalised
#              forecast contains ONLY timezone metadata + weather values — it
#              carries no latitude/longitude at all (Open-Meteo's echoed coords
#              are deliberately not copied through in _parse).
# Entries expire after CACHE_TTL_S and the table is hard-capped at
# CACHE_MAX_ENTRIES (oldest evicted). cache_clear() empties it.
_CACHE: "dict[str, tuple[float, dict]]" = {}


def _cache_key(provider: str, lat: float, lng: float) -> str:
    d = wc.CACHE_COORD_DECIMALS
    bucket = f"{provider}|{round(float(lat), d):.{d}f}|{round(float(lng), d):.{d}f}"
    return hashlib.blake2s(bucket.encode("utf-8"), digest_size=16).hexdigest()


def cache_clear() -> None:
    _CACHE.clear()


def _cache_get(key, *, now: Optional[float] = None) -> Optional[dict]:
    now = now if now is not None else time.time()
    hit = _CACHE.get(key)
    if hit is None:
        return None
    ts, forecast = hit
    if now - ts > wc.CACHE_TTL_S:
        _CACHE.pop(key, None)
        return None
    return forecast


def _cache_put(key, forecast: dict, *, now: Optional[float] = None) -> None:
    now = now if now is not None else time.time()
    if len(_CACHE) >= wc.CACHE_MAX_ENTRIES and key not in _CACHE:
        oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
        _CACHE.pop(oldest, None)
    _CACHE[key] = (now, forecast)


async def get_forecast(provider: WeatherProvider, latitude: float, longitude: float, *,
                       horizon_hours: int = wc.FORECAST_HORIZON_HOURS,
                       use_cache: bool = True) -> dict:
    """Fetch (or serve from the short-lived cache) a normalised forecast."""
    key = _cache_key(provider.name, latitude, longitude)
    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            return cached
    forecast = await provider.fetch(latitude, longitude, horizon_hours=horizon_hours)
    if use_cache:
        _cache_put(key, forecast)
    return forecast


# --------------------------------------------------------------------------- #
# selection
# --------------------------------------------------------------------------- #
def resolve_weather_provider_name() -> str:
    choice = os.getenv("COREOS_WEATHER_PROVIDER", "open-meteo").strip().lower() or "open-meteo"
    return "open-meteo" if choice in ("open-meteo", "openmeteo", "auto") else choice


def get_weather_provider(name: Optional[str] = None) -> WeatherProvider:
    n = (name or resolve_weather_provider_name()).lower()
    # only one provider today; unknown names fall back to it rather than erroring
    return OpenMeteoWeatherProvider()


# --------------------------------------------------------------------------- #
# tiny parse helpers
# --------------------------------------------------------------------------- #
def _at(obj: dict, key: str, i: int) -> Any:
    arr = obj.get(key)
    if isinstance(arr, list) and 0 <= i < len(arr):
        return arr[i]
    return None


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _int_or_none(v: Any) -> Optional[int]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
