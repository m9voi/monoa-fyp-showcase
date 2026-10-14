"""CoreOS — place-provider abstraction (Phase 5C).

One backend boundary for real external place data. The rest of CoreOS operates
on the normalised model from `places_core.build_place()` and never sees a raw
Google / OSM payload.

Providers
---------
* OverpassProvider (OpenStreetMap / Overpass) — FREE, no key, no billing.
  The default: CoreOS place search works out of the box. Supplies identity +
  coordinates + address/website/phone/opening_hours WHEN tagged. No ratings, no
  reliable "open now", no operational status (OSM has no such concept).
  Attribution: "© OpenStreetMap contributors" (ODbL).

* GooglePlacesProvider (Places API New — Text Search) — RICH: ratings, review
  counts, opening hours, open-now, business status, website, phone. Requires a
  server-side GOOGLE_PLACES_API_KEY (billing-enabled GCP project). Until that key
  is configured it reports PROVIDER_NOT_CONFIGURED — it never fabricates.

Selection: COREOS_PLACES_PROVIDER = auto (default) | osm | google.
  auto -> google if a key is present, else osm.

Privacy: coordinates are sent to the chosen provider only (never logged here,
never in a raised message, never to the AI). One user search = one upstream
request. Bounded radius, bounded result count, hard timeout, no retry loop.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

import httpx

import places_core as pc

_USER_AGENT = "CoreOS/5C (real-place search; +https://coreos.local; contact: coreos-dev@localhost)"

OVERPASS_URL = os.getenv("COREOS_OVERPASS_URL", "https://overpass-api.de/api/interpreter").strip()
GOOGLE_TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

# Phase 5H-B2: the HARD upper bound on activity-profile text queries a single
# search_texts() call will run. One /api/activities/search therefore performs at
# most 1 (search_nearby) + SEARCH_TEXTS_MAX_QUERIES upstream provider calls,
# regardless of how a profile is configured — a malformed / oversized profile
# query list can never cause unbounded upstream iteration.
SEARCH_TEXTS_MAX_QUERIES = 3
GOOGLE_DETAILS_URL = "https://places.googleapis.com/v1/places/"   # + <place_id>
# Phase 5G: field mask for a single-place details lookup (no "places." prefix).
GOOGLE_DETAILS_FIELD_MASK = ",".join([
    "id", "displayName", "location", "formattedAddress", "rating", "userRatingCount",
    "currentOpeningHours.openNow", "regularOpeningHours.weekdayDescriptions",
    "businessStatus", "websiteUri", "nationalPhoneNumber", "googleMapsUri", "types",
])
GOOGLE_FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.location",
    "places.formattedAddress",
    "places.rating",
    "places.userRatingCount",
    "places.currentOpeningHours.openNow",
    "places.regularOpeningHours.weekdayDescriptions",
    "places.businessStatus",
    "places.websiteUri",
    "places.nationalPhoneNumber",
    "places.googleMapsUri",
    "places.types",
])

OSM_ATTRIBUTION = "© OpenStreetMap contributors"
GOOGLE_ATTRIBUTION = "Powered by Google"


# --------------------------------------------------------------------------- #
# provider interface
# --------------------------------------------------------------------------- #
class PlaceProvider:
    name: str = "none"
    attribution: str = ""

    def is_configured(self) -> bool:
        raise NotImplementedError

    async def search_nearby(
        self, *, latitude: float, longitude: float, activity: str, radius_m: int
    ) -> list[dict]:
        """Return a list of normalised CoreOS place dicts (may be empty ==
        NO_RESULTS). Raise places_core.ProviderError on any failure."""
        raise NotImplementedError


def _client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        headers={"User-Agent": _USER_AGENT},
        follow_redirects=True,
    )


# --------------------------------------------------------------------------- #
# OpenStreetMap / Overpass
# --------------------------------------------------------------------------- #
class OverpassProvider(PlaceProvider):
    name = "osm"
    attribution = OSM_ATTRIBUTION

    def __init__(self, *, url: str = OVERPASS_URL, client_factory=None) -> None:
        self._url = url
        self._client_factory = client_factory or (lambda: _client(pc.PROVIDER_TIMEOUT_S))

    def is_configured(self) -> bool:
        return bool(self._url)

    def _build_query(self, lat: float, lng: float, activity: str, radius_m: int) -> str:
        spec = pc.ACTIVITY_TAXONOMY[activity]
        around = f"(around:{radius_m},{lat},{lng})"
        parts = [
            f'nwr{tag_expr}{around};'
            for tag_expr, _match in spec["osm"]
        ]
        body = "\n  ".join(parts)
        return (
            f"[out:json][timeout:20];\n"
            f"(\n  {body}\n);\n"
            f"out center tags {pc.PROVIDER_ELEMENT_CAP};"
        )

    # OSM sport tag values that count as an EXACT match for a CoreOS activity.
    _EXACT_SPORTS: dict[str, frozenset] = {
        "futsal": frozenset({"futsal"}),
        "football": frozenset({"soccer", "football"}),
        "basketball": frozenset({"basketball"}),
        "volleyball": frozenset({"volleyball", "beachvolleyball"}),
        "badminton": frozenset({"badminton"}),
        "swimming": frozenset({"swimming"}),
        "running": frozenset({"running", "athletics"}),
        "cycling": frozenset({"cycling"}),
        "tennis": frozenset({"tennis"}),
        "golf": frozenset({"golf"}),
    }

    def _match_kind_for(self, activity: str, tags: dict) -> str:
        """The union query does not say which sub-filter matched, so re-derive
        exact/broad from the returned tags."""
        sport = {s.strip().lower() for s in (tags.get("sport") or "").split(";") if s.strip()}
        leisure = (tags.get("leisure") or "").lower()
        if sport & self._EXACT_SPORTS.get(activity, frozenset()):
            return "exact"
        if activity == "swimming" and leisure == "swimming_pool":
            return "exact"
        if activity == "golf" and (leisure == "golf_course" or tags.get("golf")):
            return "exact"
        return "broad"

    def _parse(self, payload: Any, activity: str, origin_lat: float, origin_lng: float) -> list[dict]:
        if not isinstance(payload, dict) or "elements" not in payload:
            raise pc.ProviderError(pc.INVALID_PROVIDER_RESPONSE, "no elements array")
        elements = payload.get("elements")
        if not isinstance(elements, list):
            raise pc.ProviderError(pc.INVALID_PROVIDER_RESPONSE, "elements not a list")

        out: list[dict] = []
        for el in elements:
            if not isinstance(el, dict):
                continue
            el_type = el.get("type")
            el_id = el.get("id")
            if el_type not in ("node", "way", "relation") or not isinstance(el_id, int):
                continue
            if el_type == "node":
                lat, lng = el.get("lat"), el.get("lon")
            else:
                c = el.get("center") or {}
                lat, lng = c.get("lat"), c.get("lon")
            if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
                continue
            tags = el.get("tags") or {}
            if not isinstance(tags, dict):
                tags = {}
            provider_types = [
                f"{k}={tags[k]}" for k in ("leisure", "sport", "amenity") if tags.get(k)
            ]
            place = pc.build_place(
                provider="osm",
                provider_place_id=f"{el_type}:{el_id}",
                name=tags.get("name") or tags.get("official_name") or tags.get("name:en"),
                latitude=lat,
                longitude=lng,
                origin_lat=origin_lat,
                origin_lng=origin_lng,
                activity=activity,
                activity_match=self._match_kind_for(activity, tags),
                provider_types=provider_types,
                address=_osm_address(tags),
                # OSM: NO rating, NO review count, NO operational status, NO open-now.
                opening_hours=tags.get("opening_hours"),
                website=tags.get("website") or tags.get("contact:website"),
                phone=tags.get("phone") or tags.get("contact:phone"),
            )
            if place is not None:
                out.append(place)
        return out

    async def search_nearby(self, *, latitude, longitude, activity, radius_m) -> list[dict]:
        query = self._build_query(latitude, longitude, activity, radius_m)
        try:
            async with self._client_factory() as client:
                resp = await client.post(self._url, data={"data": query})
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
        return self._parse(payload, activity, latitude, longitude)


def _osm_address(tags: dict) -> Optional[str]:
    parts = [
        tags.get("addr:housenumber"),
        tags.get("addr:street"),
        tags.get("addr:suburb") or tags.get("addr:city"),
        tags.get("addr:postcode"),
    ]
    joined = " ".join(p for p in parts if p)
    return joined.strip() or None


# --------------------------------------------------------------------------- #
# Google Places API (New) — Text Search
# --------------------------------------------------------------------------- #
class GooglePlacesProvider(PlaceProvider):
    name = "google"
    attribution = GOOGLE_ATTRIBUTION

    def __init__(self, *, api_key: Optional[str] = None, url: str = GOOGLE_TEXT_SEARCH_URL,
                 client_factory=None) -> None:
        self._key = (api_key if api_key is not None
                     else os.getenv("GOOGLE_PLACES_API_KEY", "")).strip()
        self._url = url
        self._client_factory = client_factory or (lambda: _client(pc.PROVIDER_TIMEOUT_S))

    def is_configured(self) -> bool:
        return bool(self._key)

    def _parse(self, payload: Any, activity: str, origin_lat: float, origin_lng: float) -> list[dict]:
        if not isinstance(payload, dict):
            raise pc.ProviderError(pc.INVALID_PROVIDER_RESPONSE, "not an object")
        places = payload.get("places", [])
        if places in (None, ""):
            places = []
        if not isinstance(places, list):
            raise pc.ProviderError(pc.INVALID_PROVIDER_RESPONSE, "places not a list")

        out: list[dict] = []
        for p in places:
            if not isinstance(p, dict):
                continue
            pid = p.get("id")
            loc = p.get("location") or {}
            lat, lng = loc.get("latitude"), loc.get("longitude")
            if not isinstance(pid, str) or not isinstance(lat, (int, float)) \
               or not isinstance(lng, (int, float)):
                continue
            reg = p.get("regularOpeningHours") or {}
            cur = p.get("currentOpeningHours") or {}
            place = pc.build_place(
                provider="google",
                provider_place_id=pid,
                name=((p.get("displayName") or {}).get("text")),
                latitude=lat,
                longitude=lng,
                origin_lat=origin_lat,
                origin_lng=origin_lng,
                activity=activity,
                activity_match="broad",  # Text Search is keyword-based, not tag-exact
                provider_types=[str(t) for t in (p.get("types") or []) if t],
                address=p.get("formattedAddress"),
                rating=p.get("rating"),
                rating_count=p.get("userRatingCount"),
                opening_hours=reg.get("weekdayDescriptions"),
                open_now=cur.get("openNow"),
                business_status=p.get("businessStatus"),
                website=p.get("websiteUri"),
                phone=p.get("nationalPhoneNumber"),
                maps_url=p.get("googleMapsUri"),
            )
            if place is not None:
                out.append(place)
        return out

    async def _text_search(self, *, text_query: str, latitude, longitude, radius_m,
                           activity: str, included_type: Optional[str]) -> list[dict]:
        """One Text Search (New) request -> normalised places. Shared by
        search_nearby (Phase 5C/5D Map) and search_texts (Phase 5E Activity)."""
        if not self._key:
            raise pc.ProviderError(pc.PROVIDER_NOT_CONFIGURED, "no GOOGLE_PLACES_API_KEY")
        body: dict[str, Any] = {
            "textQuery": text_query,
            "pageSize": min(pc.SEARCH_RESULT_LIMIT, 20),
            "locationBias": {
                "circle": {
                    "center": {"latitude": latitude, "longitude": longitude},
                    "radius": float(radius_m),
                }
            },
        }
        if included_type:
            body["includedType"] = included_type
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self._key,
            "X-Goog-FieldMask": GOOGLE_FIELD_MASK,
        }
        try:
            async with self._client_factory() as client:
                resp = await client.post(self._url, headers=headers, json=body)
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
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            status = str(payload["error"].get("status") or "")
            if status == "RESOURCE_EXHAUSTED":
                raise pc.ProviderError(pc.PROVIDER_RATE_LIMITED, status)
            if status in ("PERMISSION_DENIED", "UNAUTHENTICATED"):
                raise pc.ProviderError(pc.PROVIDER_NOT_CONFIGURED, status)
            raise pc.ProviderError(pc.PROVIDER_UNAVAILABLE, status or "error")
        return self._parse(payload, activity, latitude, longitude)

    async def search_nearby(self, *, latitude, longitude, activity, radius_m) -> list[dict]:
        spec = pc.ACTIVITY_TAXONOMY[activity]["google"]
        return await self._text_search(
            text_query=spec["text_query"], latitude=latitude, longitude=longitude,
            radius_m=radius_m, activity=activity, included_type=spec.get("type_hint"),
        )

    async def search_texts(self, *, latitude, longitude, activity, radius_m,
                           queries: list[str], included_type: Optional[str] = None,
                           max_queries: int = SEARCH_TEXTS_MAX_QUERIES) -> list[dict]:
        """Phase 5E — run several activity-profile text queries (bounded) and
        merge, so public-geography activities (running/cycling) can surface
        parks/paths, not only venues literally named after the sport. Dedupe is
        done by the caller via the normalised place_id. `max_queries` is clamped
        to SEARCH_TEXTS_MAX_QUERIES so one call can never exceed the documented
        upstream fan-out bound (Phase 5H-B2)."""
        cap = max(0, min(int(max_queries), SEARCH_TEXTS_MAX_QUERIES))
        out: list[dict] = []
        seen: set[str] = set()
        for q in list(queries)[:cap]:
            for p in await self._text_search(
                text_query=q, latitude=latitude, longitude=longitude,
                radius_m=radius_m, activity=activity, included_type=included_type,
            ):
                pid = p.get("place_id")
                if pid and pid not in seen:
                    seen.add(pid)
                    out.append(p)
        return out

    async def place_details(self, *, provider_place_id: str, origin_lat: float,
                            origin_lng: float, activity: str = "unknown") -> Optional[dict]:
        """Phase 5G — resolve ONE Google place id to a fresh normalised CorePlace
        (live rating / open-now / hours). Raises ProviderError on a config /
        transport failure; returns None if Google no longer knows the id."""
        if not self._key:
            raise pc.ProviderError(pc.PROVIDER_NOT_CONFIGURED, "no GOOGLE_PLACES_API_KEY")
        headers = {
            "X-Goog-Api-Key": self._key,
            "X-Goog-FieldMask": GOOGLE_DETAILS_FIELD_MASK,
        }
        try:
            async with self._client_factory() as client:
                resp = await client.get(GOOGLE_DETAILS_URL + provider_place_id, headers=headers)
        except httpx.TimeoutException as e:
            raise pc.ProviderError(pc.PROVIDER_TIMEOUT, str(e)) from None
        except httpx.HTTPError as e:
            raise pc.ProviderError(pc.PROVIDER_UNAVAILABLE, type(e).__name__) from None
        if resp.status_code in (404, 400):
            return None                          # Google no longer resolves this id
        if resp.status_code == 429:
            raise pc.ProviderError(pc.PROVIDER_RATE_LIMITED, "429")
        if resp.status_code in (401, 403):
            raise pc.ProviderError(pc.PROVIDER_NOT_CONFIGURED, str(resp.status_code))
        if resp.status_code >= 400:
            raise pc.ProviderError(pc.PROVIDER_UNAVAILABLE, str(resp.status_code))
        try:
            p = resp.json()
        except (json.JSONDecodeError, ValueError):
            raise pc.ProviderError(pc.INVALID_PROVIDER_RESPONSE, "bad json") from None
        if not isinstance(p, dict) or not p.get("id"):
            return None
        loc = p.get("location") or {}
        lat, lng = loc.get("latitude"), loc.get("longitude")
        if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
            return None
        reg = p.get("regularOpeningHours") or {}
        cur = p.get("currentOpeningHours") or {}
        return pc.build_place(
            provider="google", provider_place_id=str(p["id"]),
            name=((p.get("displayName") or {}).get("text")),
            latitude=lat, longitude=lng, origin_lat=origin_lat, origin_lng=origin_lng,
            activity=activity, activity_match="broad",
            provider_types=[str(t) for t in (p.get("types") or []) if t],
            address=p.get("formattedAddress"),
            rating=p.get("rating"), rating_count=p.get("userRatingCount"),
            opening_hours=reg.get("weekdayDescriptions"), open_now=cur.get("openNow"),
            business_status=p.get("businessStatus"),
            website=p.get("websiteUri"), phone=p.get("nationalPhoneNumber"),
            maps_url=p.get("googleMapsUri"),
        )


# --------------------------------------------------------------------------- #
# selection
# --------------------------------------------------------------------------- #
def resolve_provider_name() -> str:
    choice = os.getenv("COREOS_PLACES_PROVIDER", "auto").strip().lower() or "auto"
    if choice == "google":
        return "google"
    if choice == "osm":
        return "osm"
    # auto
    return "google" if os.getenv("GOOGLE_PLACES_API_KEY", "").strip() else "osm"


def get_provider(name: Optional[str] = None) -> PlaceProvider:
    n = (name or resolve_provider_name()).lower()
    if n == "google":
        return GooglePlacesProvider()
    return OverpassProvider()
