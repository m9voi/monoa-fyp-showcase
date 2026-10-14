"""Phase 5C — verified places architecture.

Provider adapters are tested against MOCKED upstream HTTP (httpx.MockTransport):
this proves parsing / normalisation / error mapping / the API contract. It does
NOT prove a live provider account works (see the report's "live provider status").
"""
import asyncio
import json
import os
import pathlib

import httpx
import pytest

import db as db_layer
import main
import places_core as pc
import places_provider as pp

ROOT = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# KLCC / KL Sentral — public landmarks, not the user's location.
KLCC = (3.1578, 101.7117)


def _run(coro):
    return asyncio.run(coro)


def _mock_factory(handler):
    """Return a client_factory that yields an AsyncClient backed by MockTransport."""
    def factory():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)
    return factory


# ===================== taxonomy =====================
def test_every_activity_has_a_complete_mapping():
    assert set(pc.SUPPORTED_ACTIVITIES)  # non-empty
    for act, spec in pc.ACTIVITY_TAXONOMY.items():
        assert spec["label"]
        assert isinstance(spec["osm"], list) and spec["osm"], act
        for tag_expr, match in spec["osm"]:
            assert tag_expr.startswith("["), act
            assert match in ("exact", "broad"), act
        assert spec["google"]["text_query"].strip(), act


def test_taxonomy_precision_guards():
    # a bicycle SHOP is never a cycling place (precision kept from Phase 5C)
    assert all("shop" not in expr for expr, _ in pc.ACTIVITY_TAXONOMY["cycling"]["osm"])
    # Phase 5E: running / cycling DO now include public recreation geography
    # (parks, recreation grounds, nature reserves) — but only as BROAD matches;
    # the Activity Engine's evidence model (activity_core) decides how strongly
    # each ranks. A generic park is never an EXACT match.
    for act in ("running", "cycling"):
        for expr, kind in pc.ACTIVITY_TAXONOMY[act]["osm"]:
            if "leisure\"=\"park" in expr or "recreation_ground" in expr or "nature_reserve" in expr:
                assert kind == "broad", (act, expr)
    # basketball / badminton map only to their own sport
    for act in ("basketball", "badminton"):
        assert any(act in expr for expr, _ in pc.ACTIVITY_TAXONOMY[act]["osm"])
    # tennis / golf require their own facility evidence (no generic-park padding)
    assert all("park" not in expr for expr, _ in pc.ACTIVITY_TAXONOMY["tennis"]["osm"])
    assert all("park" not in expr for expr, _ in pc.ACTIVITY_TAXONOMY["golf"]["osm"])


def test_volleyball_taxonomy_uses_the_official_osm_value():
    # B9.1a: the real provider (Overpass) query mapping for volleyball. The
    # official OSM value is `sport=beachvolleyball` (one word, no underscore).
    vb = pc.ACTIVITY_TAXONOMY["volleyball"]
    exprs = [expr for expr, _ in vb["osm"]]
    joined = " ".join(exprs)
    assert "volleyball" in joined
    assert "beachvolleyball" in joined
    assert "beach_volleyball" not in joined          # NOT the official mapping
    # no generic-park padding, and no invented Google type_hint
    assert all("park" not in e for e in exprs)
    assert vb["google"]["type_hint"] is None
    assert vb["google"]["text_query"].strip() == "volleyball court"
    # the provider's exact-match set owns both official values
    from places_provider import OverpassProvider
    assert OverpassProvider._EXACT_SPORTS["volleyball"] == frozenset({"volleyball", "beachvolleyball"})


# ===================== validators =====================
@pytest.mark.parametrize("raw,expect", [("futsal", "futsal"), ("  Badminton ", "badminton"),
                                        ("SWIMMING", "swimming")])
def test_validate_activity_ok(raw, expect):
    assert pc.validate_activity(raw) == expect


@pytest.mark.parametrize("raw", ["hiking", "gym", "", None, 123, "  "])
def test_validate_activity_rejects(raw):
    with pytest.raises(ValueError):
        pc.validate_activity(raw)


