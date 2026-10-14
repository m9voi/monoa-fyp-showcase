"""Phase 5G — Map <-> Schedule integration.

Covers the durable bridge between a REAL place identity and a PERSISTENT schedule
event:
  * place_ref  — provider-neutral validation + the consumer `place` shape
  * POST /api/events  — an optional linked place, persisted identity, volatile
    facts NOT frozen, idempotency for retried creates
  * POST /api/places/resolve  — Schedule -> Map "View place": live Google details
    with a TRUTHFUL snapshot fallback, never invented metadata
  * GooglePlacesProvider.place_details  — mocked upstream HTTP

Temp DB only. No live provider is ever reached (adapter tests inject a mocked
httpx transport; endpoint tests monkeypatch the provider class).
"""
import asyncio
import json

import httpx
import pytest

import main
import place_ref
import places_core as pc
import places_provider as pp
from conftest import new_authed_client

MYT = __import__("datetime").timezone(__import__("datetime").timedelta(hours=8))


def _run(coro):
    return asyncio.run(coro)


def _now_iso(h=0):
    import datetime
    return (datetime.datetime.now(MYT).replace(microsecond=0)
            + datetime.timedelta(hours=h)).isoformat()


# A real, public place identity (KLCC park) — never the user's own location.
GOOGLE_PLACE = {
    "provider": "google",
    "provider_place_id": "ChIJ_klcc_park_01",
    "name": "KLCC Park",
    "address": "Kuala Lumpur City Centre, 50088 Kuala Lumpur",
    "latitude": 3.1553,
    "longitude": 101.7137,
}
OSM_PLACE = {
    "provider": "osm",
    "provider_place_id": "way:987654",
    "name": "Taman Tasik Perdana",
    "address": "Jalan Tembusu, Tasik Perdana",
    "latitude": 3.1420,
    "longitude": 101.6869,
}


# ============================ place_ref (pure) ============================
def test_place_ref_normalizes_google():
    out = place_ref.normalize_place_ref(GOOGLE_PLACE)
    assert out["place_provider"] == "google"
    assert out["place_provider_id"] == "ChIJ_klcc_park_01"
    assert out["location"] == "KLCC Park"          # the human NAME snapshot
    assert out["place_address"].startswith("Kuala Lumpur City Centre")
    assert out["place_lat"] == 3.1553 and out["place_lng"] == 101.7137


def test_place_ref_normalizes_osm_and_strips_prefix():
    out = place_ref.normalize_place_ref({**OSM_PLACE, "provider_place_id": "osm:way:987654"})
    assert out["place_provider"] == "osm"
    assert out["place_provider_id"] == "way:987654"


@pytest.mark.parametrize("mutation, needle", [
    ({"provider": "yelp"}, "provider"),
    ({"provider_place_id": ""}, "provider_place_id"),
    ({"provider_place_id": "!!"}, "Google place id"),
    ({"name": "  "}, "name"),
    ({"latitude": 999}, "latitude"),
    ({"longitude": "abc"}, "longitude"),
    ({"latitude": float("nan")}, "latitude"),
])
def test_place_ref_rejects_bad_google(mutation, needle):
    with pytest.raises(ValueError) as ei:
        place_ref.normalize_place_ref({**GOOGLE_PLACE, **mutation})
    assert needle.lower() in str(ei.value).lower()


def test_place_ref_rejects_bad_osm_id():
    with pytest.raises(ValueError):
        place_ref.normalize_place_ref({**OSM_PLACE, "provider_place_id": "12345"})


def test_place_ref_error_never_leaks_coordinates():
    try:
        place_ref.normalize_place_ref({**GOOGLE_PLACE, "latitude": 500})
    except ValueError as e:
        msg = str(e)
    assert "3.1553" not in msg and "101.7137" not in msg


def test_place_out_requires_full_identity():
    assert place_ref.place_out({"place_provider": "google", "place_provider_id": "x"}) is None
    full = {
        "place_provider": "google", "place_provider_id": "ChIJ_x",
        "place_lat": 3.15, "place_lng": 101.71,
        "location": "KLCC Park", "place_address": "Addr",
    }
    out = place_ref.place_out(full)
    assert out == {
        "provider": "google", "provider_place_id": "ChIJ_x", "place_id": "google:ChIJ_x",
        "name": "KLCC Park", "address": "Addr", "latitude": 3.15, "longitude": 101.71,
    }
    # a snapshot NEVER carries volatile facts
    assert "rating" not in out and "open_now" not in out and "opening_hours" not in out


