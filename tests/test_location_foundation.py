"""Phase 5B — real geolocation foundation.

Backend: POST /api/location/validate (stateless, auth-gated, no persistence,
no coordinate logging). Plus source-level guards that the Map page no longer
carries a hardcoded fallback position and that a location failure is a distinct
state from "no places found".
"""
import os
import pathlib

import pytest

import db as db_layer

ROOT = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MAP = ROOT / "app" / "map" / "page.tsx"
_LOCATION = ROOT / "app" / "lib" / "location.ts"


# --------------------------------------------------------------------------- #
# POST /api/location/validate
# --------------------------------------------------------------------------- #
def test_location_validate_requires_auth(client):
    assert client.post("/api/location/validate",
                       json={"latitude": 3.1, "longitude": 101.7}).status_code == 401


def test_location_validate_accepts_a_normal_fix(alice):
    tc, _ = alice
    r = tc.post("/api/location/validate", json={
        "latitude": 3.139, "longitude": 101.6869, "accuracy_m": 24.5,
        "timestamp_ms": 1_700_000_000_000,
    })
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "status": "VALID", "latitude": 3.139, "longitude": 101.6869,
        "accuracy_m": 24.5, "timestamp_ms": 1_700_000_000_000,
    }


def test_location_validate_accepts_extremes_and_null_accuracy(alice):
    tc, _ = alice
    for lat, lng in [(-90, -180), (90, 180), (0, 0)]:
        r = tc.post("/api/location/validate", json={"latitude": lat, "longitude": lng})
        assert r.status_code == 200, (lat, lng)
        assert r.json()["accuracy_m"] is None
        assert r.json()["timestamp_ms"] is None


@pytest.mark.parametrize("payload", [
    {"latitude": 90.001, "longitude": 10},
    {"latitude": -90.5, "longitude": 10},
    {"latitude": 10, "longitude": 180.5},
    {"latitude": 10, "longitude": -181},
    {"latitude": "abc", "longitude": 10},
    {"latitude": 10},                               # missing longitude
    {"longitude": 10},                              # missing latitude
    {},
])
def test_location_validate_rejects_bad_coordinates(alice, payload):
    tc, _ = alice
    assert tc.post("/api/location/validate", json=payload).status_code == 422


_JSON = {"Content-Type": "application/json"}


@pytest.mark.parametrize("raw", [
    b'{"latitude": 1e999, "longitude": 10}',        # overflow -> inf
    b'{"latitude": Infinity, "longitude": 10}',     # json literal
    b'{"latitude": NaN, "longitude": 10}',
    b'{"latitude": 10, "longitude": -1e999}',
])
def test_location_validate_rejects_non_finite_raw(alice, raw):
    """A hostile client can put Infinity/NaN straight in the bytes — reject them."""
    tc, _ = alice
    assert tc.post("/api/location/validate", content=raw, headers=_JSON).status_code == 422


def test_location_validate_normalises_unusable_accuracy(alice):
    tc, _ = alice
    r = tc.post("/api/location/validate",
                json={"latitude": 3.1, "longitude": 101.7, "accuracy_m": -5})
    assert r.status_code == 200 and r.json()["accuracy_m"] is None
    r = tc.post("/api/location/validate",
                content=b'{"latitude": 3.1, "longitude": 101.7, "accuracy_m": 1e999}',
                headers=_JSON)
    assert r.status_code == 200 and r.json()["accuracy_m"] is None
    ok = tc.post("/api/location/validate",
                 json={"latitude": 3.1, "longitude": 101.7, "accuracy_m": 12.0})
    assert ok.json()["accuracy_m"] == 12.0


def test_location_validate_is_stateless(alice):
    """No row is written anywhere, no new table is created, schema head unchanged."""
    tc, _ = alice
    c = db_layer.get_connection()
    tables_before = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    head_before = c.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    c.close()

    tc.post("/api/location/validate", json={"latitude": 3.1, "longitude": 101.7, "accuracy_m": 9})

    c = db_layer.get_connection()
    assert c.execute("SELECT COUNT(*) FROM map_locations").fetchone()[0] == 0
    tables_after = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    head_after = c.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    c.close()
    assert tables_after == tables_before
    assert head_after == head_before


def test_location_validate_does_not_log_coordinates(alice, capfd):
    tc, _ = alice
    lat, lng = 12.3456789, -98.7654321
    tc.post("/api/location/validate", json={"latitude": lat, "longitude": lng, "accuracy_m": 7})
    out = capfd.readouterr()
    combined = out.out + out.err
    assert "12.3456789" not in combined
    assert "98.7654321" not in combined