def test_validate_radius():
    assert pc.validate_radius_m(None) == pc.SEARCH_RADIUS_DEFAULT_M
    assert pc.validate_radius_m(5000) == 5000
    assert pc.validate_radius_m(50) == pc.SEARCH_RADIUS_MIN_M       # clamped up
    assert pc.validate_radius_m(9_999_999) == pc.SEARCH_RADIUS_MAX_M  # clamped down
    for bad in (0, -1, float("inf"), float("nan"), "x", True, [1]):
        with pytest.raises(ValueError):
            pc.validate_radius_m(bad)


# ===================== haversine =====================
def test_haversine_known_pair():
    d = pc.haversine_m(3.1578, 101.7117, 3.1339, 101.6869)  # KLCC -> KL Sentral
    assert 3400 < d < 4300


def test_haversine_zero_and_symmetry():
    assert pc.haversine_m(3.0, 101.0, 3.0, 101.0) == 0.0
    a = pc.haversine_m(3.0, 101.0, 3.5, 101.5)
    b = pc.haversine_m(3.5, 101.5, 3.0, 101.0)
    assert abs(a - b) < 1e-6


@pytest.mark.parametrize("args", [
    (float("nan"), 101.0, 3.0, 101.0),
    (3.0, float("inf"), 3.0, 101.0),
    (95.0, 101.0, 3.0, 101.0),
    (3.0, 200.0, 3.0, 101.0),
    ("3.0", 101.0, 3.0, 101.0),
])
def test_haversine_rejects_bad_coords(args):
    with pytest.raises(ValueError):
        pc.haversine_m(*args)


# ===================== normalised model + evidence =====================
def _osm_place(**over):
    base = dict(provider="osm", provider_place_id="node:1", name="Court A",
                latitude=3.16, longitude=101.71, origin_lat=3.15, origin_lng=101.70,
                activity="futsal", activity_match="exact")
    base.update(over)
    return pc.build_place(**base)


def test_place_identity_is_stable_and_composite():
    a = _osm_place()
    b = _osm_place()
    assert a["place_id"] == b["place_id"] == "osm:node:1"
    assert a["provider"] == "osm" and a["provider_place_id"] == "node:1"


def test_place_drops_rows_without_identity():
    assert _osm_place(name=None) is None
    assert _osm_place(name="   ") is None
    assert _osm_place(latitude=999) is None
    assert _osm_place(latitude=float("nan")) is None


def test_missing_optional_fields_stay_null_no_fabrication():
    p = _osm_place()  # no rating / hours / phone / website / status supplied
    assert p["rating"] is None
    assert p["rating_count"] is None
    assert p["opening_hours"] is None
    assert p["open_now"] is None
    assert p["business_status"] is None
    assert p["website"] is None and p["phone"] is None
    # evidence records the ABSENCE explicitly
    assert p["evidence"]["quality"]["source"] is None
    assert p["evidence"]["operational"]["source"] is None
    assert p["evidence"]["hours"]["source"] is None
    assert p["evidence"]["hours"]["has_hours"] is False
    # identity is the ONLY verified flag
    assert p["evidence"]["identity"] == {"source": "osm", "verified": True}
    assert "verified" not in p  # no blanket top-level verified


def test_evidence_records_provider_per_category():
    p = pc.build_place(
        provider="google", provider_place_id="ChIJxyz", name="Arena",
        latitude=3.16, longitude=101.71, origin_lat=3.15, origin_lng=101.70,
        activity="futsal", activity_match="broad",
        rating=4.4, rating_count=210, open_now=True, business_status="OPERATIONAL",
        opening_hours=["Mon: 9am-11pm"], website="https://a.example", phone="+60 3",
    )
    assert p["evidence"]["identity"]["source"] == "google"
    assert p["evidence"]["quality"] == {"source": "google", "rating": 4.4, "rating_count": 210}
    assert p["evidence"]["operational"] == {"source": "google", "status": "OPERATIONAL"}
    assert p["evidence"]["hours"]["source"] == "google" and p["evidence"]["hours"]["open_now"] is True
    assert p["evidence"]["contact"]["source"] == "google"


def test_place_rejects_impossible_evidence_values():
    p = _osm_place(rating=9.9, rating_count=-3, business_status="BANANA", open_now="yes")
    assert p["rating"] is None            # 9.9 > 5 -> dropped
    assert p["rating_count"] is None
    assert p["business_status"] is None   # not a known status -> dropped
    assert p["open_now"] is None          # "yes" is not a bool -> dropped