# ==================== POST /api/events — linked place ====================
def test_create_event_with_linked_place_persists_identity():
    tc, _ = new_authed_client("evplace1@example.com")
    body = {"title": "Morning walk", "start": _now_iso(24), "end": _now_iso(25),
            "place": {**GOOGLE_PLACE,
                      # volatile facts the client should NOT be able to freeze:
                      "rating": 4.7, "open_now": True, "business_status": "OPERATIONAL"}}
    r = tc.post("/api/events", json=body)
    assert r.status_code == 201, r.text
    ev = r.json()["event"]
    assert ev["place"] == {
        "provider": "google", "provider_place_id": "ChIJ_klcc_park_01",
        "place_id": "google:ChIJ_klcc_park_01", "name": "KLCC Park",
        "address": "Kuala Lumpur City Centre, 50088 Kuala Lumpur",
        "latitude": 3.1553, "longitude": 101.7137,
    }
    assert ev["location"] == "KLCC Park"           # legacy text column = the name
    # volatile facts were dropped, not persisted
    for k in ("rating", "open_now", "business_status", "opening_hours"):
        assert k not in ev["place"]

    # survives a fresh read (refresh / re-login): both the flat event list and the
    # recurrence-expanded occurrence carry the same identity.
    sched = tc.get("/api/schedule").json()
    flat = next(e for e in sched["all_events"] if e["id"] == ev["id"])
    assert flat["place"]["place_id"] == "google:ChIJ_klcc_park_01"
    occ = next(o for o in sched["events"] if o["id"] == ev["id"])
    assert occ["place"]["provider_place_id"] == "ChIJ_klcc_park_01"
    assert occ["place"]["latitude"] == 3.1553


def test_create_event_without_place_is_still_valid():
    tc, _ = new_authed_client("evplace2@example.com")
    r = tc.post("/api/events", json={"title": "Solo focus block",
                                     "start": _now_iso(2), "end": _now_iso(3)})
    assert r.status_code == 201
    assert r.json()["event"]["place"] is None


@pytest.mark.parametrize("bad", [
    {"provider": "bing"},
    {"provider_place_id": "!!!"},
    {"latitude": 240},
    {"longitude": None},
    {"name": ""},
])
def test_create_event_rejects_bad_place(bad):
    tc, _ = new_authed_client(f"evbad{abs(hash(str(bad)))}@example.com")
    r = tc.post("/api/events", json={"title": "x", "start": _now_iso(1), "end": _now_iso(2),
                                     "place": {**GOOGLE_PLACE, **bad}})
    assert r.status_code == 422
    # nothing persisted
    assert tc.get("/api/schedule").json()["all_events"] == []


def test_create_event_place_end_before_start_still_422():
    tc, _ = new_authed_client("evplace3@example.com")
    r = tc.post("/api/events", json={"title": "x", "start": _now_iso(5), "end": _now_iso(4),
                                     "place": GOOGLE_PLACE})
    assert r.status_code == 422


def test_linked_place_events_still_conflict():
    tc, _ = new_authed_client("evplace4@example.com")
    s, e = _now_iso(10), _now_iso(11)
    tc.post("/api/events", json={"title": "A", "start": s, "end": e, "place": GOOGLE_PLACE})
    tc.post("/api/events", json={"title": "B", "start": s, "end": e, "place": OSM_PLACE})
    conflicts = tc.get("/api/schedule").json()["conflicts"]
    assert len(conflicts) == 1


def test_patch_place_null_clears_link():
    tc, _ = new_authed_client("evplace5@example.com")
    ev = tc.post("/api/events", json={"title": "x", "start": _now_iso(1), "end": _now_iso(2),
                                      "place": GOOGLE_PLACE}).json()["event"]
    assert ev["place"] is not None
    r = tc.patch(f"/api/events/{ev['id']}", json={"place": None})
    assert r.status_code == 200
    assert r.json()["event"]["place"] is None