def test_location_not_attached_to_session(alice):
    """Posting a location does not change auth state / cookies."""
    tc, _ = alice
    before = tc.cookies.get("coreos_session")
    tc.post("/api/location/validate", json={"latitude": 1.0, "longitude": 2.0})
    assert tc.cookies.get("coreos_session") == before
    assert tc.get("/api/auth/me").status_code == 200


# --------------------------------------------------------------------------- #
# frontend source guards
# --------------------------------------------------------------------------- #
_STATES = [
    "NOT_REQUESTED", "REQUESTING", "AVAILABLE", "PERMISSION_DENIED",
    "POSITION_UNAVAILABLE", "TIMEOUT", "INSECURE_CONTEXT", "UNSUPPORTED",
    "INVALID_COORDINATES", "BACKEND_UNREACHABLE", "UNKNOWN_ERROR",
]


def test_location_service_exists_with_full_state_model():
    assert _LOCATION.is_file()
    src = _LOCATION.read_text(encoding="utf-8")
    for s in _STATES:
        assert s in src, f"missing location state: {s}"
    for fn in ("getCurrentLocation", "validateCoordinates", "normalizeGeolocationError",
               "isLocationFresh", "readPermissionState", "isInsecureContext"):
        assert fn in src, f"missing exported helper: {fn}"
    # getCurrentPosition (point-in-time), NOT watchPosition (continuous tracking)
    assert "getCurrentPosition" in src
    assert "watchPosition" not in src


def test_no_hardcoded_fallback_location_anywhere_in_app():
    for f in list((ROOT / "app").rglob("*.ts")) + list((ROOT / "app").rglob("*.tsx")):
        if "__tests__" in f.parts:
            continue
        text = f.read_text(encoding="utf-8")
        assert "2.9213" not in text, f"Cyberjaya lat in {f}"
        assert "101.6559" not in text, f"Cyberjaya lng in {f}"
        assert "fallbackLat" not in text and "fallbackLng" not in text, f"fallback coord in {f}"


def test_map_page_uses_location_foundation_and_dropped_the_fake_fallback():
    src = _MAP.read_text(encoding="utf-8")
    # Phase 5E: device-location acquisition moved into the shared hook, which is
    # what pulls the Phase 5B foundation in.
    hook = (ROOT / "app" / "lib" / "useDeviceOrigin.ts").read_text(encoding="utf-8")
    assert 'from "../lib/useDeviceOrigin"' in src and "useDeviceOrigin()" in src
    assert 'from "./location"' in hook and "getCurrentLocation" in hook
    # the old silent-fallback strings are gone
    assert "GPS blocked" not in src
    assert "Cyberjaya" not in src
    assert "Checking GPS permissions" not in src
    # coordinates are NOT put in the map-locations URL any more
    assert "&lat=" not in src and "lat=${" not in src and "lng=${" not in src


def test_map_distinguishes_location_failure_from_no_places():
    """A GPS failure must not render as a 'no places' or provider state.

    Device-location failure (`deviceLoc.phase === "failed"`), no results
    (`search.phase === "empty"`) and provider failure (`search.phase === "error"`)
    are three distinct states. The Phase 5D master-closure page nests the
    no-results / provider-error blocks under `showResultsArea` (an explicit
    search origin + a chosen activity), while the device-failure block is gated
    on its own sub-state and offers a manual-location escape hatch.
    """
    src = _MAP.read_text(encoding="utf-8")
    assert 'deviceLoc.phase === "failed"' in src
    assert 'search.phase === "empty"' in src
    assert 'search.phase === "error"' in src
    # the no-results / provider-error RENDER blocks sit inside the results-area gate
    gate = src.index("{showResultsArea && origin && (")
    assert gate < src.index('{search.phase === "empty" && (')
    assert gate < src.index('{search.phase === "error" && (')
    # the device-failure block is gated on its own sub-state, not a search phase,
    # and it renders before the results area
    assert '{deviceLoc.phase === "failed" && (' in src
    assert src.index('{deviceLoc.phase === "failed" && (') < gate
    # that failure block still lets the user search a location manually
    fail = src[src.index('{deviceLoc.phase === "failed" && ('):gate]
    assert "Search a location" in fail and "setGeoOpen(true)" in fail


def test_no_watchposition_in_app():
    hits = []
    for f in list((ROOT / "app").rglob("*.ts")) + list((ROOT / "app").rglob("*.tsx")):
        if "watchPosition" in f.read_text(encoding="utf-8"):
            hits.append(str(f.relative_to(ROOT)))
    assert not hits, f"continuous tracking introduced in: {hits}"