def test_finalize_sorts_by_distance_dedupes_and_caps():
    raw = []
    for i in range(pc.SEARCH_RESULT_LIMIT + 10):
        raw.append(_osm_place(provider_place_id=f"node:{i}", latitude=3.15 + i * 0.01))
    raw.append(dict(raw[0]))  # duplicate place_id
    out = pc.finalize_results(raw)
    assert len(out) == pc.SEARCH_RESULT_LIMIT
    dists = [p["distance_m"] for p in out]
    assert dists == sorted(dists)
    assert len({p["place_id"] for p in out}) == len(out)


# ===================== nearest-first ordering (Phase 5D acceptance) =====================
def _dist_place(pid: str, distance_m):
    """A minimal normalised place dict with an explicit distance_m for ordering
    tests — bypasses build_place so we can inject missing / invalid values."""
    return {
        "place_id": pid, "provider": "google", "provider_place_id": pid.split(":", 1)[1],
        "name": pid, "latitude": 3.16, "longitude": 101.71, "distance_m": distance_m,
        "distance_kind": "straight_line", "activity": "futsal", "activity_match": "broad",
        "provider_types": [], "rating": 4.0, "rating_count": 10, "opening_hours": None,
        "open_now": None, "business_status": None, "website": None, "phone": None,
        "maps_url": "https://maps.google/?cid=1",
        "evidence": {"identity": {"source": "google", "verified": True},
                     "operational": {"source": None, "status": None},
                     "hours": {"source": None, "has_hours": False, "open_now": None},
                     "quality": {"source": "google", "rating": 4.0, "rating_count": 10},
                     "contact": {"source": None, "website": None, "phone": None}},
    }


def test_nearest_first_exact_spec_example():
    # provider returns 5000, 800, 2500, 1200 -> CoreOS must output 800,1200,2500,5000
    raw = [_dist_place("google:a", 5000), _dist_place("google:b", 800),
           _dist_place("google:c", 2500), _dist_place("google:d", 1200)]
    out = pc.finalize_results(raw)
    assert [p["distance_m"] for p in out] == [800, 1200, 2500, 5000]
    assert [p["place_id"] for p in out] == ["google:b", "google:d", "google:c", "google:a"]


def test_provider_relevance_order_cannot_override_distance():
    # a plausible Google "relevance" order (big brand first) is re-sorted by distance
    raw = [_dist_place("google:brand_far", 9000), _dist_place("google:tiny_near", 300),
           _dist_place("google:mid", 4000)]
    out = pc.finalize_results(raw)
    assert [p["place_id"] for p in out] == ["google:tiny_near", "google:mid", "google:brand_far"]


def test_missing_or_invalid_distance_sorts_last_never_ahead():
    for bad in (None, float("nan"), float("inf"), -1, "x", True):
        raw = [_dist_place("google:bad", bad), _dist_place("google:near", 500),
               _dist_place("google:far", 7000)]
        out = pc.finalize_results(raw)
        assert out[0]["place_id"] == "google:near"
        assert out[1]["place_id"] == "google:far"
        assert out[-1]["place_id"] == "google:bad", bad


def test_ordering_is_provider_neutral():
    for provider in ("google", "osm"):
        raw = [dict(_dist_place("x:c", 3000), provider=provider),
               dict(_dist_place("x:a", 100), provider=provider),
               dict(_dist_place("x:b", 900), provider=provider)]
        out = pc.finalize_results(raw)
        assert [p["distance_m"] for p in out] == [100, 900, 3000], provider


def test_finalize_results_deterministic_tie_break():
    # equal distance -> the order must be reproducible, decided by place_id, not
    # by the order the provider happened to return them in.
    forward = [_dist_place("google:zzz", 1000), _dist_place("google:aaa", 1000),
               _dist_place("google:mmm", 1000)]
    out1 = pc.finalize_results(list(forward))
    out2 = pc.finalize_results(list(reversed(forward)))
    assert [p["place_id"] for p in out1] == ["google:aaa", "google:mmm", "google:zzz"]
    assert [p["place_id"] for p in out1] == [p["place_id"] for p in out2]


def test_sort_does_not_mutate_evidence_or_fields():
    p = _dist_place("google:x", 1234)
    before = json.loads(json.dumps(p))
    pc.finalize_results([p, _dist_place("google:y", 10)])
    assert p == before  # the dict passed in is untouched


