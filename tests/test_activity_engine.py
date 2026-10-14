"""Phase 5E — Activity Engine.

The deterministic engine (activity_core) is the authority for requirements,
evidence, relevance and ordering — no LLM. These tests lock in:
  * the Activity Requirements ontology,
  * the evidence hierarchy + conflict detection,
  * the relevance/confidence scoring and truthful classification,
  * the /api/activities/search contract (auth, validation, privacy, races),
  * the "no fake activity data" source guards on the frontend.
"""
import json
import os
import pathlib
import re

import pytest

import activity_core as ac
import main
import places_core as pc

ROOT = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _place(name, ptypes, *, provider="osm", match=None, dist=1000.0,
           rating=None, count=None, pid=None):
    return {
        "place_id": pid or f"{provider}:{re.sub(r'[^a-z0-9]+', '_', name.lower())}",
        "provider": provider, "provider_place_id": "x", "name": name,
        "latitude": 3.16, "longitude": 101.71, "address": None,
        "distance_m": dist, "distance_kind": "straight_line",
        "activity": "futsal", "activity_match": match, "provider_types": ptypes,
        "rating": rating, "rating_count": count, "opening_hours": None,
        "open_now": None, "business_status": None, "website": None, "phone": None,
        "maps_url": "https://maps.google/?cid=1",
        "evidence": {"identity": {"source": provider, "verified": True},
                     "operational": {"source": None, "status": None},
                     "hours": {"source": None, "has_hours": False, "open_now": None},
                     "quality": {"source": None, "rating": None, "rating_count": None},
                     "contact": {"source": None, "website": None, "phone": None}},
    }


def _classes(ranked):
    return {r["name"]: r["activity_relevance"]["class"] for r in ranked}


# ===================== ontology / profiles =====================
def test_every_profile_is_well_formed():
    assert set(ac.SUPPORTED_ACTIVITY_IDS) == set(ac.ACTIVITY_PROFILES)
    assert len(ac.SUPPORTED_ACTIVITY_IDS) == len(set(ac.SUPPORTED_ACTIVITY_IDS))  # no dup ids
    for aid, p in ac.ACTIVITY_PROFILES.items():
        assert p.activity_id == aid
        assert p.label and isinstance(p.label, str)
        assert isinstance(p.requires_facility, bool)
        assert isinstance(p.allows_public_geography, bool)
        assert isinstance(p.google_text_queries, list) and p.google_text_queries
        assert 0.0 < p.min_relevance < 1.0
        # a public-geography activity must actually declare some public geo signals
        if p.allows_public_geography:
            assert p.public_geo_osm or p.public_geo_google_types
        # a facility activity must have at least one strong activity-specific signal
        assert p.strong_osm or p.strong_google_types or p.strong_name_kw


def test_registry_has_the_mandatory_activities_and_is_not_count_locked():
    # every FYP-required activity exists (Volleyball included, added in B9.1) ...
    assert ac.MANDATORY_ACTIVITY_IDS <= set(ac.SUPPORTED_ACTIVITY_IDS)
    assert "volleyball" in ac.SUPPORTED_ACTIVITY_IDS
    # ... but the registry is deliberately OPEN: this test (and the engine) must
    # not need editing when a future ActivityProfile is appended, so it asserts a
    # lower bound, never an exact count.
    assert len(ac.SUPPORTED_ACTIVITY_IDS) >= len(ac.MANDATORY_ACTIVITY_IDS)
    assert set(ac.MANDATORY_ACTIVITY_IDS) == {
        aid for aid in ac.MANDATORY_ACTIVITY_IDS if aid in ac.ACTIVITY_PROFILES
    }


