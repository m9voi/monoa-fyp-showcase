"""CoreOS — geocoding provider abstraction (Phase 5D).

Resolves a typed area / place name (e.g. "Cyberjaya", "KLCC", "Bukit Bintang")
to real coordinates so the user can set an EXPLICIT search origin on the Map.

Mirrors places_provider.py:
  * server-side only — the API key never reaches the browser or a log line;
  * provider-neutral normalised result shape;
  * normalised failure taxonomy (reuses places_core.ProviderError / PROVIDER_*);
  * one user action = one upstream request (an explicit "Search", not live
    keystroke autocomplete), bounded result count, hard timeout.

Providers
---------
* GoogleGeocodeProvider — Google Geocoding API. Rich, needs a key with the
  "Geocoding API" enabled (falls back to GOOGLE_PLACES_API_KEY).
* NominatimProvider — OpenStreetMap Nominatim. Free, no key. Attribution
  "© OpenStreetMap contributors". Used with an explicit Search button (not
  autocomplete) and a descriptive User-Agent, per its usage policy.

Selection: COREOS_GEOCODE_PROVIDER = auto (default) | google | osm.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

import httpx

import places_core as pc  # ProviderError + PROVIDER_* kinds are shared

_USER_AGENT = "CoreOS/5D (area search; +https://coreos.local; contact: coreos-dev@localhost)"
GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
NOMINATIM_URL = os.getenv("COREOS_NOMINATIM_URL", "https://nominatim.openstreetmap.org/search").strip()

GEOCODE_RESULT_LIMIT = 6
GEOCODE_TIMEOUT_S = 10.0
GEOCODE_QUERY_MIN = 2
GEOCODE_QUERY_MAX = 120

OSM_ATTRIBUTION = "© OpenStreetMap contributors"
GOOGLE_ATTRIBUTION = "Powered by Google"


def _geocoding_key() -> str:
    return (os.getenv("GOOGLE_GEOCODING_API_KEY") or os.getenv("GOOGLE_PLACES_API_KEY", "")).strip()


def _client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout), headers={"User-Agent": _USER_AGENT},
                             follow_redirects=True)


class GeoProvider:
    name: str = "none"
    attribution: str = ""

    def is_configured(self) -> bool:
        raise NotImplementedError

    async def geocode(self, query: str) -> list[dict]:
        """Return a list of normalised area results (may be empty == NO_RESULTS).
        Raise places_core.ProviderError on any failure."""
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Google Geocoding API
# --------------------------------------------------------------------------- #
class GoogleGeocodeProvider(GeoProvider):
    name = "google"
    attribution = GOOGLE_ATTRIBUTION

    def __init__(self, *, api_key: Optional[str] = None, url: str = GOOGLE_GEOCODE_URL,
                 client_factory=None) -> None:
        self._key = api_key if api_key is not None else _geocoding_key()
        self._url = url
        self._client_factory = client_factory or (lambda: _client(GEOCODE_TIMEOUT_S))

    def is_configured(self) -> bool:
        return bool(self._key)

    def _parse(self, payload: Any) -> list[dict]:
        if not isinstance(payload, dict):
            raise pc.ProviderError(pc.INVALID_PROVIDER_RESPONSE, "not an object")
        status = str(payload.get("status") or "")
        if status == "OK":
            out: list[dict] = []
            for r in (payload.get("results") or [])[:GEOCODE_RESULT_LIMIT]:
                if not isinstance(r, dict):
                    continue
                loc = ((r.get("geometry") or {}).get("location") or {})
                lat, lng = loc.get("lat"), loc.get("lng")
                label = (r.get("formatted_address") or "").strip()
                if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)) or not label:
                    continue
                pid = r.get("place_id")
                types = r.get("types") or []
                out.append({
                    "id": f"google:{pid}" if pid else f"google:{lat},{lng}",
                    "label": label, "latitude": float(lat), "longitude": float(lng),
                    "provider": "google", "result_type": (str(types[0]) if types else None),
                })
            return out
        if status == "ZERO_RESULTS":
            return []
        if status in ("REQUEST_DENIED", "UNAUTHENTICATED"):
            raise pc.ProviderError(pc.PROVIDER_NOT_CONFIGURED, status)
        if status in ("OVER_QUERY_LIMIT", "OVER_DAILY_LIMIT", "RESOURCE_EXHAUSTED"):
            raise pc.ProviderError(pc.PROVIDER_RATE_LIMITED, status)
        if status in ("INVALID_REQUEST", "MAX_ELEMENTS_EXCEEDED"):
            raise pc.ProviderError(pc.INVALID_PROVIDER_RESPONSE, status)
        raise pc.ProviderError(pc.PROVIDER_UNAVAILABLE, status or "error")

    async def geocode(self, query: str) -> list[dict]:
        if not self._key:
            raise pc.ProviderError(pc.PROVIDER_NOT_CONFIGURED, "no GOOGLE_GEOCODING_API_KEY")
        try:
            async with self._client_factory() as client:
                resp = await client.get(self._url, params={"address": query, "key": self._key})
        except httpx.TimeoutException as e:
            raise pc.ProviderError(pc.PROVIDER_TIMEOUT, str(e)) from None
        except httpx.HTTPError as e:
            raise pc.ProviderError(pc.PROVIDER_UNAVAILABLE, type(e).__name__) from None
        if resp.status_code == 429:
            raise pc.ProviderError(pc.PROVIDER_RATE_LIMITED, "429")
        if resp.status_code in (401, 403):
            raise pc.ProviderError(pc.PROVIDER_NOT_CONFIGURED, str(resp.status_code))
        if resp.status_code >= 400:
            raise pc.ProviderError(pc.PROVIDER_UNAVAILABLE, str(resp.status_code))
        try:
            payload = resp.json()
        except (json.JSONDecodeError, ValueError):
            raise pc.ProviderError(pc.INVALID_PROVIDER_RESPONSE, "bad json") from None
        return self._parse(payload)


# --------------------------------------------------------------------------- #
# OpenStreetMap Nominatim
# --------------------------------------------------------------------------- #
class NominatimProvider(GeoProvider):
    name = "osm"
    attribution = OSM_ATTRIBUTION

    def __init__(self, *, url: str = NOMINATIM_URL, client_factory=None) -> None:
        self._url = url
        self._client_factory = client_factory or (lambda: _client(GEOCODE_TIMEOUT_S))

    def is_configured(self) -> bool:
        return bool(self._url)

    def _parse(self, payload: Any) -> list[dict]:
        if not isinstance(payload, list):
            raise pc.ProviderError(pc.INVALID_PROVIDER_RESPONSE, "not a list")
        out: list[dict] = []
        for r in payload[:GEOCODE_RESULT_LIMIT]:
            if not isinstance(r, dict):
                continue
            try:
                lat, lng = float(r.get("lat")), float(r.get("lon"))
            except (TypeError, ValueError):
                continue
            label = (r.get("display_name") or "").strip()
            if not label:
                continue
            ot, oid = r.get("osm_type"), r.get("osm_id")
            ident = f"{str(ot)[:1]}{oid}" if ot and oid is not None else f"{lat},{lng}"
            out.append({
                "id": f"osm:{ident}", "label": label,
                "latitude": lat, "longitude": lng,
                "provider": "osm", "result_type": r.get("type"),
            })
        return out

    async def geocode(self, query: str) -> list[dict]:
        try:
            async with self._client_factory() as client:
                resp = await client.get(self._url, params={
                    "q": query, "format": "jsonv2", "limit": GEOCODE_RESULT_LIMIT, "addressdetails": 0,
                })
        except httpx.TimeoutException as e:
            raise pc.ProviderError(pc.PROVIDER_TIMEOUT, str(e)) from None
        except httpx.HTTPError as e:
            raise pc.ProviderError(pc.PROVIDER_UNAVAILABLE, type(e).__name__) from None
        if resp.status_code == 429:
            raise pc.ProviderError(pc.PROVIDER_RATE_LIMITED, "429")
        if resp.status_code in (503, 504):
            raise pc.ProviderError(pc.PROVIDER_UNAVAILABLE, str(resp.status_code))
        if resp.status_code >= 400:
            raise pc.ProviderError(pc.PROVIDER_UNAVAILABLE, str(resp.status_code))
        try:
            payload = resp.json()
        except (json.JSONDecodeError, ValueError):
            raise pc.ProviderError(pc.INVALID_PROVIDER_RESPONSE, "bad json") from None
        return self._parse(payload)


# --------------------------------------------------------------------------- #
# selection
# --------------------------------------------------------------------------- #
def resolve_geo_provider_name() -> str:
    choice = os.getenv("COREOS_GEOCODE_PROVIDER", "auto").strip().lower() or "auto"
    if choice == "google":
        return "google"
    if choice == "osm":
        return "osm"
    return "google" if _geocoding_key() else "osm"


def get_geo_provider(name: Optional[str] = None) -> GeoProvider:
    n = (name or resolve_geo_provider_name()).lower()
    if n == "google":
        return GoogleGeocodeProvider()
    return NominatimProvider()