def test_endpoint_returns_nearest_first(alice, monkeypatch):
    tc, _ = alice
    scrambled = [_dist_place("google:5k", 5000), _dist_place("google:800", 800),
                 _dist_place("google:2500", 2500), _dist_place("google:1200", 1200)]
    _use_provider(monkeypatch, _StubProvider(result=scrambled))
    b = tc.post("/api/places/search",
                json={"activity": "futsal", "latitude": 3.1578, "longitude": 101.7117}).json()
    assert b["status"] == "OK"
    assert [p["distance_m"] for p in b["places"]] == [800, 1200, 2500, 5000]
    # stable identity is place_id, not position
    assert b["places"][0]["place_id"] == "google:800"


# ===================== Overpass adapter (mocked upstream) =====================
_OVERPASS_OK = {
    "elements": [
        {"type": "node", "id": 11, "lat": 3.16, "lon": 101.71,
         "tags": {"name": "Champion Futsal", "leisure": "pitch", "sport": "futsal",
                  "opening_hours": "Mo-Su 09:00-23:00", "website": "https://cf.my"}},
        {"type": "way", "id": 22, "center": {"lat": 3.155, "lon": 101.70},
         "tags": {"name": "City Sports Hall", "leisure": "sports_centre", "sport": "multi"}},
        {"type": "node", "id": 33, "lat": 3.14, "lon": 101.69, "tags": {"leisure": "pitch"}},  # no name -> dropped
    ]
}


def test_overpass_parses_and_normalises():
    def handler(_req):
        return httpx.Response(200, json=_OVERPASS_OK)
    prov = pp.OverpassProvider(client_factory=_mock_factory(handler))
    out = _run(prov.search_nearby(latitude=KLCC[0], longitude=KLCC[1], activity="futsal", radius_m=8000))
    assert len(out) == 2  # unnamed node dropped
    champ = next(p for p in out if p["name"] == "Champion Futsal")
    assert champ["place_id"] == "osm:node:11"
    assert champ["provider"] == "osm"
    assert champ["activity_match"] == "exact"
    assert champ["opening_hours"] == "Mo-Su 09:00-23:00"
    assert champ["website"] == "https://cf.my"
    assert champ["rating"] is None and champ["open_now"] is None       # OSM has none
    assert champ["evidence"]["quality"]["source"] is None
    hall = next(p for p in out if p["name"] == "City Sports Hall")
    assert hall["activity_match"] == "broad"
    assert hall["latitude"] == 3.155 and hall["longitude"] == 101.70   # way centroid


def test_overpass_empty_is_no_results_not_error():
    prov = pp.OverpassProvider(client_factory=_mock_factory(lambda r: httpx.Response(200, json={"elements": []})))
    assert _run(prov.search_nearby(latitude=KLCC[0], longitude=KLCC[1], activity="futsal", radius_m=8000)) == []


@pytest.mark.parametrize("status,kind", [
    (429, pc.PROVIDER_RATE_LIMITED),
    (503, pc.PROVIDER_UNAVAILABLE),
    (500, pc.PROVIDER_UNAVAILABLE),
])
def test_overpass_http_error_mapping(status, kind):
    prov = pp.OverpassProvider(client_factory=_mock_factory(lambda r: httpx.Response(status, text="x")))
    with pytest.raises(pc.ProviderError) as ei:
        _run(prov.search_nearby(latitude=KLCC[0], longitude=KLCC[1], activity="futsal", radius_m=8000))
    assert ei.value.kind == kind


def test_overpass_timeout_mapping():
    def handler(_req):
        raise httpx.ReadTimeout("slow")
    prov = pp.OverpassProvider(client_factory=_mock_factory(handler))
    with pytest.raises(pc.ProviderError) as ei:
        _run(prov.search_nearby(latitude=KLCC[0], longitude=KLCC[1], activity="futsal", radius_m=8000))
    assert ei.value.kind == pc.PROVIDER_TIMEOUT


def test_overpass_malformed_json_mapping():
    prov = pp.OverpassProvider(client_factory=_mock_factory(lambda r: httpx.Response(200, text="<<not json>>")))
    with pytest.raises(pc.ProviderError) as ei:
        _run(prov.search_nearby(latitude=KLCC[0], longitude=KLCC[1], activity="futsal", radius_m=8000))
    assert ei.value.kind == pc.INVALID_PROVIDER_RESPONSE