# ==================== POST /api/events — idempotency ====================
def test_idempotent_retry_returns_first_event():
    tc, _ = new_authed_client("idem1@example.com")
    body = {"title": "Booked once", "start": _now_iso(1), "end": _now_iso(2),
            "client_token": "tok-abc-123", "place": GOOGLE_PLACE}
    r1 = tc.post("/api/events", json=body)
    r2 = tc.post("/api/events", json=body)
    assert r1.status_code == 201 and r2.status_code == 200
    assert r2.json().get("idempotent") is True
    assert r1.json()["event"]["id"] == r2.json()["event"]["id"]
    assert len(tc.get("/api/schedule").json()["all_events"]) == 1


def test_different_tokens_create_distinct_events():
    tc, _ = new_authed_client("idem2@example.com")
    base = {"title": "T", "start": _now_iso(1), "end": _now_iso(2)}
    tc.post("/api/events", json={**base, "client_token": "a"})
    tc.post("/api/events", json={**base, "client_token": "b"})
    assert len(tc.get("/api/schedule").json()["all_events"]) == 2


def test_no_token_is_not_deduplicated():
    tc, _ = new_authed_client("idem3@example.com")
    base = {"title": "T", "start": _now_iso(1), "end": _now_iso(2)}
    tc.post("/api/events", json=base)
    tc.post("/api/events", json=base)
    assert len(tc.get("/api/schedule").json()["all_events"]) == 2


def test_idempotency_index_exists():
    import sqlite3
    import db as db_layer
    conn = db_layer.get_connection()
    try:
        idx = {r[1] for r in conn.execute("PRAGMA index_list(schedule_events)")}
    finally:
        conn.close()
    assert "idx_schedule_events_user_client_token" in idx


def test_idempotency_is_per_user():
    tc_a, _ = new_authed_client("idemA@example.com")
    tc_b, _ = new_authed_client("idemB@example.com")
    body = {"title": "T", "start": _now_iso(1), "end": _now_iso(2), "client_token": "shared"}
    ra = tc_a.post("/api/events", json=body)
    rb = tc_b.post("/api/events", json=body)
    assert ra.status_code == 201 and rb.status_code == 201       # not each other's
    assert ra.json()["event"]["id"] != rb.json()["event"]["id"]


# ==================== POST /api/places/resolve ====================
class _StubGP:
    """Stand-in for GooglePlacesProvider used by /api/places/resolve."""
    configured = True
    mode = "live"          # "live" | "error" | "none"

    def __init__(self, *a, **k):
        pass

    def is_configured(self):
        return type(self).configured

    async def place_details(self, *, provider_place_id, origin_lat, origin_lng, activity="unknown"):
        if type(self).mode == "error":
            raise pc.ProviderError(pc.PROVIDER_UNAVAILABLE, "503")
        if type(self).mode == "none":
            return None
        return pc.build_place(
            provider="google", provider_place_id=provider_place_id,
            name="KLCC Park", latitude=3.1553, longitude=101.7137,
            origin_lat=origin_lat, origin_lng=origin_lng,
            activity=activity, activity_match="broad", provider_types=["park"],
            address="Fresh formatted address",
            rating=4.6, rating_count=5123,
            opening_hours=["Monday: Open 24 hours"], open_now=True,
            business_status="OPERATIONAL", website="https://klccpark.example",
            maps_url="https://maps.google/?cid=99",
        )


@pytest.fixture()
def stub_gp(monkeypatch):
    _StubGP.configured = True
    _StubGP.mode = "live"
    monkeypatch.setattr(main.places_provider, "GooglePlacesProvider", _StubGP)
    return _StubGP


def test_places_resolve_requires_auth(client):
    assert client.post("/api/places/resolve", json=GOOGLE_PLACE).status_code == 401


def test_places_resolve_live_google(stub_gp):
    tc, _ = new_authed_client("resolve1@example.com")
    r = tc.post("/api/places/resolve", json=GOOGLE_PLACE)
    assert r.status_code == 200
    b = r.json()
    assert b["status"] == "LIVE" and b["reason"] is None
    assert b["provider"] == "google"
    assert b["place"]["rating"] == 4.6              # volatile facts are FRESH
    assert b["place"]["open_now"] is True
    assert b["place"]["address"] == "Fresh formatted address"
    assert b["place"]["place_id"] == "google:ChIJ_klcc_park_01"