def test_volleyball_profile_uses_only_the_official_osm_taxonomy():
    # B9.1a taxonomy hotfix: the official OSM value is `sport=beachvolleyball`
    # (one word, no underscore). The earlier `sport=beach_volleyball` spelling
    # must not appear anywhere as authoritative evidence.
    p = ac.ACTIVITY_PROFILES["volleyball"]
    assert "sport=volleyball" in p.strong_osm
    assert "sport=beachvolleyball" in p.strong_osm
    assert "sport=beach_volleyball" not in p.strong_osm
    assert all("beach_volleyball" not in t for t in p.strong_osm)
    assert p.requires_facility is True and p.allows_public_geography is False
    # volleyball is no longer treated as a "foreign" conflicting sport
    assert "volleyball" not in ac._FOREIGN_SPECIFIC_SPORTS
    assert "beach_volleyball" not in ac._FOREIGN_SPECIFIC_SPORTS
    # sport ownership / conflict ontology uses the official value
    assert ac._SPORT_OWNER.get("volleyball") == "volleyball"
    assert ac._SPORT_OWNER.get("beachvolleyball") == "volleyball"
    assert "beach_volleyball" not in ac._SPORT_OWNER


def test_volleyball_has_no_unsupported_google_place_type_as_strong_evidence():
    # B9.1a: `volleyball` is NOT an official Google Places type -> removed from
    # strong_google_types. A generic Google sports venue must NOT become a
    # Confirmed volleyball result without volleyball-specific evidence.
    p = ac.ACTIVITY_PROFILES["volleyball"]
    assert p.strong_google_types == frozenset()
    for generic in (["athletic_field"], ["sports_activity_location"], ["sports_complex"], ["sports_club"]):
        ranked = ac.rank("volleyball", [
            _place("Generic Google Sports Venue", generic, provider="google",
                   match="broad", dist=200, rating=4.9, count=800),
        ])
        for r in ranked:
            assert r["activity_relevance"]["class"] not in ("strong", "likely")


def test_volleyball_accepts_sport_volleyball_and_official_beachvolleyball():
    ranked = ac.rank("volleyball", [
        _place("Titiwangsa Volleyball Courts", ["leisure=pitch", "sport=volleyball"], match="exact", dist=2200),
        _place("Bkt Jalil Beach Volleyball", ["sport=beachvolleyball"], match="exact", dist=6100),
    ])
    cls = _classes(ranked)
    assert cls["Titiwangsa Volleyball Courts"] == "strong"
    assert cls["Bkt Jalil Beach Volleyball"] == "strong"    # official value -> owned by volleyball


def test_volleyball_drops_unrelated_and_conflicting_sports():
    ranked = ac.rank("volleyball", [
        _place("Generic Basketball Court", ["sport=basketball"], match="broad", dist=250),
        _place("Anytime Fitness", ["gym", "fitness_center"], provider="google", dist=180),
        _place("Kopitiam", ["amenity=cafe"], dist=90),
        # the OLD misspelling is just an unrecognised sport value -> no evidence
        _place("Sand Court (mislabelled tag)", ["sport=beach_volleyball"], match="broad", dist=300),
    ])
    assert ranked == []


def test_volleyball_has_no_evidence_means_no_result():
    assert ac.rank("volleyball", [_place("Kopitiam", ["amenity=cafe"], dist=90)]) == []


def test_validate_activity_id():
    assert ac.validate_activity_id("  Badminton ") == "badminton"
    for bad in ("hiking", "gym", "", None, 123, "  "):
        with pytest.raises(ValueError):
            ac.validate_activity_id(bad)


# ===================== relevance: the acceptance scenarios =====================
def test_badminton_specific_court_outranks_generic_and_drops_conflicts():
    ranked = ac.rank("badminton", [
        _place("Sunway Badminton Hall", ["leisure=sports_centre", "sport=badminton"], match="exact", dist=2100),
        _place("Basketball / Netball Court", ["leisure=pitch", "sport=basketball;netball"], match="broad", dist=400),
        _place("MMU Multi-Sport Complex", ["leisure=sports_centre", "sport=multi"], match="broad", dist=800),
        _place("Anytime Fitness", ["gym", "fitness_center"], provider="google", dist=250),
    ])
    cls = _classes(ranked)
    assert cls["Sunway Badminton Hall"] == "strong"
    assert ranked[0]["name"] == "Sunway Badminton Hall"          # beats the 400 m generic
    assert "Basketball / Netball Court" not in cls               # conflict -> dropped
    assert "Anytime Fitness" not in cls                          # bare gym -> not a result
    assert cls.get("MMU Multi-Sport Complex") in ("likely", "possible")


