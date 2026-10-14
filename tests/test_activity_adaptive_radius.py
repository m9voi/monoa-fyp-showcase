"""B9 / B9.1 — Activity adaptive nearby-search radius ladder (8 km -> 12 km -> 20 km).

B9.1 makes the ladder a TRUE staged provider search: one real provider request
per rung. The search issues a request at 8 km; only if fewer than 3 valid,
deduplicated, category-relevant, IN-RADIUS results remain does it issue a fresh
request at 12 km; only if still short does it issue one at 20 km. It never issues
a 4th request and never starts at 20 km. A strict application-side straight-line
distance filter (places_core.within_radius) still drops anything a provider
returns outside the active radius. No user-facing radius control exists.

Deterministic: a stub provider that records the exact radius sequence it was
asked for (no live Overpass/Google). Real coreos.db is never touched (temp DB via
conftest).
"""


import pytest

import main
import places_core as pc
import activity_core as ac


LADDER = pc.ACTIVITY_RADIUS_LADDER_M          # (8000, 12000, 20000)


# --------------------------------------------------------------------------- #
# a stub provider that records the radii it was asked for, in order
# --------------------------------------------------------------------------- #
def _badminton_place(name, dist_m, *, pid=None, ptypes=("sport=badminton",), match="exact"):
    """A normalised CoreOS place dict with a caller-chosen straight-line distance.
    (The stub bypasses build_place, exactly like the existing engine tests.)"""
    return {
        "place_id": pid or f"osm:{name.lower().replace(' ', '_')}",
        "provider": "osm", "provider_place_id": "x", "name": name,
        "latitude": 3.16, "longitude": 101.71, "address": None,
        "distance_m": dist_m, "distance_kind": "straight_line",
        "activity": "badminton", "activity_match": match, "provider_types": list(ptypes),
        "rating": None, "rating_count": None, "opening_hours": None, "open_now": None,
        "business_status": None, "website": None, "phone": None,
        "maps_url": "https://maps.google/?cid=1",
        "evidence": {"identity": {"source": "osm", "verified": True},
                     "operational": {"source": None, "status": None},
                     "hours": {"source": None, "has_hours": False, "open_now": None},
                     "quality": {"source": None, "rating": None, "rating_count": None},
                     "contact": {"source": None, "website": None, "phone": None}},
    }


def _place(activity, name, dist_m, ptypes, *, pid=None, match="exact"):
    p = _badminton_place(name, dist_m, pid=pid, ptypes=ptypes, match=match)
    p["activity"] = activity
    return p