def test_places_resolve_provider_unavailable_falls_back_to_snapshot(stub_gp):
    _StubGP.mode = "error"
    tc, _ = new_authed_client("resolve2@example.com")
    r = tc.post("/api/places/resolve", json=GOOGLE_PLACE)
    assert r.status_code == 200
    b = r.json()
    assert b["status"] == "SNAPSHOT"
    assert b["reason"] == pc.PROVIDER_UNAVAILABLE
    # the stored snapshot is truthful; NO invented volatile metadata
    assert b["place"]["name"] == "KLCC Park"
    assert b["place"]["address"] == GOOGLE_PLACE["address"]
    assert b["place"]["rating"] is None and b["place"]["open_now"] is None
    assert b["place"]["opening_hours"] is None and b["place"]["business_status"] is None


def test_places_resolve_no_match_falls_back_to_snapshot(stub_gp):
    _StubGP.mode = "none"
    tc, _ = new_authed_client("resolve3@example.com")
    b = tc.post("/api/places/resolve", json=GOOGLE_PLACE).json()
    assert b["status"] == "SNAPSHOT" and b["reason"] == "PROVIDER_NO_MATCH"
    assert b["place"]["rating"] is None


def test_places_resolve_not_configured_falls_back_to_snapshot(stub_gp):
    _StubGP.configured = False
    tc, _ = new_authed_client("resolve4@example.com")
    b = tc.post("/api/places/resolve", json=GOOGLE_PLACE).json()
    assert b["status"] == "SNAPSHOT" and b["reason"] == "PROVIDER_NOT_CONFIGURED"
    assert b["place"]["maps_url"].endswith("query_place_id=ChIJ_klcc_park_01")


def test_places_resolve_osm_is_snapshot_only():
    tc, _ = new_authed_client("resolve5@example.com")
    b = tc.post("/api/places/resolve", json=OSM_PLACE).json()
    assert b["status"] == "SNAPSHOT" and b["reason"] == "NO_LIVE_PROVIDER"
    assert b["provider"] == "osm"
    assert "OpenStreetMap" in b["attribution"]
    assert b["place"]["name"] == "Taman Tasik Perdana"
    assert b["place"]["rating"] is None
    # osm snapshot deep-link is a coordinate query (no google place id)
    assert "query_place_id" not in b["place"]["maps_url"]


def test_places_resolve_rejects_bad_body():
    tc, _ = new_authed_client("resolve6@example.com")
    assert tc.post("/api/places/resolve", json={"provider": "google"}).status_code == 422
    assert tc.post("/api/places/resolve", json=[]).status_code == 422


def test_places_resolve_error_logs_kind_only_no_coordinates(stub_gp, capsys):
    _StubGP.mode = "error"
    tc, _ = new_authed_client("resolve7@example.com")
    tc.post("/api/places/resolve", json=GOOGLE_PLACE)
    out = capsys.readouterr().out
    assert "PLACE_RESOLVE" in out and "error=" in out
    assert "3.1553" not in out and "101.7137" not in out


# ============== GooglePlacesProvider.place_details (mocked HTTP) ==============
_DETAILS_OK = {
    "id": "ChIJ_klcc_park_01",
    "displayName": {"text": "KLCC Park"},
    "location": {"latitude": 3.1553, "longitude": 101.7137},
    "formattedAddress": "KLCC, 50088 Kuala Lumpur",
    "rating": 4.6, "userRatingCount": 5123,
    "currentOpeningHours": {"openNow": True},
    "regularOpeningHours": {"weekdayDescriptions": ["Monday: Open 24 hours"]},
    "businessStatus": "OPERATIONAL",
    "websiteUri": "https://klccpark.example",
    "googleMapsUri": "https://maps.google/?cid=99",
    "types": ["park", "tourist_attraction"],
}


def _factory(handler):
    def factory():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)
    return factory


def test_place_details_parses_live_fields_and_hides_key():
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["key"] = req.headers.get("X-Goog-Api-Key")
        seen["mask"] = req.headers.get("X-Goog-FieldMask")
        return httpx.Response(200, json=_DETAILS_OK)

    prov = pp.GooglePlacesProvider(api_key="SECRET_KEY", client_factory=_factory(handler))
    out = _run(prov.place_details(provider_place_id="ChIJ_klcc_park_01",
                                  origin_lat=3.1553, origin_lng=101.7137))
    assert out["place_id"] == "google:ChIJ_klcc_park_01"
    assert out["rating"] == 4.6 and out["rating_count"] == 5123
    assert out["open_now"] is True
    assert out["opening_hours"] == ["Monday: Open 24 hours"]
    assert out["business_status"] == "OPERATIONAL"
    assert "ChIJ_klcc_park_01" in seen["url"]
    assert "SECRET_KEY" not in seen["url"]          # key travels in the header only
    assert seen["key"] == "SECRET_KEY"
    assert "displayName" in seen["mask"] and "places." not in seen["mask"]


