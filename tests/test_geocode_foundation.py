"""Phase 5D — geocoding foundation ("Change location" -> explicit search origin).

Provider adapters are tested against MOCKED upstream HTTP (httpx.MockTransport):
this proves query building / parsing / normalisation / error mapping and the
privacy contract (the key goes to the provider, never into a response or a log).
It does NOT prove a live geocoding account works.
"""
import asyncio
import os
import pathlib

import httpx
import pytest

import db as db_layer
import main
import places_core as pc
import geocode_provider as geo

ROOT = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# A public landmark — NOT the user's device location.
KLCC = (3.1578, 101.7117)


def _run(coro):
    return asyncio.run(coro)


def _mock_factory(handler):
    def factory():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)
    return factory


# ===================== Google Geocoding adapter (mocked) =====================
_GOOGLE_OK = {
    "status": "OK",
    "results": [
        {"place_id": "ChIJ_cyberjaya", "formatted_address": "Cyberjaya, Selangor, Malaysia",
         "geometry": {"location": {"lat": 2.9213, "lng": 101.6559}},
         "types": ["locality", "political"]},
        {"place_id": "ChIJ_putrajaya", "formatted_address": "Putrajaya, Malaysia",
         "geometry": {"location": {"lat": 2.9264, "lng": 101.6964}},
         "types": ["locality", "political"]},
    ],
}


def test_google_geocode_parses_and_normalises():
    captured = {}

    def handler(req):
        captured["url"] = str(req.url)
        captured["params"] = dict(req.url.params)
        return httpx.Response(200, json=_GOOGLE_OK)

    prov = geo.GoogleGeocodeProvider(api_key="TEST_KEY_do_not_leak", client_factory=_mock_factory(handler))
    out = _run(prov.geocode("Cyberjaya"))

    # key travels as a query param to Google, server-side only
    assert captured["params"].get("key") == "TEST_KEY_do_not_leak"
    assert captured["params"].get("address") == "Cyberjaya"

    assert [r["id"] for r in out] == ["google:ChIJ_cyberjaya", "google:ChIJ_putrajaya"]
    first = out[0]
    assert first["label"] == "Cyberjaya, Selangor, Malaysia"
    assert first["latitude"] == 2.9213 and first["longitude"] == 101.6559
    assert first["provider"] == "google"
    assert first["result_type"] == "locality"
    # the key is NEVER echoed back in a normalised result
    assert "TEST_KEY_do_not_leak" not in repr(out)


def test_google_geocode_caps_result_count():
    payload = {"status": "OK", "results": [
        {"place_id": f"ChIJ_{i}", "formatted_address": f"Place {i}",
         "geometry": {"location": {"lat": 1.0 + i, "lng": 2.0 + i}}, "types": ["locality"]}
        for i in range(20)
    ]}
    prov = geo.GoogleGeocodeProvider(api_key="K", client_factory=_mock_factory(lambda r: httpx.Response(200, json=payload)))
    assert len(_run(prov.geocode("many"))) == geo.GEOCODE_RESULT_LIMIT


def test_google_geocode_zero_results_is_empty_not_error():
    prov = geo.GoogleGeocodeProvider(api_key="K", client_factory=_mock_factory(
        lambda r: httpx.Response(200, json={"status": "ZERO_RESULTS", "results": []})))
    assert _run(prov.geocode("nowhere at all")) == []


def test_google_geocode_missing_key_is_not_configured():
    prov = geo.GoogleGeocodeProvider(api_key="")
    with pytest.raises(pc.ProviderError) as ei:
        _run(prov.geocode("Cyberjaya"))
    assert ei.value.kind == pc.PROVIDER_NOT_CONFIGURED


@pytest.mark.parametrize("status_str,kind", [
    ("REQUEST_DENIED", pc.PROVIDER_NOT_CONFIGURED),
    ("UNAUTHENTICATED", pc.PROVIDER_NOT_CONFIGURED),
    ("OVER_QUERY_LIMIT", pc.PROVIDER_RATE_LIMITED),
    ("OVER_DAILY_LIMIT", pc.PROVIDER_RATE_LIMITED),
    ("RESOURCE_EXHAUSTED", pc.PROVIDER_RATE_LIMITED),
    ("INVALID_REQUEST", pc.INVALID_PROVIDER_RESPONSE),
    ("UNKNOWN_ERROR", pc.PROVIDER_UNAVAILABLE),
])
def test_google_geocode_status_error_mapping(status_str, kind):
    prov = geo.GoogleGeocodeProvider(api_key="K", client_factory=_mock_factory(
        lambda r: httpx.Response(200, json={"status": status_str, "results": []})))
    with pytest.raises(pc.ProviderError) as ei:
        _run(prov.geocode("x y"))
    assert ei.value.kind == kind