def test_generic_basketball_court_is_not_a_strong_badminton_match():
    ranked = ac.rank("badminton", [
        _place("Court 7 (Basketball)", ["sport=basketball"], match="broad", dist=300),
    ])
    assert ranked == []   # no badminton evidence, conflicting sport -> not shown


def test_real_pool_outranks_generic_sports_complex_for_swimming():
    ranked = ac.rank("swimming", [
        _place("Cyberjaya Aquatic Centre", ["leisure=swimming_pool"], match="exact", dist=2600),
        _place("Shaftsbury Sports Complex", ["sports_complex"], provider="google", match="broad", dist=500),
        _place("D'Pulze Pool", ["swimming_pool"], provider="google", match="broad", dist=1900, rating=4.3, count=140),
    ])
    cls = _classes(ranked)
    assert cls["Cyberjaya Aquatic Centre"] == "strong"
    assert cls["D'Pulze Pool"] == "strong"
    assert "Shaftsbury Sports Complex" not in cls               # no pool evidence -> excluded


def test_real_tennis_court_outranks_generic_recreation_centre():
    ranked = ac.rank("tennis", [
        _place("Putrajaya Tennis Centre", ["sport=tennis"], match="exact", dist=3000),
        _place("Community Recreation Centre", ["leisure=sports_centre"], match="broad", dist=350),
    ])
    assert ranked[0]["name"] == "Putrajaya Tennis Centre"
    assert _classes(ranked)["Putrajaya Tennis Centre"] == "strong"


def test_running_park_ranks_without_a_formal_facility():
    ranked = ac.rank("running", [
        _place("Taman Tasik Cyberjaya", ["leisure=park"], match="broad", dist=1600),
        _place("MSN Athletics Track", ["leisure=track", "sport=athletics"], match="exact", dist=6000),
    ])
    cls = _classes(ranked)
    assert cls["MSN Athletics Track"] == "strong"
    assert cls["Taman Tasik Cyberjaya"] in ("likely", "possible")   # surfaces, honestly hedged
    assert ranked[0]["activity_relevance"]["is_public_geography"] is False  # the track


def test_running_does_not_only_match_venues_named_running():
    ranked = ac.rank("running", [
        _place("Central Park", ["leisure=park"], match="broad", dist=900),
        _place("Lakeside Recreation Ground", ["leisure=recreation_ground"], match="broad", dist=1200),
    ])
    assert {r["name"] for r in ranked} == {"Central Park", "Lakeside Recreation Ground"}
    for r in ranked:
        assert r["activity_relevance"]["is_public_geography"] is True


def test_golf_course_and_driving_range_rank_for_golf():
    ranked = ac.rank("golf", [
        _place("Cyberview Golf Course", ["leisure=golf_course"], match="exact", dist=5200, rating=4.4, count=300),
        _place("KL Sports Complex", ["sports_complex"], provider="google", match="broad", dist=400),
        _place("TopGolf Driving Range", ["golf=driving_range"], match="exact", dist=8000),
    ])
    cls = _classes(ranked)
    assert cls["Cyberview Golf Course"] == "strong"
    assert cls["TopGolf Driving Range"] == "strong"
    assert "KL Sports Complex" not in cls


def test_futsal_prioritises_real_facilities_downgrades_unrelated():
    ranked = ac.rank("futsal", [
        _place("Champion Futsal", ["leisure=pitch", "sport=futsal"], match="exact", dist=3500, rating=4.5, count=210),
        _place("Padang Bola (soccer)", ["leisure=pitch", "sport=soccer"], match="broad", dist=1200),
        _place("Putrajaya Tennis Club", ["sport=tennis"], match="broad", dist=800),
    ])
    cls = _classes(ranked)
    assert ranked[0]["name"] == "Champion Futsal" and cls["Champion Futsal"] == "strong"
    assert cls.get("Padang Bola (soccer)") in ("likely", "possible")  # soccer pitch = broad futsal
    assert "Putrajaya Tennis Club" not in cls                          # conflict


