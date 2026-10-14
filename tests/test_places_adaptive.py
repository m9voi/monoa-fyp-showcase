"""B10 — Map/Explore adaptive parity for POST /api/places/search.

With `adaptive_radius: true` the endpoint runs the SAME staged policy as
/api/activities/search: a genuine provider request at 8 km, then (only if < 3
valid, in-radius, deduped results) 12 km, then (only if still < 3) 20 km. Never a
4th rung, never 20 km first, stop at the first rung that clears the bar. Each
rung's response is authoritative for that rung (not accumulated) and is strictly
gated by places_core.within_radius before finalize_results.

Without the flag the endpoint keeps its exact Phase 5C behaviour (single
search_nearby at validate_radius_m(radius_m), finalize_results, NO radius gate).

Deterministic: a stub provider that records the radius sequence. Temp DB via
conftest — the real coreos.db is never touched.
"""

import pytest

import main
import places_core as pc

LADDER = pc.ACTIVITY_RADIUS_LADDER_M          # (8000, 12000, 20000)


# --------------------------------------------------------------------------- #
# a normalised CoreOS place dict with a caller-chosen straight-line distance
# --------------------------------------------------------------------------- #
def _place(name, dist_m, *, pid=None):
    return {
        "place_id": pid or f"osm:{name.lower().replace(' ', '_')}",
        "provider": "osm", "provider_place_id": "way:1", "name": name,
        "latitude": 3.16, "longitude": 101.71, "address": None,
        "distance_m": float(dist_m), "distance_kind": "straight_line",
        "activity": "futsal", "activity_match": "exact", "provider_types": ["sport=futsal"],
        "rating": None, "rating_count": None, "opening_hours": None, "open_now": None,
        "business_status": None, "website": None, "phone": None,
        "maps_url": "https://www.openstreetmap.org/",
        "evidence": {"identity": {"source": "osm", "verified": True},
                     "operational": {"source": None, "status": None},
                     "hours": {"source": None, "has_hours": False, "open_now": None},
                     "quality": {"source": None, "rating": None, "rating_count": None},
                     "contact": {"source": None, "website": None, "phone": None}},
    }


class _RadiusStub:
    """Records every radius it is asked for. ``hard_filter=True`` mimics Overpass
    (returns only elements within the queried radius); ``hard_filter=False``
    mimics Google Text Search (returns the full set every call). ``error=True``
    raises a provider timeout on the first call."""
    name = "osm"
    attribution = "© OpenStreetMap contributors"

    def __init__(self, places, *, hard_filter=True, error=False):
        self._places = places
        self._hard = hard_filter
        self._error = error
        self.radii = []

    async def search_nearby(self, *, latitude, longitude, activity, radius_m):
        self.radii.append(radius_m)
        if self._error:
            raise pc.ProviderError(pc.PROVIDER_TIMEOUT)
        if self._hard:
            return [dict(p) for p in self._places if p["distance_m"] <= radius_m]
        return [dict(p) for p in self._places]


def _use(monkeypatch, stub):
    monkeypatch.setattr(main.places_provider, "get_provider", lambda *a, **k: stub)


def _adaptive(tc, **over):
    body = {"activity": "futsal", "latitude": 3.16, "longitude": 101.71, "adaptive_radius": True}
    body.update(over)
    r = tc.post("/api/places/search", json=body)
    return r


# --------------------------------------------------------------------------- #
# staged provider sequence
# --------------------------------------------------------------------------- #
def test_8km_sufficient_makes_exactly_one_request(alice, monkeypatch):
    tc, _ = alice
    stub = _RadiusStub([_place("A", 1000), _place("B", 3000), _place("C", 6000), _place("D", 7500)])
    _use(monkeypatch, stub)
    b = _adaptive(tc).json()
    assert b["status"] == "OK" and b["radius_m"] == 8000
    assert stub.radii == [8000]
    assert len(b["places"]) >= 3
    assert all(p["distance_m"] <= 8000 for p in b["places"])


def test_expands_to_12km_then_stops(alice, monkeypatch):
    tc, _ = alice
    stub = _RadiusStub([_place("n1", 3000), _place("n2", 5000), _place("m1", 10000), _place("m2", 11000)])
    _use(monkeypatch, stub)
    b = _adaptive(tc).json()
    assert b["status"] == "OK" and b["radius_m"] == 12000
    assert stub.radii == [8000, 12000]
    assert len(b["places"]) >= 3
    assert all(p["distance_m"] <= 12000 for p in b["places"])