@pytest.mark.parametrize("http_status,kind", [
    (429, pc.PROVIDER_RATE_LIMITED),
    (401, pc.PROVIDER_NOT_CONFIGURED),
    (403, pc.PROVIDER_NOT_CONFIGURED),
    (500, pc.PROVIDER_UNAVAILABLE),
    (503, pc.PROVIDER_UNAVAILABLE),
])
def test_google_geocode_http_error_mapping(http_status, kind):
    prov = geo.GoogleGeocodeProvider(api_key="K", client_factory=_mock_factory(
        lambda r: httpx.Response(http_status, text="nope")))
    with pytest.raises(pc.ProviderError) as ei:
        _run(prov.geocode("x y"))
    assert ei.value.kind == kind


def test_google_geocode_timeout_mapping():
    def handler(_req):
        raise httpx.ReadTimeout("slow")
    prov = geo.GoogleGeocodeProvider(api_key="K", client_factory=_mock_factory(handler))
    with pytest.raises(pc.ProviderError) as ei:
        _run(prov.geocode("x y"))
    assert ei.value.kind == pc.PROVIDER_TIMEOUT


def test_google_geocode_malformed_json_mapping():
    prov = geo.GoogleGeocodeProvider(api_key="K", client_factory=_mock_factory(
        lambda r: httpx.Response(200, text="<<not json>>")))
    with pytest.raises(pc.ProviderError) as ei:
        _run(prov.geocode("x y"))
    assert ei.value.kind == pc.INVALID_PROVIDER_RESPONSE


# ===================== Nominatim adapter (mocked) =====================
_NOMINATIM_OK = [
    {"osm_type": "relation", "osm_id": 111, "display_name": "Cyberjaya, Sepang, Selangor, Malaysia",
     "lat": "2.9213", "lon": "101.6559", "type": "administrative"},
    {"osm_type": "way", "osm_id": 222, "display_name": "Persiaran APEC, Cyberjaya",
     "lat": "2.9250", "lon": "101.6600", "type": "residential"},
    {"osm_type": "node", "osm_id": 333, "display_name": "Cyberjaya Transport Terminal",
     "lat": "2.9300", "lon": "101.6700", "type": "bus_station"},
]


def test_nominatim_parses_composite_ids_and_labels():
    captured = {}

    def handler(req):
        captured["params"] = dict(req.url.params)
        return httpx.Response(200, json=_NOMINATIM_OK)

    prov = geo.NominatimProvider(client_factory=_mock_factory(handler))
    out = _run(prov.geocode("Cyberjaya"))

    assert captured["params"].get("q") == "Cyberjaya"
    assert captured["params"].get("format") == "jsonv2"

    assert [r["id"] for r in out] == ["osm:r111", "osm:w222", "osm:n333"]
    assert out[0]["label"] == "Cyberjaya, Sepang, Selangor, Malaysia"
    assert out[0]["latitude"] == 2.9213 and out[0]["longitude"] == 101.6559
    assert out[0]["provider"] == "osm"
    assert out[0]["result_type"] == "administrative"


def test_nominatim_default_client_sends_descriptive_user_agent():
    # Per the Nominatim usage policy — a real, identifying User-Agent (not the
    # bare httpx default). The default client_factory is what production uses.
    assert "CoreOS" in geo._USER_AGENT
    client = geo._client(geo.GEOCODE_TIMEOUT_S)
    try:
        assert "CoreOS" in client.headers.get("User-Agent", "")
    finally:
        _run(client.aclose())


def test_nominatim_empty_is_no_results_not_error():
    prov = geo.NominatimProvider(client_factory=_mock_factory(lambda r: httpx.Response(200, json=[])))
    assert _run(prov.geocode("nowhere")) == []


