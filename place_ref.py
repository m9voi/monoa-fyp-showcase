"""CoreOS — provider-neutral place reference (Phase 5G: Map <-> Schedule).

A Schedule event may link a REAL place. The canonical persisted relationship is
a small, STABLE, provider-neutral identity — never a bare display name, never
volatile facts (rating / open_now / today's hours / business status).

    {
      "provider":          "google" | "osm",
      "provider_place_id":  "ChIJ..."            (google)  |  "node:123" / "way:9" (osm)
      "name":              "Cyberjaya Lake Gardens",
      "address":           "Persiaran ...",       (optional snapshot)
      "latitude":          2.92,
      "longitude":         101.65
    }

`normalize_place_ref` validates the shape server-side (never trust arbitrary
frontend metadata) and returns the columns to persist, or raises ValueError.
"""
from __future__ import annotations

import math
import re
from typing import Any, Optional

PLACE_PROVIDERS = ("google", "osm")

_NAME_MAX = 200
_ADDR_MAX = 300
_PID_MAX = 128

# google place ids are opaque url-safe tokens; osm ids are "<type>:<int>".
_GOOGLE_PID_RE = re.compile(r"^[A-Za-z0-9_\-]{4,128}$")
_OSM_PID_RE = re.compile(r"^(node|way|relation):[0-9]{1,20}$")

_LAT_MIN, _LAT_MAX = -90.0, 90.0
_LNG_MIN, _LNG_MAX = -180.0, 180.0


def _finite(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _clean_str(v: Any, limit: int) -> Optional[str]:
    if not isinstance(v, str):
        return None
    s = v.strip()
    return s[:limit] if s else None


def normalize_place_ref(raw: Any) -> dict:
    """Validate a client-supplied place reference. Returns a dict with the
    persisted column values:
      {place_provider, place_provider_id, location, place_address,
       place_lat, place_lng}
    Raises ValueError (message is safe for a 422 detail; never contains coords).
    """
    if not isinstance(raw, dict):
        raise ValueError("place must be an object.")

    provider = str(raw.get("provider") or "").strip().lower()
    if provider not in PLACE_PROVIDERS:
        raise ValueError("place.provider must be one of: " + ", ".join(PLACE_PROVIDERS) + ".")

    pid = _clean_str(raw.get("provider_place_id"), _PID_MAX)
    if not pid:
        raise ValueError("place.provider_place_id is required.")
    # accept an optional "google:" / "osm:" prefix and strip it
    if pid.lower().startswith(provider + ":"):
        pid = pid[len(provider) + 1:]
    if provider == "google" and not _GOOGLE_PID_RE.match(pid):
        raise ValueError("place.provider_place_id is not a valid Google place id.")
    if provider == "osm" and not _OSM_PID_RE.match(pid):
        raise ValueError("place.provider_place_id is not a valid OSM id (expected 'node:123').")

    name = _clean_str(raw.get("name"), _NAME_MAX)
    if not name:
        raise ValueError("place.name is required.")

    lat = _finite(raw.get("latitude"))
    lng = _finite(raw.get("longitude"))
    if lat is None or not (_LAT_MIN <= lat <= _LAT_MAX):
        raise ValueError("place.latitude must be a number between -90 and 90.")
    if lng is None or not (_LNG_MIN <= lng <= _LNG_MAX):
        raise ValueError("place.longitude must be a number between -180 and 180.")

    return {
        "place_provider": provider,
        "place_provider_id": pid,
        "location": name,                      # the human place NAME snapshot
        "place_address": _clean_str(raw.get("address"), _ADDR_MAX),
        "place_lat": lat,
        "place_lng": lng,
    }


def place_out(row: dict) -> Optional[dict]:
    """Build the consumer-facing nested `place` object from a schedule_events row,
    or None when the event has no linked place. Only stable identity + snapshot —
    no volatile provider facts."""
    prov = row.get("place_provider")
    pid = row.get("place_provider_id")
    lat = row.get("place_lat")
    lng = row.get("place_lng")
    if not prov or not pid or lat is None or lng is None:
        return None
    return {
        "provider": prov,
        "provider_place_id": pid,
        "place_id": f"{prov}:{pid}",
        "name": row.get("location"),
        "address": row.get("place_address"),
        "latitude": lat,
        "longitude": lng,
    }