def test_distance_influences_but_cannot_override_a_tier_gap():
    # a real badminton centre 5 km away vs a multi-sport hall 200 m away
    ranked = ac.rank("badminton", [
        _place("Far Real Badminton Centre", ["sport=badminton"], match="exact", dist=5000),
        _place("Near Multi Hall", ["leisure=sports_centre", "sport=multi"], match="broad", dist=200),
    ])
    assert ranked[0]["name"] == "Far Real Badminton Centre"
    s0 = ranked[0]["activity_relevance"]["score"]
    s1 = ranked[1]["activity_relevance"]["score"]
    assert s0 > s1


def test_distance_orders_candidates_within_the_same_tier():
    ranked = ac.rank("badminton", [
        _place("Badminton A", ["sport=badminton"], match="exact", dist=4000, pid="osm:a"),
        _place("Badminton B", ["sport=badminton"], match="exact", dist=1000, pid="osm:b"),
    ])
    assert [r["name"] for r in ranked] == ["Badminton B", "Badminton A"]


def test_ranking_is_deterministic_with_a_stable_tie_break():
    items = [
        _place("Z", ["sport=badminton"], match="exact", dist=1000, pid="osm:z"),
        _place("A", ["sport=badminton"], match="exact", dist=1000, pid="osm:a"),
        _place("M", ["sport=badminton"], match="exact", dist=1000, pid="osm:m"),
    ]
    r1 = [r["place_id"] for r in ac.rank("badminton", list(items))]
    r2 = [r["place_id"] for r in ac.rank("badminton", list(reversed(items)))]
    assert r1 == r2 == ["osm:a", "osm:m", "osm:z"]


def test_unreliable_distance_disables_the_distance_factor():
    args = [
        _place("Near weak", ["leisure=sports_centre", "sport=multi"], match="broad", dist=200),
        _place("Far strong", ["sport=badminton"], match="exact", dist=9000),
    ]
    ranked = ac.rank("badminton", list(args), distance_confident=False)
    assert ranked[0]["name"] == "Far strong"     # still tier-ordered, distance ignored


# ===================== Phase 5E closure: revised ranking policy =====================
def test_equivalent_evidence_orders_by_distance():
    # several real pools, no distinguishing evidence -> pure ascending distance
    ps = [_place(f"Pool {d}", ["swimming_pool"], provider="google", match="broad",
                 dist=d, pid=f"g:{d}") for d in (11000, 1600, 4700, 2000, 3100)]
    ranked = ac.rank("swimming", ps)
    assert [round(r["distance_m"]) for r in ranked] == [1600, 2000, 3100, 4700, 11000]
    # and the 11 km pool must NOT be lifted above nearer ones by a rating/popularity bonus
    ps2 = list(ps) + [_place("Pool 11k rated", ["swimming_pool"], provider="google",
                             match="broad", dist=11000, rating=4.9, count=900, pid="g:rated")]
    r2 = ac.rank("swimming", ps2)
    assert r2[0]["distance_m"] == 1600
    assert r2[-1]["name"] in ("Pool 11000", "Pool 11k rated")


def test_large_distance_gap_lets_a_near_likely_beat_a_far_confirmed():
    ranked = ac.rank("futsal", [
        _place("Far Confirmed Futsal Court", ["sport=futsal"], match="exact", dist=15500, pid="g:far"),
        _place("Near Soccer Pitch", ["leisure=pitch", "sport=soccer"], match="broad", dist=830, pid="g:near"),
    ])
    assert [r["name"] for r in ranked] == ["Near Soccer Pitch", "Far Confirmed Futsal Court"]
    assert ranked[0]["activity_relevance"]["class"] == "likely"
    assert ranked[1]["activity_relevance"]["class"] == "strong"   # class is evidence, not rank


def test_small_distance_gap_cannot_let_weak_beat_strong():
    ranked = ac.rank("futsal", [
        _place("Nearby generic arena", ["sports_complex"], provider="google", match="broad", dist=100, pid="g:w"),
        _place("Confirmed futsal 3km", ["sport=futsal"], match="exact", dist=3000, pid="g:s"),
    ])
    # the confirmed court is first; the generic arena is weak and, with a
    # stronger result present, is not even returned
    assert ranked[0]["name"] == "Confirmed futsal 3km"          # strong is first
    arena = [r for r in ranked if r["name"] == "Nearby generic arena"]
    assert not arena or arena[0]["activity_relevance"]["class"] == "possible"


