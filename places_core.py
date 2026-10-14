"""CoreOS — verified places core (Phase 5C).

Deterministic, provider-neutral domain logic for real-place search:

  * activity -> provider-search taxonomy (explicit, testable, precision-first)
  * search-radius / activity / result-count policy (centralised, server-validated)
  * Haversine straight-line distance
  * the normalised CoreOS place model + an explicit EVIDENCE / provenance model
  * the normalised provider-failure taxonomy

Real-world facts come ONLY from an external provider (see places_provider.py).
Nothing in this module invents a name, coordinate, rating, opening hour, phone,
website or "open now" value. Missing evidence stays missing.
"""
from __future__ import annotations

import math
from typing import Any, Optional

# --------------------------------------------------------------------------- #
# policy constants
# --------------------------------------------------------------------------- #
SEARCH_RADIUS_DEFAULT_M = 8_000
SEARCH_RADIUS_MIN_M = 200
SEARCH_RADIUS_MAX_M = 20_000          # historical CoreOS "~20 km nearby" ceiling
SEARCH_RESULT_LIMIT = 20             # bound results returned to the client

# B9 — adaptive Activity nearby-search radius ladder. The first search runs at
# the base radius; the search widens ONLY when too few valid, deduplicated,
# category-relevant, in-radius results remain, up to a hard 20 km ceiling. This
# is NOT a user control — no radius selector is exposed. 20 km is the final
# fallback and the maximum, never the silent default.
ACTIVITY_RADIUS_LADDER_M = (8_000, 12_000, 20_000)
ACTIVITY_MIN_RESULTS_BEFORE_EXPAND = 3
# A CoreOS-computed straight-line distance can land a few metres over the nominal
# radius purely from floating-point rounding in the Haversine, so a venue sitting
# exactly on the ring is kept. This is ONLY a boundary tolerance — it is never a
# way to surface a genuinely out-of-radius result.
RADIUS_BOUNDARY_TOLERANCE_M = 25.0
PROVIDER_ELEMENT_CAP = 60           # bound elements requested from the provider
# Single hard timeout for the one upstream request. Sized for the public
# Overpass endpoint, whose latency for a small `around` query is usually a few
# seconds but spikes under load; beyond this the search is reported as
# PROVIDER_TIMEOUT rather than making the user wait indefinitely.
PROVIDER_TIMEOUT_S = 15.0

# --------------------------------------------------------------------------- #
# provider failure taxonomy  (NO_RESULTS is NOT here — an empty list is success)
# --------------------------------------------------------------------------- #
PROVIDER_NOT_CONFIGURED = "PROVIDER_NOT_CONFIGURED"
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
INVALID_PROVIDER_RESPONSE = "INVALID_PROVIDER_RESPONSE"

PROVIDER_ERROR_KINDS = frozenset({
    PROVIDER_NOT_CONFIGURED,
    PROVIDER_UNAVAILABLE,
    PROVIDER_TIMEOUT,
    PROVIDER_RATE_LIMITED,
    INVALID_PROVIDER_RESPONSE,
})


class ProviderError(Exception):
    """Raised by a provider adapter. `kind` is one of PROVIDER_ERROR_KINDS.
    The message is for server logs only and must never contain coordinates."""

    def __init__(self, kind: str, detail: str = "") -> None:
        if kind not in PROVIDER_ERROR_KINDS:
            kind = PROVIDER_UNAVAILABLE
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind


# --------------------------------------------------------------------------- #
# activity -> search taxonomy
# --------------------------------------------------------------------------- #
# EXACT  = the tag / query names the sport itself (a futsal court, a pool).
# BROAD  = a venue type that plausibly hosts the activity (a multi-sport centre)
#          but is not specific. The UI can label these differently.
#
# Precision-first (Phase 5C §10): a bicycle shop is not a cycling place; a park
# is not automatically a football field; a stadium is not automatically public.
# Ambiguous venue types are intentionally omitted rather than padding results.