def test_place_details_returns_none_when_google_forgets_id():
    prov = pp.GooglePlacesProvider(api_key="K",
                                   client_factory=_factory(lambda r: httpx.Response(404, json={})))
    assert _run(prov.place_details(provider_place_id="ChIJ_gone",
                                   origin_lat=3.1, origin_lng=101.7)) is None


@pytest.mark.parametrize("status, kind", [
    (429, pc.PROVIDER_RATE_LIMITED),
    (403, pc.PROVIDER_NOT_CONFIGURED),
    (500, pc.PROVIDER_UNAVAILABLE),
])
def test_place_details_error_mapping(status, kind):
    prov = pp.GooglePlacesProvider(api_key="K",
                                   client_factory=_factory(lambda r: httpx.Response(status, json={})))
    with pytest.raises(pc.ProviderError) as ei:
        _run(prov.place_details(provider_place_id="ChIJ_x", origin_lat=3.1, origin_lng=101.7))
    assert ei.value.kind == kind


def test_place_details_timeout_maps_to_provider_timeout():
    def handler(req):
        raise httpx.ReadTimeout("slow")
    prov = pp.GooglePlacesProvider(api_key="K", client_factory=_factory(handler))
    with pytest.raises(pc.ProviderError) as ei:
        _run(prov.place_details(provider_place_id="ChIJ_x", origin_lat=3.1, origin_lng=101.7))
    assert ei.value.kind == pc.PROVIDER_TIMEOUT


def test_place_details_without_key_is_not_configured():
    prov = pp.GooglePlacesProvider(api_key="")
    with pytest.raises(pc.ProviderError) as ei:
        _run(prov.place_details(provider_place_id="ChIJ_x", origin_lat=3.1, origin_lng=101.7))
    assert ei.value.kind == pc.PROVIDER_NOT_CONFIGURED


# ========== closure review: linked-place edit consistency ==========
# INVARIANT: an event must never DISPLAY one destination (free-text location)
# while its STRUCTURED place identity (provider id / coordinates) points at
# another. A manual free-text location edit through the generic event form (no
# structured `place` in the PATCH) that moves the text away from the linked
# place name clears the structured link ATOMICALLY in the same update.

def _raw_place_row(event_id):
    import db as db_layer
    conn = db_layer.get_connection()
    try:
        r = conn.execute(
            "SELECT location, place_provider, place_provider_id, place_lat, place_lng, place_address "
            "FROM schedule_events WHERE id = ?",
            (event_id,),
        ).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _mk_linked(tc, place=GOOGLE_PLACE, title="Linked A"):
    r = tc.post("/api/events", json={"title": title, "start": _now_iso(24), "end": _now_iso(25), "place": place})
    assert r.status_code == 201, r.text
    return r.json()["event"]


def test_edit_location_away_from_linked_place_clears_structured_link_atomically():
    tc, _ = new_authed_client("editcons1@example.com")
    ev = _mk_linked(tc)
    assert ev["place"]["provider_place_id"] == "ChIJ_klcc_park_01"

    r = tc.patch(f"/api/events/{ev['id']}", json={"location": "Somewhere Else Entirely"})
    assert r.status_code == 200, r.text
    out = r.json()["event"]
    assert out["location"] == "Somewhere Else Entirely"
    assert out["place"] is None                       # response carries no structured place

    row = _raw_place_row(ev["id"])                    # and neither does the persisted row
    assert row["location"] == "Somewhere Else Entirely"
    assert row["place_provider"] is None and row["place_provider_id"] is None
    assert row["place_lat"] is None and row["place_lng"] is None
    assert row["place_address"] is None               # no stale Place A address left behind


def test_occurrence_and_flat_output_cannot_expose_stale_place_after_edit():
    tc, _ = new_authed_client("editcons2@example.com")
    ev = _mk_linked(tc)
    tc.patch(f"/api/events/{ev['id']}", json={"location": "New Text Place"})
    sched = tc.get("/api/schedule").json()
    occ = next(o for o in sched["events"] if o["id"] == ev["id"])
    flat = next(e for e in sched["all_events"] if e["id"] == ev["id"])
    assert occ["place"] is None and flat["place"] is None
    assert occ["location"] == "New Text Place"