def test_rating_and_reviews_cannot_confirm_an_unsupported_venue():
    ranked = ac.rank("badminton", [
        _place("Massively Popular Sports Complex", ["sports_complex"], provider="google",
               match="broad", dist=300, rating=4.9, count=5000),
    ])
    # no badminton evidence -> at best a weak "possible", never likely/strong;
    # rating/popularity changes nothing
    assert all(r["activity_relevance"]["class"] not in ("strong", "likely") for r in ranked)


def test_running_gets_stronger_practical_distance_weighting():
    # a far confirmed track vs a near suitable public space: for running the
    # nearby space should be competitive or preferable
    ranked = ac.rank("running", [
        _place("Far Athletics Track", ["leisure=track", "sport=athletics"], match="exact", dist=17800, pid="g:far"),
        _place("Nearby Lake Park", ["leisure=park"], provider="google", match="broad", dist=740, pid="g:near"),
    ])
    assert ranked[0]["name"] == "Nearby Lake Park"
    # golf, by contrast, tolerates distance far better (higher travel willingness)
    g = ac.rank("golf", [
        _place("Confirmed Golf Course 12km", ["leisure=golf_course"], match="exact", dist=12000, pid="g:c"),
        _place("Country Club 1km", ["country_club"], provider="google", match="broad", dist=1000, pid="g:n"),
    ])
    assert g[0]["name"] == "Confirmed Golf Course 12km"


@pytest.mark.parametrize("activity,generic", [
    ("badminton", ["sports_complex"]),
    ("swimming", ["sports_complex"]),
    ("tennis", ["sports_complex"]),
    ("golf", ["sports_complex"]),
])
def test_strict_facility_activities_never_confirm_a_generic_venue(activity, generic):
    ranked = ac.rank(activity, [
        _place("Generic Venue", generic, provider="google", match="broad", dist=200, rating=4.8, count=400),
    ])
    for r in ranked:
        assert r["activity_relevance"]["class"] == "possible"   # never strong/likely
        assert r["activity_relevance"]["tier"] in ("weak",)


def test_weak_results_are_dropped_when_enough_strong_or_likely_exist():
    strong3 = [_place(f"Real Futsal {i}", ["sport=futsal"], match="exact", dist=4000 + i, pid=f"g:s{i}") for i in range(3)]
    weak5 = [_place(f"Generic Arena {i}", ["sports_complex"], provider="google", match="broad", dist=500 + i, pid=f"g:w{i}") for i in range(5)]
    ranked = ac.rank("futsal", strong3 + weak5)
    assert {r["name"] for r in ranked} == {"Real Futsal 0", "Real Futsal 1", "Real Futsal 2"}
    # scarce case: only 1 strong -> a SMALL number of "possible" may return
    ranked2 = ac.rank("futsal", [strong3[0]] + weak5)
    poss = [r for r in ranked2 if r["activity_relevance"]["class"] == "possible"]
    assert 1 <= len(poss) <= ac.POSSIBLE_KEEP_MAX


def test_engine_does_not_pad_to_twenty():
    many_pools = [_place(f"Pool {i}", ["swimming_pool"], provider="google", match="broad",
                         dist=1000 + i * 300, pid=f"g:{i}") for i in range(40)]
    ranked = ac.rank("swimming", many_pools)
    assert len(ranked) <= ac.RECOMMENDATION_LIMIT
    # and when there is genuinely little, it returns little (not filler)
    thin = ac.rank("swimming", [_place("Community Library", ["library"], dist=100)])
    assert thin == []


def test_far_thin_evidence_is_dropped_but_far_confirmed_survives():
    ranked = ac.rank("badminton", [
        _place("Confirmed Badminton 18km", ["sport=badminton"], match="exact", dist=18000, pid="g:c"),
        _place("Vaguely Named Badminton place", ["sports_complex"], provider="google", match="broad", dist=17000, pid="g:t"),
    ])
    names = {r["name"] for r in ranked}
    assert "Confirmed Badminton 18km" in names         # 1.00 - ~0.60 = 0.40 >= MIN_USEFUL_SCORE
    assert "Vaguely Named Badminton place" not in names