ACTIVITY_TAXONOMY: dict[str, dict[str, Any]] = {
    "futsal": {
        "label": "Futsal",
        # Overpass: (filter, match) pairs — filter is an OSM tag expression.
        "osm": [
            ('["sport"~"^(futsal|multi)$"]', "exact"),
            ('["leisure"="pitch"]["sport"~"soccer|football"]', "broad"),
            ('["leisure"="sports_centre"]["sport"~"futsal"]', "exact"),
        ],
        "google": {"text_query": "futsal court", "type_hint": "sports_complex"},
    },
    "football": {
        "label": "Field Football",
        "osm": [
            ('["leisure"="pitch"]["sport"~"^(soccer|football)$"]', "exact"),
            ('["leisure"="stadium"]["sport"~"^(soccer|football)$"]', "broad"),
        ],
        "google": {"text_query": "football field", "type_hint": "stadium"},
    },
    "basketball": {
        "label": "Basketball",
        "osm": [
            ('["sport"="basketball"]', "exact"),
            ('["leisure"="pitch"]["sport"~"basketball"]', "exact"),
        ],
        "google": {"text_query": "basketball court", "type_hint": "sports_complex"},
    },
    "volleyball": {
        "label": "Volleyball",
        # Official OSM values only: sport=volleyball and sport=beachvolleyball
        # (one word, no underscore). Never inferred from a "beach"/"sand" name,
        # a plain sports centre, or an unrelated venue. No generic Google
        # type_hint — no official Google Places type names volleyball.
        "osm": [
            ('["sport"~"^(volleyball|beachvolleyball)$"]', "exact"),
            ('["leisure"="pitch"]["sport"~"^(volleyball|beachvolleyball)$"]', "exact"),
            ('["leisure"="sports_centre"]["sport"~"volleyball"]', "exact"),
        ],
        "google": {"text_query": "volleyball court", "type_hint": None},
    },
    "badminton": {
        "label": "Badminton",
        "osm": [
            ('["sport"="badminton"]', "exact"),
            ('["leisure"="sports_centre"]["sport"~"badminton"]', "exact"),
        ],
        "google": {"text_query": "badminton court", "type_hint": "sports_complex"},
    },
    "swimming": {
        "label": "Swimming",
        "osm": [
            ('["leisure"="swimming_pool"]', "exact"),
            ('["sport"="swimming"]', "exact"),
            ('["leisure"="water_park"]', "broad"),
        ],
        "google": {"text_query": "public swimming pool", "type_hint": "swimming_pool"},
    },
    "running": {
        "label": "Running",
        # Phase 5E: running does NOT require a formal facility. Formal tracks are
        # the EXACT tier; public running geography (parks, recreation grounds,
        # nature reserves, lakefront gardens) is a legitimate BROAD tier — the
        # Activity Engine's evidence model decides how strongly each ranks. A
        # bicycle shop / generic amenity is still never included.
        "osm": [
            ('["leisure"="track"]["sport"~"running|athletics|multi"]', "exact"),
            ('["leisure"="pitch"]["sport"="athletics"]', "exact"),
            ('["sport"="athletics"]', "exact"),
            ('["leisure"="park"]', "broad"),
            ('["leisure"="recreation_ground"]', "broad"),
            ('["leisure"="nature_reserve"]', "broad"),
            ('["leisure"="garden"]["garden:type"!="private"]', "broad"),
        ],
        "google": {"text_query": "running track", "type_hint": "athletic_field"},
    },
    "cycling": {
        "label": "Cycling",
        # Phase 5E: as with running, appropriate paths / parks / recreational
        # geography count as BROAD candidates. Still deliberately NOT shop=bicycle.
        "osm": [
            ('["sport"="cycling"]', "exact"),
            ('["leisure"="track"]["sport"="cycling"]', "exact"),
            ('["leisure"="park"]', "broad"),
            ('["leisure"="nature_reserve"]', "broad"),
            ('["route"="bicycle"]', "broad"),
        ],
        "google": {"text_query": "cycling track velodrome", "type_hint": None},
    },
    "tennis": {
        "label": "Tennis",
        "osm": [
            ('["sport"="tennis"]', "exact"),
            ('["leisure"="pitch"]["sport"~"tennis"]', "exact"),
            ('["leisure"="sports_centre"]["sport"~"tennis"]', "exact"),
        ],
        "google": {"text_query": "tennis court", "type_hint": None},
    },
    "golf": {
        "label": "Golf",
        "osm": [
            ('["leisure"="golf_course"]', "exact"),
            ('["sport"="golf"]', "exact"),
            ('["golf"="driving_range"]', "exact"),
        ],
        "google": {"text_query": "golf course driving range", "type_hint": "golf_course"},
    },
}

SUPPORTED_ACTIVITIES = tuple(ACTIVITY_TAXONOMY.keys())