def test_overpass_query_has_no_shop_bicycle_for_cycling():
    prov = pp.OverpassProvider()
    q = prov._build_query(KLCC[0], KLCC[1], "cycling", 8000)
    assert "shop" not in q and "sport" in q and "around:8000" in q


# ===================== Google adapter (mocked upstream) =====================
_GOOGLE_OK = {
    "places": [
        {"id": "ChIJ_futsal_1", "displayName": {"text": "Elite Futsal"},
         "location": {"latitude": 3.161, "longitude": 101.712},
         "formattedAddress": "1 Jalan X", "rating": 4.6, "userRatingCount": 402,
         "currentOpeningHours": {"openNow": True},
         "regularOpeningHours": {"weekdayDescriptions": ["Monday: 9:00 AM – 11:00 PM"]},
         "businessStatus": "OPERATIONAL", "websiteUri": "https://elite.my",
         "nationalPhoneNumber": "03-1234 5678", "googleMapsUri": "https://maps.google/?cid=1",
         "types": ["sports_complex"]},
    ]
}


def test_google_not_configured_without_key():
    prov = pp.GooglePlacesProvider(api_key="")
    with pytest.raises(pc.ProviderError) as ei:
        _run(prov.search_nearby(latitude=KLCC[0], longitude=KLCC[1], activity="futsal", radius_m=8000))
    assert ei.value.kind == pc.PROVIDER_NOT_CONFIGURED


def test_google_parses_rich_fields():
    captured = {}

    def handler(req):
        captured["auth"] = req.headers.get("X-Goog-Api-Key")
        captured["mask"] = req.headers.get("X-Goog-FieldMask")
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json=_GOOGLE_OK)

    prov = pp.GooglePlacesProvider(api_key="TEST_KEY_do_not_log", client_factory=_mock_factory(handler))
    out = _run(prov.search_nearby(latitude=KLCC[0], longitude=KLCC[1], activity="futsal", radius_m=8000))
    assert captured["auth"] == "TEST_KEY_do_not_log"       # key in header, server-side
    assert "places.rating" in captured["mask"]             # field mask limits cost
    assert captured["body"]["textQuery"] == "futsal court"
    p = out[0]
    assert p["place_id"] == "google:ChIJ_futsal_1"
    assert p["rating"] == 4.6 and p["rating_count"] == 402
    assert p["open_now"] is True
    assert p["business_status"] == "OPERATIONAL"
    assert p["website"] == "https://elite.my" and p["phone"] == "03-1234 5678"
    assert p["maps_url"] == "https://maps.google/?cid=1"
    assert p["evidence"]["quality"]["source"] == "google"
    assert p["evidence"]["operational"]["status"] == "OPERATIONAL"


@pytest.mark.parametrize("status,payload,kind", [
    (429, {}, pc.PROVIDER_RATE_LIMITED),
    (403, {}, pc.PROVIDER_NOT_CONFIGURED),
    (401, {}, pc.PROVIDER_NOT_CONFIGURED),
    (500, {}, pc.PROVIDER_UNAVAILABLE),
    (200, {"error": {"status": "RESOURCE_EXHAUSTED"}}, pc.PROVIDER_RATE_LIMITED),
    (200, {"error": {"status": "PERMISSION_DENIED"}}, pc.PROVIDER_NOT_CONFIGURED),
])
def test_google_error_mapping(status, payload, kind):
    prov = pp.GooglePlacesProvider(api_key="K", client_factory=_mock_factory(
        lambda r: httpx.Response(status, json=payload)))
    with pytest.raises(pc.ProviderError) as ei:
        _run(prov.search_nearby(latitude=KLCC[0], longitude=KLCC[1], activity="futsal", radius_m=8000))
    assert ei.value.kind == kind


def test_google_malformed_json():
    prov = pp.GooglePlacesProvider(api_key="K", client_factory=_mock_factory(
        lambda r: httpx.Response(200, text="not-json")))
    with pytest.raises(pc.ProviderError) as ei:
        _run(prov.search_nearby(latitude=KLCC[0], longitude=KLCC[1], activity="futsal", radius_m=8000))
    assert ei.value.kind == pc.INVALID_PROVIDER_RESPONSE