# ===================== evidence / no fabrication =====================
def test_engine_never_adds_place_facts():
    p = _place("Real Pool", ["leisure=swimming_pool"], match="exact")
    ranked = ac.rank("swimming", [p])
    out = ranked[0]
    for k in ("rating", "rating_count", "opening_hours", "open_now", "business_status", "website", "phone"):
        assert out[k] is None                       # nulls stay null
    assert out["evidence"] == p["evidence"]          # evidence block untouched
    assert set(out) - set(p) == {"activity_relevance"}  # only the assessment is added


def test_no_evidence_means_no_result():
    assert ac.rank("badminton", [_place("Nasi Lemak Stall", ["amenity=restaurant"], dist=100)]) == []
    assert ac.rank("swimming", [_place("Public Library", ["amenity=library"], dist=100)]) == []


def test_rank_does_not_mutate_inputs():
    p = _place("Pool", ["leisure=swimming_pool"], match="exact")
    before = json.loads(json.dumps(p))
    ac.rank("swimming", [p])
    assert p == before


# ===================== /api/activities/search endpoint =====================
class _StubProvider:
    name = "osm"
    attribution = "© OpenStreetMap contributors"

    def __init__(self, result=None, error=None):
        self._result, self._error = result or [], error

    async def search_nearby(self, **_kw):
        if self._error:
            raise pc.ProviderError(self._error)
        return list(self._result)


def _use_provider(monkeypatch, stub):
    monkeypatch.setattr(main.places_provider, "get_provider", lambda *a, **k: stub)


_BODY = {"activity_id": "badminton", "origin": {"latitude": 2.92, "longitude": 101.65, "source": "device"}}


def test_activities_search_requires_auth(client):
    assert client.post("/api/activities/search", json=_BODY).status_code == 401


def test_activities_search_ok_ranks_real_candidates(alice, monkeypatch):
    tc, _ = alice
    _use_provider(monkeypatch, _StubProvider(result=[
        _place("Real Badminton Centre", ["sport=badminton"], match="exact", dist=2000),
        _place("Basketball Court", ["sport=basketball"], match="broad", dist=300),
    ]))
    b = tc.post("/api/activities/search", json=_BODY).json()
    assert b["status"] == "OK"
    assert b["activity_id"] == "badminton" and b["provider"] == "osm"
    assert b["origin_source"] == "device" and b["distance_confident"] is True
    names = [c["name"] for c in b["candidates"]]
    assert names == ["Real Badminton Centre"]                # conflict dropped
    assert b["candidates"][0]["activity_relevance"]["class"] == "strong"
    assert "reason" in b["candidates"][0]["activity_relevance"]


def test_activities_search_truthful_empty(alice, monkeypatch):
    tc, _ = alice
    _use_provider(monkeypatch, _StubProvider(result=[
        _place("Some Basketball Court", ["sport=basketball"], match="broad", dist=200),
    ]))
    b = tc.post("/api/activities/search", json=_BODY).json()
    assert b["status"] == "NO_RESULTS" and b["candidates"] == [] and b["error"] is None


@pytest.mark.parametrize("kind", list(pc.PROVIDER_ERROR_KINDS))
def test_activities_search_provider_error_is_normalised(alice, monkeypatch, kind):
    tc, _ = alice
    _use_provider(monkeypatch, _StubProvider(error=kind))
    b = tc.post("/api/activities/search", json=_BODY).json()
    assert b["status"] == "PROVIDER_ERROR" and b["error"] == kind and b["candidates"] == []


@pytest.mark.parametrize("body", [
    {"origin": {"latitude": 2.92, "longitude": 101.65, "source": "device"}},          # missing activity
    {"activity_id": "chess", "origin": {"latitude": 2.92, "longitude": 101.65}},       # unsupported
    {"activity_id": "badminton", "origin": {"latitude": 200, "longitude": 101.65}},    # bad lat
    {"activity_id": "badminton", "origin": {"latitude": 2.92, "longitude": 999}},      # bad lng
    {"activity_id": "badminton"},                                                      # missing origin
    {"activity_id": "badminton", "origin": {"latitude": 2.92}},                        # missing lng
])
def test_activities_search_validation_422(alice, monkeypatch, body):
    tc, _ = alice
    _use_provider(monkeypatch, _StubProvider(result=[]))
    assert tc.post("/api/activities/search", json=body).status_code == 422