@pytest.mark.parametrize("status,kind", [
    (429, pc.PROVIDER_RATE_LIMITED),
    (503, pc.PROVIDER_UNAVAILABLE),
    (504, pc.PROVIDER_UNAVAILABLE),
    (500, pc.PROVIDER_UNAVAILABLE),
])
def test_nominatim_http_error_mapping(status, kind):
    prov = geo.NominatimProvider(client_factory=_mock_factory(lambda r: httpx.Response(status, text="x")))
    with pytest.raises(pc.ProviderError) as ei:
        _run(prov.geocode("x y"))
    assert ei.value.kind == kind


def test_nominatim_malformed_json_mapping():
    prov = geo.NominatimProvider(client_factory=_mock_factory(lambda r: httpx.Response(200, text="{oops")))
    with pytest.raises(pc.ProviderError) as ei:
        _run(prov.geocode("x y"))
    assert ei.value.kind == pc.INVALID_PROVIDER_RESPONSE


# ===================== provider selection =====================
def test_resolve_geo_provider_name(monkeypatch):
    monkeypatch.delenv("GOOGLE_GEOCODING_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    monkeypatch.setenv("COREOS_GEOCODE_PROVIDER", "auto")
    assert geo.resolve_geo_provider_name() == "osm"
    monkeypatch.setenv("GOOGLE_GEOCODING_API_KEY", "k")
    assert geo.resolve_geo_provider_name() == "google"
    monkeypatch.setenv("COREOS_GEOCODE_PROVIDER", "osm")
    assert geo.resolve_geo_provider_name() == "osm"          # explicit override wins
    monkeypatch.setenv("COREOS_GEOCODE_PROVIDER", "google")
    monkeypatch.delenv("GOOGLE_GEOCODING_API_KEY", raising=False)
    assert geo.resolve_geo_provider_name() == "google"       # explicit; will report NOT_CONFIGURED


def test_geocoding_key_falls_back_to_places_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_GEOCODING_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "shared-key")
    assert geo._geocoding_key() == "shared-key"
    monkeypatch.setenv("GOOGLE_GEOCODING_API_KEY", "dedicated-key")
    assert geo._geocoding_key() == "dedicated-key"           # dedicated key preferred


# ===================== endpoint contract =====================
class _StubGeo:
    def __init__(self, name="osm", attribution="© OpenStreetMap contributors",
                 result=None, error=None):
        self.name, self.attribution = name, attribution
        self._result, self._error = result or [], error

    def is_configured(self):
        return True

    async def geocode(self, query):
        if self._error:
            raise pc.ProviderError(self._error)
        return self._result


def _use_geo(monkeypatch, stub):
    monkeypatch.setattr(main.geocode, "get_geo_provider", lambda *a, **k: stub)


_RESULT_ROW = {"id": "osm:r111", "label": "Cyberjaya, Selangor, Malaysia",
               "latitude": 2.9213, "longitude": 101.6559, "provider": "osm",
               "result_type": "administrative"}


def test_geocode_search_requires_auth(client):
    r = client.post("/api/geocode/search", json={"query": "Cyberjaya"})
    assert r.status_code == 401


def test_geocode_search_ok(alice, monkeypatch):
    tc, _ = alice
    _use_geo(monkeypatch, _StubGeo(result=[_RESULT_ROW]))
    b = tc.post("/api/geocode/search", json={"query": "Cyberjaya"}).json()
    assert b["status"] == "OK"
    assert b["provider"] == "osm"
    assert b["results"] == [_RESULT_ROW]
    assert b["attribution"] == "© OpenStreetMap contributors"
    assert b["error"] is None


def test_geocode_search_no_results(alice, monkeypatch):
    tc, _ = alice
    _use_geo(monkeypatch, _StubGeo(result=[]))
    b = tc.post("/api/geocode/search", json={"query": "asdkjhaskjdh"}).json()
    assert b["status"] == "NO_RESULTS" and b["results"] == [] and b["error"] is None


@pytest.mark.parametrize("kind", list(pc.PROVIDER_ERROR_KINDS))
def test_geocode_search_provider_error_is_normalised(alice, monkeypatch, kind):
    tc, _ = alice
    _use_geo(monkeypatch, _StubGeo(error=kind))
    b = tc.post("/api/geocode/search", json={"query": "Cyberjaya"}).json()
    assert b["status"] == "PROVIDER_ERROR" and b["error"] == kind and b["results"] == []