def test_expands_to_20km_when_8_and_12_thin(alice, monkeypatch):
    tc, _ = alice
    stub = _RadiusStub([_place("near", 3000), _place("far1", 15000), _place("far2", 18000)])
    _use(monkeypatch, stub)
    b = _adaptive(tc).json()
    assert b["status"] == "OK" and b["radius_m"] == 20000
    assert stub.radii == [8000, 12000, 20000]
    assert len(b["places"]) == 3


def test_never_a_fourth_request_and_never_20km_first(alice, monkeypatch):
    tc, _ = alice
    for places in ([], [_place("x", 500)], [_place("a", 1), _place("b", 2), _place("c", 3)]):
        stub = _RadiusStub(list(places))
        _use(monkeypatch, stub)
        _adaptive(tc)
        assert stub.radii[0] == 8000
        assert stub.radii == sorted(stub.radii)
        assert len(stub.radii) <= len(LADDER)


def test_20km_partial_is_an_honest_result(alice, monkeypatch):
    tc, _ = alice
    stub = _RadiusStub([_place("only", 3000), _place("toofar", 25000)])
    _use(monkeypatch, stub)
    b = _adaptive(tc).json()
    assert b["radius_m"] == 20000 and stub.radii == [8000, 12000, 20000]
    assert b["status"] == "OK" and len(b["places"]) == 1
    assert all(p["distance_m"] <= 20000 for p in b["places"])


def test_nothing_in_20km_is_truthful_no_results(alice, monkeypatch):
    tc, _ = alice
    stub = _RadiusStub([_place("way1", 22000), _place("way2", 30000)])
    _use(monkeypatch, stub)
    b = _adaptive(tc).json()
    assert b["status"] == "NO_RESULTS" and b["places"] == [] and b["error"] is None
    assert b["radius_m"] == 20000 and stub.radii == [8000, 12000, 20000]


def test_provider_error_is_truthful_and_bounded(alice, monkeypatch):
    tc, _ = alice
    stub = _RadiusStub([_place("A", 1000)], error=True)
    _use(monkeypatch, stub)
    b = _adaptive(tc).json()
    assert b["status"] == "PROVIDER_ERROR" and b["error"] == pc.PROVIDER_TIMEOUT
    assert b["places"] == [] and stub.radii == [8000]     # stopped at the failing rung


# --------------------------------------------------------------------------- #
# strict within-radius per rung + no cross-rung accumulation
# --------------------------------------------------------------------------- #
def test_google_like_soft_bias_outlier_is_rejected(alice, monkeypatch):
    tc, _ = alice
    # provider returns a 9 km + a 40 km entry at EVERY rung; 3 sit inside 8 km.
    stub = _RadiusStub([
        _place("in1", 1000), _place("in2", 4000), _place("in3", 7000),
        _place("just_out", 9000), _place("way_out", 40000),
    ], hard_filter=False)
    _use(monkeypatch, stub)
    b = _adaptive(tc).json()
    assert b["radius_m"] == 8000 and stub.radii == [8000]
    names = {p["name"] for p in b["places"]}
    assert "just_out" not in names and "way_out" not in names
    assert all(p["distance_m"] <= 8000 for p in b["places"])


def test_each_rung_is_authoritative_not_accumulated(alice, monkeypatch):
    tc, _ = alice
    stub = _RadiusStub([_place("near", 2000), _place("far1", 15000), _place("far2", 17000)], hard_filter=False)
    _use(monkeypatch, stub)
    b = _adaptive(tc).json()
    # 8km: 1 in radius -> widen; 12km: 1 -> widen; 20km: 3 -> stop.
    assert stub.radii == [8000, 12000, 20000] and b["radius_m"] == 20000
    assert {p["name"] for p in b["places"]} == {"near", "far1", "far2"}


@pytest.mark.parametrize("bad", [None, "far", float("nan"), float("inf"), -10, True])
def test_within_radius_drops_unprovable_distances_endpoint(alice, monkeypatch, bad):
    tc, _ = alice
    good = [_place(f"g{i}", 1000 + i) for i in range(3)]
    bad_p = _place("bad", 0)
    bad_p["distance_m"] = bad
    stub = _RadiusStub(good + [bad_p], hard_filter=False)
    _use(monkeypatch, stub)
    b = _adaptive(tc).json()
    assert "bad" not in {p["name"] for p in b["places"]}