# --------------------------------------------------------------------------- #
# validators (server-side authoritative — do not trust the client)
# --------------------------------------------------------------------------- #
def validate_activity(raw: Any) -> str:
    """Return the canonical activity key, or raise ValueError."""
    if not isinstance(raw, str):
        raise ValueError("activity is required.")
    key = raw.strip().lower()
    if key not in ACTIVITY_TAXONOMY:
        raise ValueError("activity is not a supported CoreOS activity.")
    return key


def validate_radius_m(raw: Any) -> int:
    """Return a radius in metres within [MIN, MAX]. None/absent -> default.
    Zero / negative / non-numeric -> ValueError. Above MAX -> clamped to MAX."""
    if raw is None:
        return SEARCH_RADIUS_DEFAULT_M
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError("radius_m must be a number.")
    v = float(raw)
    if not math.isfinite(v) or v <= 0:
        raise ValueError("radius_m must be a positive number.")
    if v < SEARCH_RADIUS_MIN_M:
        return SEARCH_RADIUS_MIN_M
    if v > SEARCH_RADIUS_MAX_M:
        return SEARCH_RADIUS_MAX_M
    return int(v)


# --------------------------------------------------------------------------- #
# distance
# --------------------------------------------------------------------------- #
_EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle STRAIGHT-LINE distance in metres. Not a travel distance.

    Raises ValueError on non-finite / out-of-range inputs (defence in depth —
    callers should already have run the Phase 5B coordinate validator)."""
    for v in (lat1, lon1, lat2, lon2):
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(float(v)):
            raise ValueError("coordinates must be finite numbers.")
    if not (-90.0 <= lat1 <= 90.0 and -90.0 <= lat2 <= 90.0):
        raise ValueError("latitude out of range.")
    if not (-180.0 <= lon1 <= 180.0 and -180.0 <= lon2 <= 180.0):
        raise ValueError("longitude out of range.")
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


# --------------------------------------------------------------------------- #
# normalised CoreOS place model + EVIDENCE model
# --------------------------------------------------------------------------- #
BUSINESS_STATUSES = frozenset({"OPERATIONAL", "CLOSED_TEMPORARILY", "CLOSED_PERMANENTLY"})


def _clean_str(v: Any) -> Optional[str]:
    if not isinstance(v, str):
        return None
    s = v.strip()
    return s or None


def _clean_web_url(v: Any) -> Optional[str]:
    """A provider-supplied URL is untrusted third-party data. Return it only if
    it is a well-formed absolute http/https URL with a host; anything else
    (``javascript:``, ``data:``, ``file:``, a bare word, a malformed string) is
    dropped so a downstream ``<a href>`` can never carry a dangerous scheme.
    Phase 5H-B4 (L1). No replacement URL is ever fabricated."""
    s = _clean_str(v)
    if s is None:
        return None
    try:
        from urllib.parse import urlsplit
        u = urlsplit(s)
    except ValueError:
        return None
    if u.scheme.lower() in ("http", "https") and u.netloc:
        return s
    return None


def _clean_rating(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return round(f, 1) if math.isfinite(f) and 0.0 <= f <= 5.0 else None


def _clean_count(v: Any) -> Optional[int]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    n = int(v)
    return n if n >= 0 else None


def _clean_open_now(v: Any) -> Optional[bool]:
    return v if isinstance(v, bool) else None


def build_place(
    *,
    provider: str,
    provider_place_id: str,
    name: Optional[str],
    latitude: float,
    longitude: float,
    origin_lat: float,
    origin_lng: float,
    activity: str,
    activity_match: str,
    provider_types: Optional[list[str]] = None,
    address: Any = None,
    rating: Any = None,
    rating_count: Any = None,
    opening_hours: Any = None,
    open_now: Any = None,
    business_status: Any = None,
    website: Any = None,
    phone: Any = None,
    maps_url: Any = None,
) -> Optional[dict]:
    """Assemble ONE normalised CoreOS place from already-parsed provider fields.

    Returns None if identity evidence is insufficient (no name, or bad coords) —
    such a row is dropped rather than shown with a blank/invented name.

    Every optional field that the provider did not supply stays None. The
    EVIDENCE block records, per category, WHICH provider supplied the fact (or
    None). `identity.verified` is the ONLY 'verified' flag — it is never a
    blanket claim about hours / ratings / operational status.
    """
    nm = _clean_str(name)
    if nm is None:
        return None
    try:
        lat = float(latitude)
        lng = float(longitude)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lng)):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
        return None

    dist = haversine_m(origin_lat, origin_lng, lat, lng)

    rating_v = _clean_rating(rating)
    count_v = _clean_count(rating_count)
    website_v = _clean_web_url(website)
    phone_v = _clean_str(phone)
    address_v = _clean_str(address)
    open_now_v = _clean_open_now(open_now)
    status_v = business_status if business_status in BUSINESS_STATUSES else None
    hours_v: Optional[Any]
    if isinstance(opening_hours, str):
        hours_v = _clean_str(opening_hours)
    elif isinstance(opening_hours, list) and opening_hours:
        hours_v = [str(x) for x in opening_hours]
    else:
        hours_v = None

    has_hours = hours_v is not None or open_now_v is not None
    maps = _clean_web_url(maps_url) or (
        f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"
    )

    evidence = {
        "identity": {"source": provider, "verified": True},
        "operational": {
            "source": provider if status_v is not None else None,
            "status": status_v,
        },
        "hours": {
            "source": provider if has_hours else None,
            "has_hours": bool(hours_v is not None),
            "open_now": open_now_v,
        },
        "quality": {
            "source": provider if rating_v is not None else None,
            "rating": rating_v,
            "rating_count": count_v if rating_v is not None else None,
        },
        "contact": {
            "source": provider if (website_v or phone_v) else None,
            "website": website_v,
            "phone": phone_v,
        },
    }

    return {
        "place_id": f"{provider}:{provider_place_id}",
        "provider": provider,
        "provider_place_id": provider_place_id,
        "name": nm,
        "latitude": lat,
        "longitude": lng,
        "address": address_v,
        "distance_m": round(dist, 1),
        "distance_kind": "straight_line",
        "activity": activity,
        "activity_match": "exact" if activity_match == "exact" else "broad",
        "provider_types": list(provider_types or []),
        "rating": rating_v,
        "rating_count": count_v if rating_v is not None else None,
        "opening_hours": hours_v,
        "open_now": open_now_v,
        "business_status": status_v,
        "website": website_v,
        "phone": phone_v,
        "maps_url": maps,
        "evidence": evidence,
    }


def within_radius(
    places: list[dict],
    radius_m: float,
    *,
    tolerance_m: float = RADIUS_BOUNDARY_TOLERANCE_M,
) -> list[dict]:
    """Return only the places whose CoreOS-computed straight-line ``distance_m``
    is within ``radius_m`` (plus a tiny boundary tolerance).

    STRICT: a place whose distance is missing, non-numeric, non-finite or
    negative is DROPPED — it cannot be shown as "nearby" without a proven
    distance. This runs on already-normalised CoreOS place dicts, so it applies
    identically whatever the provider (Overpass already hard-filters by radius;
    Google Text Search only *biases*, so this is the real enforcement there).
    Order is preserved; the caller sorts.
    """
    try:
        limit = float(radius_m) + float(tolerance_m)
    except (TypeError, ValueError):
        return []
    kept: list[dict] = []
    for p in places:
        if not isinstance(p, dict):
            continue
        d = p.get("distance_m")
        if isinstance(d, bool) or not isinstance(d, (int, float)):
            continue
        dv = float(d)
        if math.isfinite(dv) and 0.0 <= dv <= limit:
            kept.append(p)
    return kept


def _distance_sort_key(place: dict) -> float:
    """Nearest-first sort key over the CoreOS-computed straight-line distance.
    A missing / null / non-finite distance sorts LAST — never ahead of a known
    distance."""
    d = place.get("distance_m")
    if isinstance(d, bool) or not isinstance(d, (int, float)):
        return math.inf
    dv = float(d)
    return dv if math.isfinite(dv) and dv >= 0 else math.inf


def finalize_results(places: list[dict]) -> list[dict]:
    """Dedupe by place_id, then order NEAREST-FIRST by the CoreOS-computed
    ``distance_m`` (Haversine straight line from the validated user coordinates),
    then cap at the limit.

    This is the AUTHORITATIVE, provider-neutral ordering: it runs on normalised
    CorePlace dicts regardless of provider, so a provider's own relevance ranking
    (e.g. Google Text Search) can never override geographic distance order.
    Sorting does not mutate any place field or its evidence. Ties are broken
    deterministically by ``place_id`` so the order is fully reproducible.
    """
    seen: set[str] = set()
    out: list[dict] = []
    for p in sorted(places, key=lambda x: (_distance_sort_key(x), str(x.get("place_id") or ""))):
        pid = p.get("place_id")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        out.append(p)
        if len(out) >= SEARCH_RESULT_LIMIT:
            break
    return out