@pytest.mark.parametrize("body", [
    {},                                   # missing query
    {"query": 123},                       # not a string
    {"query": "a"},                       # too short
    {"query": " x "},                     # too short after strip
    {"query": "z" * (geo.GEOCODE_QUERY_MAX + 1)},   # too long
])
def test_geocode_search_validation_422(alice, monkeypatch, body):
    tc, _ = alice
    _use_geo(monkeypatch, _StubGeo(result=[]))
    assert tc.post("/api/geocode/search", json=body).status_code == 422


def test_geocode_search_rejects_non_object_body(alice, monkeypatch):
    tc, _ = alice
    _use_geo(monkeypatch, _StubGeo(result=[]))
    r = tc.post("/api/geocode/search", content=b'"just a string"',
                headers={"Content-Type": "application/json"})
    assert r.status_code == 422


def test_geocode_search_does_not_auto_select(alice, monkeypatch):
    """The server returns a LIST; it never marks one result as chosen."""
    tc, _ = alice
    rows = [dict(_RESULT_ROW, id="osm:r1"), dict(_RESULT_ROW, id="osm:r2")]
    _use_geo(monkeypatch, _StubGeo(result=rows))
    b = tc.post("/api/geocode/search", json={"query": "Cyberjaya"}).json()
    assert len(b["results"]) == 2
    assert "selected" not in b and "chosen" not in b
    for r in b["results"]:
        assert "selected" not in r


# ===================== privacy =====================
def test_geocode_endpoint_never_returns_the_key(alice, monkeypatch):
    tc, _ = alice

    def handler(req):
        return httpx.Response(200, json=_GOOGLE_OK)

    stub = geo.GoogleGeocodeProvider(api_key="test-geo-key-DO-NOT-LEAK", client_factory=_mock_factory(handler))
    _use_geo(monkeypatch, stub)
    r = tc.post("/api/geocode/search", json={"query": "Cyberjaya"})
    assert r.status_code == 200
    assert "test-geo-key-DO-NOT-LEAK" not in r.text
    assert "key=" not in r.text.lower() and "api_key" not in r.text.lower()


def test_geocode_search_does_not_log_query_or_coordinates(alice, monkeypatch, capfd):
    tc, _ = alice
    _use_geo(monkeypatch, _StubGeo(error=pc.PROVIDER_TIMEOUT))
    tc.post("/api/geocode/search", json={"query": "1 Secret Street KLCC 3.1578"})
    out = capfd.readouterr()
    combined = out.out + out.err
    assert "Secret Street" not in combined
    assert "3.1578" not in combined
    assert "PROVIDER_TIMEOUT" in combined          # the KIND is logged, nothing else


def test_geocode_search_is_stateless(alice, monkeypatch):
    tc, _ = alice
    _use_geo(monkeypatch, _StubGeo(result=[_RESULT_ROW]))
    c = db_layer.get_connection()
    tables_before = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    head = c.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    c.close()
    tc.post("/api/geocode/search", json={"query": "Cyberjaya"})
    c = db_layer.get_connection()
    assert {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")} == tables_before
    assert c.execute("SELECT version_num FROM alembic_version").fetchone()[0] == head
    c.close()


def test_geocode_search_does_not_touch_ai(alice, monkeypatch):
    tc, _ = alice
    called = {"n": 0}
    monkeypatch.setattr(main.AIService, "interpret",
                        staticmethod(lambda *_a, **_k: called.__setitem__("n", called["n"] + 1)))
    _use_geo(monkeypatch, _StubGeo(result=[_RESULT_ROW]))
    tc.post("/api/geocode/search", json={"query": "Cyberjaya"})
    assert called["n"] == 0


# ===================== config surface =====================
def test_env_example_documents_geocode_vars_without_a_real_key():
    src = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "COREOS_GEOCODE_PROVIDER" in src
    assert "GOOGLE_GEOCODING_API_KEY" in src
    assert "NEXT_PUBLIC_GOOGLE_GEOCODING" not in src           # never a browser key
    import re
    assert not re.search(r"AIza[0-9A-Za-z_\-]{10,}", src)      # no real key committed