def test_endpoint_boundary_and_tolerance(alice, monkeypatch):
    tc, _ = alice
    keep_on = _place("on_ring", 8000.0)
    keep_tol = _place("in_tol", 8000.0 + pc.RADIUS_BOUNDARY_TOLERANCE_M)
    drop_past = _place("past_tol", 8000.0 + pc.RADIUS_BOUNDARY_TOLERANCE_M + 1)
    stub = _RadiusStub([keep_on, keep_tol, drop_past, _place("near", 500)], hard_filter=False)
    _use(monkeypatch, stub)
    b = _adaptive(tc).json()
    got = {p["name"] for p in b["places"]}
    assert {"on_ring", "in_tol", "near"} <= got
    assert "past_tol" not in got


def test_dedupe_and_deterministic_order_preserved(alice, monkeypatch):
    tc, _ = alice
    stub = _RadiusStub([
        _place("Court", 4000, pid="osm:court"),
        _place("Court dup", 4000, pid="osm:court"),
        _place("Near", 1000, pid="osm:near"),
        _place("Mid", 2500, pid="osm:mid"),
    ])
    _use(monkeypatch, stub)
    b = _adaptive(tc).json()
    ids = [p["place_id"] for p in b["places"]]
    assert len(ids) == len(set(ids))
    assert [p["name"] for p in b["places"]] == ["Near", "Mid", "Court"]   # nearest-first


# --------------------------------------------------------------------------- #
# non-adaptive callers unchanged  +  mutual exclusivity
# --------------------------------------------------------------------------- #
def test_non_adaptive_default_behaviour_is_unchanged(alice, monkeypatch):
    tc, _ = alice
    # provider returns a 9 km entry; the legacy path does NOT drop it (no gate)
    stub = _RadiusStub([_place("in", 2000), _place("beyond_default", 9000)], hard_filter=False)
    _use(monkeypatch, stub)
    b = tc.post("/api/places/search", json={"activity": "futsal", "latitude": 3.16, "longitude": 101.71}).json()
    assert b["radius_m"] == pc.SEARCH_RADIUS_DEFAULT_M            # 8000, the documented default
    assert stub.radii == [8000]                                   # exactly one call, no ladder
    assert "beyond_default" in {p["name"] for p in b["places"]}   # legacy: no within_radius gate


def test_non_adaptive_still_honours_a_supplied_radius(alice, monkeypatch):
    tc, _ = alice
    stub = _RadiusStub([_place("a", 1000)], hard_filter=False)
    _use(monkeypatch, stub)
    b = tc.post("/api/places/search",
                json={"activity": "futsal", "latitude": 3.16, "longitude": 101.71, "radius_m": 15000}).json()
    assert b["radius_m"] == 15000 and stub.radii == [15000]


def test_adaptive_and_fixed_radius_together_is_422(alice, monkeypatch):
    tc, _ = alice
    _use(monkeypatch, _RadiusStub([]))
    r = tc.post("/api/places/search", json={
        "activity": "futsal", "latitude": 3.16, "longitude": 101.71,
        "adaptive_radius": True, "radius_m": 12000,
    })
    assert r.status_code == 422


def test_adaptive_flag_must_be_boolean_true_not_truthy(alice, monkeypatch):
    tc, _ = alice
    stub = _RadiusStub([_place("a", 1000), _place("b", 2000), _place("c", 3000), _place("d", 4000)])
    _use(monkeypatch, stub)
    # a non-`true` value falls through to the legacy single-shot path
    b = tc.post("/api/places/search", json={
        "activity": "futsal", "latitude": 3.16, "longitude": 101.71, "adaptive_radius": "yes",
    }).json()
    assert stub.radii == [8000] and b["radius_m"] == pc.SEARCH_RADIUS_DEFAULT_M


def test_adaptive_upstream_fanout_is_bounded_to_the_ladder(alice, monkeypatch):
    tc, _ = alice
    stub = _RadiusStub([])          # nothing anywhere -> runs every rung
    _use(monkeypatch, stub)
    _adaptive(tc)
    assert len(stub.radii) == len(LADDER) == 3      # never a storm, never a 4th