def test_activities_search_coarse_device_fix_flags_distance_not_confident(alice, monkeypatch):
    tc, _ = alice
    _use_provider(monkeypatch, _StubProvider(result=[
        _place("Real Badminton Centre", ["sport=badminton"], match="exact", dist=2000),
    ]))
    body = {"activity_id": "badminton",
            "origin": {"latitude": 2.92, "longitude": 101.65, "source": "device", "accuracy_m": 45000}}
    b = tc.post("/api/activities/search", json=body).json()
    assert b["distance_confident"] is False
    # a manual origin is always treated as a definite point
    body2 = {"activity_id": "badminton",
             "origin": {"latitude": 2.92, "longitude": 101.65, "source": "manual", "accuracy_m": 45000}}
    assert tc.post("/api/activities/search", json=body2).json()["distance_confident"] is True


def test_activities_search_never_leaks_key_or_logs_coordinates(alice, monkeypatch, capfd):
    tc, _ = alice
    _use_provider(monkeypatch, _StubProvider(error=pc.PROVIDER_TIMEOUT))
    lat, lng = 12.3456789, -98.7654321
    r = tc.post("/api/activities/search", json={
        "activity_id": "badminton",
        "origin": {"latitude": lat, "longitude": lng, "source": "device"},
    })
    out = capfd.readouterr()
    combined = out.out + out.err
    assert "12.3456789" not in combined and "98.7654321" not in combined
    assert "PROVIDER_TIMEOUT" in combined                       # KIND only
    assert "AIza" not in r.text and "api_key" not in r.text.lower() and "x-goog" not in r.text.lower()