# ===================== provider selection =====================
def test_provider_selection(monkeypatch):
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    monkeypatch.setenv("COREOS_PLACES_PROVIDER", "auto")
    assert pp.resolve_provider_name() == "osm"
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "x")
    assert pp.resolve_provider_name() == "google"
    monkeypatch.setenv("COREOS_PLACES_PROVIDER", "osm")
    assert pp.resolve_provider_name() == "osm"
    monkeypatch.setenv("COREOS_PLACES_PROVIDER", "google")
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    assert pp.resolve_provider_name() == "google"  # explicit choice, will report NOT_CONFIGURED


# ===================== endpoint contract =====================
class _StubProvider:
    name = "osm"
    attribution = "© OpenStreetMap contributors"

    def __init__(self, result=None, error=None):
        self._result, self._error = result or [], error

    async def search_nearby(self, **_kw):
        if self._error:
            raise pc.ProviderError(self._error)
        return self._result


def _use_provider(monkeypatch, stub):
    monkeypatch.setattr(main.places_provider, "get_provider", lambda *a, **k: stub)


def test_places_search_requires_auth(client):
    assert client.post("/api/places/search",
                       json={"activity": "futsal", "latitude": 3.1, "longitude": 101.7}).status_code == 401


def test_places_search_ok(alice, monkeypatch):
    tc, _ = alice
    stub = _StubProvider(result=[
        _osm_place(provider_place_id="node:2", latitude=3.20),   # farther
        _osm_place(provider_place_id="node:1", latitude=3.158),  # nearer
    ])
    _use_provider(monkeypatch, stub)
    r = tc.post("/api/places/search", json={"activity": "futsal", "latitude": 3.1578, "longitude": 101.7117})
    assert r.status_code == 200
    b = r.json()
    assert b["status"] == "OK" and b["provider"] == "osm"
    assert b["attribution"] == "© OpenStreetMap contributors"
    assert [p["provider_place_id"] for p in b["places"]] == ["node:1", "node:2"]  # distance-sorted
    assert b["error"] is None


def test_places_search_no_results(alice, monkeypatch):
    tc, _ = alice
    _use_provider(monkeypatch, _StubProvider(result=[]))
    b = tc.post("/api/places/search", json={"activity": "futsal", "latitude": 3.1, "longitude": 101.7}).json()
    assert b["status"] == "NO_RESULTS" and b["places"] == [] and b["error"] is None


@pytest.mark.parametrize("kind", list(pc.PROVIDER_ERROR_KINDS))
def test_places_search_provider_error_is_normalised(alice, monkeypatch, kind):
    tc, _ = alice
    _use_provider(monkeypatch, _StubProvider(error=kind))
    r = tc.post("/api/places/search", json={"activity": "futsal", "latitude": 3.1, "longitude": 101.7})
    assert r.status_code == 200
    b = r.json()
    assert b["status"] == "PROVIDER_ERROR" and b["error"] == kind and b["places"] == []


@pytest.mark.parametrize("body", [
    {"activity": "futsal", "latitude": 200, "longitude": 101.7},
    {"activity": "futsal", "latitude": 3.1, "longitude": 999},
    {"activity": "hiking", "latitude": 3.1, "longitude": 101.7},
    {"activity": "futsal", "latitude": 3.1, "longitude": 101.7, "radius_m": 0},
    {"activity": "futsal", "latitude": 3.1, "longitude": 101.7, "radius_m": -5},
    {"latitude": 3.1, "longitude": 101.7},
    {"activity": "futsal", "longitude": 101.7},
])
def test_places_search_validation_422(alice, monkeypatch, body):
    tc, _ = alice
    _use_provider(monkeypatch, _StubProvider(result=[]))
    assert tc.post("/api/places/search", json=body).status_code == 422


def test_places_search_rejects_non_finite_raw(alice, monkeypatch):
    tc, _ = alice
    _use_provider(monkeypatch, _StubProvider(result=[]))
    r = tc.post("/api/places/search",
                content=b'{"activity":"futsal","latitude":1e999,"longitude":101.7}',
                headers={"Content-Type": "application/json"})
    assert r.status_code == 422