class _RadiusStub:
    """B9.1 issues ONE real provider request per rung and records the radius each
    time. ``hard_filter=True`` mimics a provider that itself returns only
    in-radius elements (Overpass ``around:``); ``hard_filter=False`` mimics one
    that only *biases* (Google Text Search) and returns the full set every call —
    the app-side within_radius gate must then enforce the rung. ``error=True``
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


def _search(tc, activity_id="badminton"):
    body = {"activity_id": activity_id,
            "origin": {"latitude": 3.16, "longitude": 101.71, "source": "device"}}
    r = tc.post("/api/activities/search", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------- #
# 1. 8 km already has >= 3 valid results -> ONE request, no expansion
# --------------------------------------------------------------------------- #
def test_8km_sufficient_makes_exactly_one_request(alice, monkeypatch):
    tc, _ = alice
    stub = _RadiusStub([
        _badminton_place("A", 2000), _badminton_place("B", 3500),
        _badminton_place("C", 6000), _badminton_place("D", 7500),
    ])
    _use(monkeypatch, stub)
    b = _search(tc)
    assert b["status"] == "OK"
    assert b["radius_m"] == 8000
    assert stub.radii == [8000]                        # genuine: one staged request
    assert len(b["candidates"]) >= 3
    assert all(c["distance_m"] <= 8000 for c in b["candidates"])


# --------------------------------------------------------------------------- #
# 2. 8 km insufficient, 12 km sufficient -> requests [8000, 12000], then stops
# --------------------------------------------------------------------------- #
def test_expands_to_12km_and_stops(alice, monkeypatch):
    tc, _ = alice
    stub = _RadiusStub([
        _badminton_place("Near1", 3000), _badminton_place("Near2", 5000),
        _badminton_place("Mid1", 10000), _badminton_place("Mid2", 11000),
    ])
    _use(monkeypatch, stub)
    b = _search(tc)
    assert b["status"] == "OK"
    assert b["radius_m"] == 12000
    assert stub.radii == [8000, 12000]                 # one real widen, then stop
    assert len(b["candidates"]) >= 3
    assert all(c["distance_m"] <= 12000 for c in b["candidates"])


# --------------------------------------------------------------------------- #
# 3. 8 and 12 km insufficient -> requests [8000, 12000, 20000]
# --------------------------------------------------------------------------- #
def test_expands_to_20km_when_8_and_12_thin(alice, monkeypatch):
    tc, _ = alice
    stub = _RadiusStub([
        _badminton_place("Near", 3000),
        _badminton_place("Far1", 15000), _badminton_place("Far2", 18000),
    ])
    _use(monkeypatch, stub)
    b = _search(tc)
    assert b["status"] == "OK"
    assert b["radius_m"] == 20000
    assert stub.radii == [8000, 12000, 20000]
    assert len(b["candidates"]) == 3


# --------------------------------------------------------------------------- #
# 4. 20 km still insufficient -> honest partial, NO 4th request
# --------------------------------------------------------------------------- #
def test_20km_insufficient_returns_honest_partial_and_never_a_fourth_request(alice, monkeypatch):
    tc, _ = alice
    stub = _RadiusStub([_badminton_place("Only", 3000), _badminton_place("TooFar", 25000)])
    _use(monkeypatch, stub)
    b = _search(tc)
    assert b["radius_m"] == 20000
    assert stub.radii == [8000, 12000, 20000]          # exactly three, never four
    assert b["status"] == "OK" and len(b["candidates"]) == 1
    assert all(c["distance_m"] <= 20000 for c in b["candidates"])


def test_nothing_in_20km_is_truthful_no_results(alice, monkeypatch):
    tc, _ = alice
    stub = _RadiusStub([_badminton_place("Way1", 22000), _badminton_place("Way2", 30000)])
    _use(monkeypatch, stub)
    b = _search(tc)
    assert b["status"] == "NO_RESULTS"
    assert b["candidates"] == [] and b["error"] is None
    assert b["radius_m"] == 20000
    assert stub.radii == [8000, 12000, 20000]


def test_never_requests_twenty_km_first(alice, monkeypatch):
    # whatever the data, the FIRST upstream request is always the 8 km rung.
    for places in (
        [],
        [_badminton_place("x", 500)],
        [_badminton_place("a", 500), _badminton_place("b", 900), _badminton_place("c", 1200)],
    ):
        stub = _RadiusStub(list(places))
        _use(monkeypatch, stub)
        _search(tc=alice[0])
        assert stub.radii[0] == 8000
        assert stub.radii == sorted(stub.radii)        # monotonically widening
        assert len(stub.radii) <= len(LADDER)          # never more than three


# --------------------------------------------------------------------------- #
# 5. results outside the ACTIVE radius are rejected even if the provider returns them
# --------------------------------------------------------------------------- #
def test_out_of_radius_results_are_rejected_soft_bias_provider(alice, monkeypatch):
    tc, _ = alice
    # hard_filter=False -> the provider returns the 9 km + 40 km entries at every
    # rung; within_radius must still drop them while 3 are inside 8 km.
    stub = _RadiusStub([
        _badminton_place("in1", 1000), _badminton_place("in2", 4000),
        _badminton_place("in3", 7000),
        _badminton_place("just_out", 9000), _badminton_place("way_out", 40000),
    ], hard_filter=False)
    _use(monkeypatch, stub)
    b = _search(tc)
    assert b["radius_m"] == 8000
    assert stub.radii == [8000]
    names = {c["name"] for c in b["candidates"]}
    assert "just_out" not in names and "way_out" not in names
    assert all(c["distance_m"] <= 8000 for c in b["candidates"])


def test_each_rung_response_is_authoritative_not_accumulated(alice, monkeypatch):
    # A soft-bias provider that returns EVERYTHING each call. If the engine
    # (wrongly) accumulated rungs, the 8 km result set would already include the
    # far places. It must not: the 8 km rung sees only its own in-radius subset.
    stub = _RadiusStub([
        _badminton_place("near", 2000),
        _badminton_place("far1", 15000), _badminton_place("far2", 17000),
    ], hard_filter=False)
    _use(monkeypatch, stub)
    b = _search(tc=alice[0])
    # 8km: 1 in radius -> widen; 12km: 1 -> widen; 20km: 3 -> stop
    assert stub.radii == [8000, 12000, 20000]
    assert b["radius_m"] == 20000
    assert {c["name"] for c in b["candidates"]} == {"near", "far1", "far2"}


def test_hard_filtering_provider_still_drives_the_ladder(alice, monkeypatch):
    tc, _ = alice
    stub = _RadiusStub([
        _badminton_place("n", 5000), _badminton_place("m1", 13000), _badminton_place("m2", 19000),
    ], hard_filter=True)
    _use(monkeypatch, stub)
    b = _search(tc)
    assert stub.radii == [8000, 12000, 20000]
    assert b["radius_m"] == 20000 and len(b["candidates"]) == 3


# --------------------------------------------------------------------------- #
# 6. exact-boundary behaviour  (within_radius unit + endpoint)
# --------------------------------------------------------------------------- #
def test_within_radius_boundary_and_tolerance():
    p = lambda d: {"place_id": f"x{d}", "distance_m": d}
    got = pc.within_radius(
        [p(0), p(7999.9), p(8000.0), p(8000.0 + pc.RADIUS_BOUNDARY_TOLERANCE_M),
         p(8000.0 + pc.RADIUS_BOUNDARY_TOLERANCE_M + 1), p(12000)],
        8000,
    )
    kept = {q["distance_m"] for q in got}
    assert 8000.0 in kept
    assert (8000.0 + pc.RADIUS_BOUNDARY_TOLERANCE_M) in kept
    assert (8000.0 + pc.RADIUS_BOUNDARY_TOLERANCE_M + 1) not in kept
    assert 12000 not in kept


@pytest.mark.parametrize("bad", [None, "far", float("nan"), float("inf"), -10, True])
def test_within_radius_drops_unprovable_distances(bad):
    got = pc.within_radius([{"place_id": "a", "distance_m": bad}, {"place_id": "b", "distance_m": 100}], 8000)
    assert [q["place_id"] for q in got] == ["b"]


def test_endpoint_keeps_a_place_exactly_on_the_ring(alice, monkeypatch):
    tc, _ = alice
    stub = _RadiusStub([
        _badminton_place("on_ring", 8000.0), _badminton_place("a", 1000), _badminton_place("b", 2000),
    ])
    _use(monkeypatch, stub)
    b = _search(tc)
    assert b["radius_m"] == 8000
    assert stub.radii == [8000]
    assert "on_ring" in {c["name"] for c in b["candidates"]}


# --------------------------------------------------------------------------- #
# 7. distance sorting  (equal evidence -> nearest first, stable)
# --------------------------------------------------------------------------- #
def test_equal_evidence_results_are_sorted_nearest_first(alice, monkeypatch):
    tc, _ = alice
    stub = _RadiusStub([
        _badminton_place("far", 7000), _badminton_place("near", 1500), _badminton_place("mid", 4000),
    ])
    _use(monkeypatch, stub)
    b = _search(tc)
    dists = [c["distance_m"] for c in b["candidates"]]
    assert dists == sorted(dists)


# --------------------------------------------------------------------------- #
# 8. deduplication by stable provider id
# --------------------------------------------------------------------------- #
def test_duplicate_place_ids_are_deduplicated(alice, monkeypatch):
    tc, _ = alice
    stub = _RadiusStub([
        _badminton_place("Court", 2000, pid="osm:court"),
        _badminton_place("Court (dup)", 2000, pid="osm:court"),
        _badminton_place("Other", 3000, pid="osm:other"),
        _badminton_place("Third", 4000, pid="osm:third"),
    ])
    _use(monkeypatch, stub)
    b = _search(tc)
    ids = [c["place_id"] for c in b["candidates"]]
    assert len(ids) == len(set(ids))
    assert ids.count("osm:court") == 1


# --------------------------------------------------------------------------- #
# 9-10. current-location vs manual origin both feed the search
# --------------------------------------------------------------------------- #
def test_device_origin_feeds_the_search(alice, monkeypatch):
    tc, _ = alice
    stub = _RadiusStub([_badminton_place("A", 2000), _badminton_place("B", 3000), _badminton_place("C", 4000)])
    _use(monkeypatch, stub)
    b = tc.post("/api/activities/search", json={
        "activity_id": "badminton",
        "origin": {"latitude": 3.16, "longitude": 101.71, "source": "device"},
    }).json()
    assert b["origin_source"] == "device" and b["status"] == "OK"
    assert stub.radii == [8000]


def test_manual_origin_feeds_the_search(alice, monkeypatch):
    tc, _ = alice
    stub = _RadiusStub([_badminton_place("A", 2000), _badminton_place("B", 3000), _badminton_place("C", 4000)])
    _use(monkeypatch, stub)
    b = tc.post("/api/activities/search", json={
        "activity_id": "badminton",
        "origin": {"latitude": 3.16, "longitude": 101.71, "source": "manual"},
    }).json()
    assert b["origin_source"] == "manual" and b["status"] == "OK"


# --------------------------------------------------------------------------- #
# 12-13. provider error -> truthful PROVIDER_ERROR, ONE call, no fabrication
# --------------------------------------------------------------------------- #
def test_provider_error_on_the_first_rung_is_truthful_and_bounded(alice, monkeypatch):
    tc, _ = alice
    stub = _RadiusStub([_badminton_place("A", 2000)], error=True)
    _use(monkeypatch, stub)
    b = _search(tc)
    assert b["status"] == "PROVIDER_ERROR" and b["error"] == pc.PROVIDER_TIMEOUT
    assert b["candidates"] == []
    assert stub.radii == [8000]                        # failed on the first attempt, stopped


@pytest.mark.parametrize("kind", list(pc.PROVIDER_ERROR_KINDS))
def test_all_provider_error_kinds_are_normalised(alice, monkeypatch, kind):
    tc, _ = alice

    class _K(_RadiusStub):
        async def search_nearby(self, **kw):
            self.radii.append(kw["radius_m"])
            raise pc.ProviderError(kind)

    _use(monkeypatch, _K([]))
    b = _search(tc)
    assert b["status"] == "PROVIDER_ERROR" and b["error"] == kind and b["candidates"] == []


# --------------------------------------------------------------------------- #
# 15-16. no hardcoded fallback centre, no fabricated results
# --------------------------------------------------------------------------- #
def test_no_hardcoded_cyberjaya_and_no_fake_results(alice, monkeypatch):
    tc, _ = alice
    stub = _RadiusStub([])                            # provider returns nothing at every rung
    _use(monkeypatch, stub)
    b = _search(tc)
    assert b["status"] == "NO_RESULTS" and b["candidates"] == []
    assert stub.radii == [8000, 12000, 20000]         # every rung searched from the real origin


# --------------------------------------------------------------------------- #
# 17. every currently-supported category still runs through the staged ladder
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("activity_id", list(ac.SUPPORTED_ACTIVITY_IDS))
def test_every_supported_category_still_operates(alice, monkeypatch, activity_id):
    tc, _ = alice
    stub = _RadiusStub([])
    _use(monkeypatch, stub)
    b = tc.post("/api/activities/search", json={
        "activity_id": activity_id,
        "origin": {"latitude": 3.16, "longitude": 101.71, "source": "device"},
    })
    assert b.status_code == 200
    j = b.json()
    assert j["activity_id"] == activity_id
    assert j["status"] in ("OK", "NO_RESULTS")
    assert j["radius_m"] in LADDER
    assert stub.radii and stub.radii[0] == 8000 and len(stub.radii) <= len(LADDER)


def test_the_mandatory_named_categories_are_present():
    # football/futsal, cycling, running, golf, tennis, badminton, AND volleyball
    # (volleyball became a real activity in B9.1).
    for aid in ("futsal", "football", "cycling", "running", "golf", "tennis",
                "badminton", "volleyball"):
        assert aid in ac.SUPPORTED_ACTIVITY_IDS


# --------------------------------------------------------------------------- #
# B9.1 — Volleyball through the staged ladder: valid / empty / out-of-radius
# --------------------------------------------------------------------------- #
def _volley(name, dist_m, ptypes=("sport=volleyball",), *, pid=None, match="exact"):
    return _place("volleyball", name, dist_m, list(ptypes), pid=pid, match=match)


def test_volleyball_valid_results_settle_at_8km(alice, monkeypatch):
    tc, _ = alice
    stub = _RadiusStub([
        _volley("Court A", 1200, pid="osm:va"),
        _volley("Beach Court B", 4200, ("sport=beachvolleyball",), pid="osm:vb"),
        _volley("Sports Hall C", 6800, ("leisure=sports_hall", "sport=volleyball"), pid="osm:vc"),
    ])
    _use(monkeypatch, stub)
    b = _search(tc, "volleyball")
    assert b["status"] == "OK" and b["radius_m"] == 8000
    assert stub.radii == [8000]
    assert len(b["candidates"]) == 3


def test_volleyball_empty_is_truthful(alice, monkeypatch):
    tc, _ = alice
    _use(monkeypatch, _RadiusStub([]))
    b = _search(tc, "volleyball")
    assert b["status"] == "NO_RESULTS" and b["candidates"] == []


def test_volleyball_unrelated_places_do_not_count(alice, monkeypatch):
    tc, _ = alice
    stub = _RadiusStub([
        _volley("Basketball Only", 500, ("sport=basketball",), pid="osm:bb", match="broad"),
        _volley("Restaurant", 700, ("amenity=restaurant",), pid="osm:rr", match="broad"),
    ])
    _use(monkeypatch, stub)
    b = _search(tc, "volleyball")
    assert b["status"] == "NO_RESULTS" and b["candidates"] == []
    assert stub.radii == [8000, 12000, 20000]         # kept widening, found nothing real


def test_volleyball_out_of_radius_rejected(alice, monkeypatch):
    tc, _ = alice
    stub = _RadiusStub([
        _volley("In", 2000, pid="osm:in"),
        _volley("Out", 9000, pid="osm:out"),
    ], hard_filter=False)
    _use(monkeypatch, stub)
    b = _search(tc, "volleyball")
    # only 1 inside 8 km -> widen to 12 (In stays, Out now in radius) -> 2 -> widen
    # to 20 -> still 2 -> stop; "Out" appears only once the radius reaches it.
    assert stub.radii == [8000, 12000, 20000]
    got = {c["name"]: c["distance_m"] for c in b["candidates"]}
    assert set(got) == {"In", "Out"}
    assert all(d <= 20000 for d in got.values())