def test_title_or_time_only_edit_preserves_linked_place():
    tc, _ = new_authed_client("editcons3@example.com")
    ev = _mk_linked(tc)
    r1 = tc.patch(f"/api/events/{ev['id']}", json={"title": "Renamed"})
    assert r1.json()["event"]["place"]["provider_place_id"] == "ChIJ_klcc_park_01"
    r2 = tc.patch(f"/api/events/{ev['id']}", json={"start": _now_iso(30), "end": _now_iso(31)})
    assert r2.json()["event"]["place"]["provider_place_id"] == "ChIJ_klcc_park_01"
    row = _raw_place_row(ev["id"])
    assert row["place_provider"] == "google" and row["place_lat"] is not None


def test_resaving_unchanged_location_preserves_linked_place():
    # the generic form re-submits EVERY field on save, including the untouched
    # location (which equals the linked place name) — that must not unlink.
    tc, _ = new_authed_client("editcons4@example.com")
    ev = _mk_linked(tc)
    assert ev["location"] == "KLCC Park"
    r = tc.patch(f"/api/events/{ev['id']}", json={
        "title": "KLCC session", "location": "KLCC Park",
        "start": _now_iso(24), "end": _now_iso(25),
    })
    assert r.status_code == 200
    assert r.json()["event"]["place"]["provider_place_id"] == "ChIJ_klcc_park_01"


def test_explicit_place_null_still_unlinks_and_clears_row():
    tc, _ = new_authed_client("editcons5@example.com")
    ev = _mk_linked(tc)
    r = tc.patch(f"/api/events/{ev['id']}", json={"place": None})
    assert r.status_code == 200 and r.json()["event"]["place"] is None
    row = _raw_place_row(ev["id"])
    assert row["place_provider"] is None and row["place_lat"] is None


def test_patch_with_new_structured_place_replaces_never_clears():
    tc, _ = new_authed_client("editcons6@example.com")
    ev = _mk_linked(tc, place=GOOGLE_PLACE)
    r = tc.patch(f"/api/events/{ev['id']}", json={"place": OSM_PLACE})
    assert r.status_code == 200
    out = r.json()["event"]
    assert out["place"]["provider"] == "osm"
    assert out["place"]["provider_place_id"] == "way:987654"
    assert out["location"] == "Taman Tasik Perdana"
    row = _raw_place_row(ev["id"])
    assert row["place_provider"] == "osm" and row["place_provider_id"] == "way:987654"


def test_patch_with_new_place_and_location_text_keeps_new_place_identity():
    # both supplied in one PATCH — the structured place wins, no accidental clear
    tc, _ = new_authed_client("editcons7@example.com")
    ev = _mk_linked(tc, place=GOOGLE_PLACE)
    r = tc.patch(f"/api/events/{ev['id']}", json={"location": "ignored text", "place": OSM_PLACE})
    assert r.status_code == 200
    assert r.json()["event"]["place"]["provider_place_id"] == "way:987654"


def test_location_edit_on_unlinked_event_is_a_plain_update():
    tc, _ = new_authed_client("editcons8@example.com")
    ev = tc.post("/api/events", json={"title": "x", "start": _now_iso(1), "end": _now_iso(2),
                                      "location": "Old"}).json()["event"]
    r = tc.patch(f"/api/events/{ev['id']}", json={"location": "New"})
    assert r.status_code == 200
    assert r.json()["event"]["location"] == "New" and r.json()["event"]["place"] is None


def test_create_with_place_and_idempotency_unaffected_by_edit_rule():
    tc, _ = new_authed_client("editcons9@example.com")
    body = {"title": "Once", "start": _now_iso(1), "end": _now_iso(2),
            "client_token": "edit-rule-tok", "place": GOOGLE_PLACE}
    r1 = tc.post("/api/events", json=body)
    r2 = tc.post("/api/events", json=body)
    assert r1.status_code == 201 and r2.status_code == 200 and r2.json()["idempotent"] is True
    assert r1.json()["event"]["id"] == r2.json()["event"]["id"]
    assert len(tc.get("/api/schedule").json()["all_events"]) == 1