def test_places_search_bounds_result_count(alice, monkeypatch):
    tc, _ = alice
    many = [_osm_place(provider_place_id=f"node:{i}", latitude=3.10 + i * 0.001) for i in range(60)]
    _use_provider(monkeypatch, _StubProvider(result=many))
    b = tc.post("/api/places/search", json={"activity": "futsal", "latitude": 3.1, "longitude": 101.7}).json()
    assert len(b["places"]) == pc.SEARCH_RESULT_LIMIT


def test_radius_above_max_is_clamped_not_rejected(alice, monkeypatch):
    tc, _ = alice
    _use_provider(monkeypatch, _StubProvider(result=[]))
    b = tc.post("/api/places/search",
                json={"activity": "futsal", "latitude": 3.1, "longitude": 101.7, "radius_m": 999999}).json()
    assert b["radius_m"] == pc.SEARCH_RADIUS_MAX_M


# ===================== privacy =====================
def test_places_search_does_not_log_coordinates(alice, monkeypatch, capfd):
    tc, _ = alice
    _use_provider(monkeypatch, _StubProvider(error=pc.PROVIDER_TIMEOUT))
    lat, lng = 12.3456789, -98.7654321
    tc.post("/api/places/search", json={"activity": "futsal", "latitude": lat, "longitude": lng})
    out = capfd.readouterr()
    combined = out.out + out.err
    assert "12.3456789" not in combined and "98.7654321" not in combined
    assert "PROVIDER_TIMEOUT" in combined  # the KIND is logged, not the coords


def test_places_search_is_stateless(alice, monkeypatch):
    tc, _ = alice
    _use_provider(monkeypatch, _StubProvider(result=[_osm_place()]))
    c = db_layer.get_connection()
    tables_before = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    head = c.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    c.close()
    tc.post("/api/places/search", json={"activity": "futsal", "latitude": 3.1, "longitude": 101.7})
    c = db_layer.get_connection()
    assert c.execute("SELECT COUNT(*) FROM map_locations").fetchone()[0] == 0
    assert {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")} == tables_before
    assert c.execute("SELECT version_num FROM alembic_version").fetchone()[0] == head
    c.close()


def test_places_search_response_never_carries_provider_key(alice, monkeypatch):
    tc, _ = alice
    _use_provider(monkeypatch, _StubProvider(result=[_osm_place()]))
    r = tc.post("/api/places/search", json={"activity": "futsal", "latitude": 3.1, "longitude": 101.7})
    assert "api_key" not in r.text.lower() and "x-goog" not in r.text.lower()


def test_place_search_does_not_touch_ai(alice, monkeypatch):
    tc, _ = alice
    called = {"n": 0}
    monkeypatch.setattr(main.AIService, "interpret",
                        staticmethod(lambda *_a, **_k: called.__setitem__("n", called["n"] + 1)))
    _use_provider(monkeypatch, _StubProvider(result=[_osm_place()]))
    tc.post("/api/places/search", json={"activity": "futsal", "latitude": 3.1, "longitude": 101.7})
    assert called["n"] == 0


# ===================== frontend source guards =====================
_MAP = ROOT / "app" / "map" / "page.tsx"
_TYPES = ROOT / "app" / "lib" / "types.ts"


def test_map_page_uses_places_search_via_post_body():
    src = _MAP.read_text(encoding="utf-8")
    assert '/api/places/search' in src
    assert 'method: "POST"' in src
    # coordinates are NOT in a URL
    assert "&lat=" not in src and "lat=${" not in src and "lng=${" not in src


def test_map_page_dropped_misleading_trust_copy():
    src = _MAP.read_text(encoding="utf-8")
    assert "Verified Location" not in src          # replaced with evidence-appropriate copy
    assert "Identity from" in src                  # provider-scoped identity claim
    assert "2.9213" not in src and "101.6559" not in src


def test_types_expose_evidence_model():
    src = _TYPES.read_text(encoding="utf-8")
    for token in ("PlaceEvidence", "CorePlace", "PlacesSearchResponse",
                  "activity_match", "PROVIDER_NOT_CONFIGURED"):
        assert token in src


def test_no_provider_key_var_in_frontend():
    for f in list((ROOT / "app").rglob("*.ts")) + list((ROOT / "app").rglob("*.tsx")):
        t = f.read_text(encoding="utf-8")
        assert "GOOGLE_PLACES_API_KEY" not in t
        assert "NEXT_PUBLIC_GOOGLE" not in t