def test_activities_search_is_stateless(alice, monkeypatch):
    import db as db_layer
    tc, _ = alice
    _use_provider(monkeypatch, _StubProvider(result=[_place("Pool", ["leisure=swimming_pool"], match="exact")]))
    c = db_layer.get_connection()
    tables_before = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    head = c.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    c.close()
    tc.post("/api/activities/search", json={"activity_id": "swimming",
                                            "origin": {"latitude": 2.92, "longitude": 101.65, "source": "manual"}})
    c = db_layer.get_connection()
    assert {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")} == tables_before
    assert c.execute("SELECT version_num FROM alembic_version").fetchone()[0] == head
    c.close()


def test_activities_search_does_not_touch_ai(alice, monkeypatch):
    tc, _ = alice
    called = {"n": 0}
    monkeypatch.setattr(main.AIService, "interpret",
                        staticmethod(lambda *_a, **_k: called.__setitem__("n", called["n"] + 1)))
    _use_provider(monkeypatch, _StubProvider(result=[_place("Pool", ["leisure=swimming_pool"], match="exact")]))
    tc.post("/api/activities/search", json={"activity_id": "swimming",
                                            "origin": {"latitude": 2.92, "longitude": 101.65, "source": "manual"}})
    assert called["n"] == 0


# ===================== frontend source guards =====================
_ACT = (ROOT / "app" / "activity" / "page.tsx").read_text(encoding="utf-8")
_TYPES = (ROOT / "app" / "lib" / "types.ts").read_text(encoding="utf-8")


def test_activity_page_has_no_fake_static_data():
    for banned in ("Cyberjaya, Selangor", "26°C", "26\\u00b0C", "Clear Sky", "Perfect conditions",
                   "Tamarind Square", "Community Club", "1.2 km away", "2.5 km away",
                   "Current Conditions", "CloudSun", "It will rain"):
        assert banned not in _ACT, banned
    # no invented coordinates
    assert "2.9213" not in _ACT and "101.6559" not in _ACT and "AIza" not in _ACT
    # Phase 5F: the ONLY weather reference is the read-only <WeatherLine> at the
    # search origin — no inline fabricated weather (that component is guarded by
    # tests/test_weather_foundation.py).
    lower = _ACT.lower()
    weather_refs = lower.count("weather")
    assert "<weatherline" in lower and weather_refs <= 6      # import(x2) + comment + tag, no more
    # No stray hardcoded temperature reading — anchored to a degree mark (literal
    # or escaped-unicode) so it can't collide with an unrelated legitimate number
    # like the text-[26px] page-title CSS class (Phase 08's tab-root title size,
    # shared with Today/Plan/Wallet/Profile). A real fake temperature is still
    # caught regardless of which digits it uses.
    assert "°" not in _ACT and "\\u00b0" not in _ACT
    import re
    assert not re.search(r">\s*\d{1,2}°", _ACT)               # no hardcoded °C in JSX


def test_activity_page_uses_the_real_engine_endpoint_body_only():
    assert '"/api/activities/search"' in _ACT
    assert 'method: "POST"' in _ACT
    assert "activity_id" in _ACT and "origin" in _ACT
    assert "&lat=" not in _ACT and "lat=${" not in _ACT and "lng=${" not in _ACT


def test_activity_page_shows_truthful_relevance_and_reason():
    # the engine's class + reason are surfaced, not hidden
    assert "activity_relevance" in _ACT
    assert ".reason" in _ACT
    for cls in ("strong", "likely", "possible"):
        assert cls in _ACT
    # possible/weak matches carry an explicit "not confirmed" caveat
    assert "not confirmed" in _ACT.lower() or "isn't confirmed" in _ACT.lower() or "doesn" in _ACT.lower()


def test_activity_page_reuses_shared_location_and_directions():
    assert 'from "../lib/useDeviceOrigin"' in _ACT and "useDeviceOrigin()" in _ACT
    assert "getCurrentLocation" not in _ACT and "navigator.geolocation" not in _ACT
    assert "directionsUrl(" in _ACT               # real place identity handoff, not text-name
    assert "distanceLabel(" in _ACT               # 5D unreliable-distance suppression reused


def test_activity_types_exposed():
    for token in ("ActivityCandidate", "ActivitySearchResponse", "ActivityRelevance",
                  "ActivityRelevanceClass", "distance_confident"):
        assert token in _TYPES


def test_no_activity_ai_recommendation_fabrication_remains():
    # the old static "recommendations" card path invented a title over rec.message
    assert "Perfect conditions" not in _ACT
    assert "active_recommendations" not in _ACT   # Activity no longer renders that stream


def test_missing_metadata_rows_are_omitted_not_rendered_as_not_available():
    # the card builds a `rows` array and only pushes a row when the value exists
    assert 'if (c.rating != null) rows.push(' in _ACT
    assert "if (hoursText) rows.push(" in _ACT
    assert "if (c.phone) rows.push(" in _ACT
    # the old always-on "not available" placeholder rows are gone
    assert '"not available"' not in _ACT and "not available" not in _ACT


def test_raw_evidence_debug_fields_are_not_shown_in_the_consumer_ui():
    # the plain factual reason stays; the tier/signals debug row is gone
    assert "rel.reason" in _ACT
    assert "rel.signals" not in _ACT
    assert "rel.tier" not in _ACT
    assert "Evidence:" not in _ACT and "Evidence</dt>" not in _ACT
    # ...but the engine still RETURNS the internal evidence for later use
    b = ac.rank("swimming", [_place("Pool", ["leisure=swimming_pool"], match="exact")])[0]["activity_relevance"]
    assert set(b) >= {"tier", "signals", "score", "evidence_score", "conflicts"}


def test_ranking_policy_constants_are_sane():
    assert ac.RECOMMENDATION_LIMIT <= 15
    assert ac.MIN_GOOD_TO_DROP_POSSIBLE >= 2
    assert 0.0 < ac.MIN_USEFUL_SCORE < 0.40      # a far CONFIRMED (~0.40) still clears it
    # a far confirmed venue's floor score is above the useful threshold
    from activity_core import _TIER_BASE, _DIST_MAX_PENALTY
    assert _TIER_BASE["strong"] - _DIST_MAX_PENALTY > ac.MIN_USEFUL_SCORE
    assert _TIER_BASE["strong"] - _DIST_MAX_PENALTY > _TIER_BASE["weak"]   # weak can't beat far-strong
