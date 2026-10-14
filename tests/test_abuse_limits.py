"""Phase 5H-B2 — abuse / denial-of-wallet protection.

Proves that the paid / rate-policed provider routes cannot be driven in a
request storm:

  * a per-(authenticated user + route) sliding-window limiter returns 429
    BEFORE any upstream provider / model call;
  * the limiter is content-independent — varying the query text / coordinates /
    place id / activity does NOT bypass it;
  * per-user and per-route buckets are isolated;
  * one /api/activities/search fans out to a hard-bounded number of upstream
    calls;
  * /api/process-input rejects an oversized command before OpenAI is touched.

Temp DB, no real provider / model is ever reached.
"""
import auth as auth_core
import main
from conftest import new_authed_client

_KLCC = (3.1578, 101.7117)


# --------------------------------------------------------------------------- #
# counting stubs — every upstream entry point increments a counter
# --------------------------------------------------------------------------- #
class _CountingPlaces:
    name = "osm"
    attribution = "© OpenStreetMap contributors"

    def __init__(self, *, with_texts=False):
        self.nearby_calls = 0
        self.text_query_calls = 0            # one per individual text query issued
        self._with_texts = with_texts

    async def search_nearby(self, **_kw):
        self.nearby_calls += 1
        return []

    # only present when with_texts=True (mirrors GooglePlacesProvider)
    async def search_texts(self, *, queries, max_queries=3, **_kw):
        n = 0
        for _q in list(queries)[:max_queries]:
            self.text_query_calls += 1
            n += 1
        return []

    def __getattr__(self, name):
        if name == "search_texts" and not self._with_texts:
            raise AttributeError(name)
        raise AttributeError(name)


class _CountingGeo:
    name = "osm"
    attribution = "© OpenStreetMap contributors"

    def __init__(self):
        self.calls = 0

    async def geocode(self, query):
        self.calls += 1
        return []


class _CountingGP:
    """Stand-in for GooglePlacesProvider in /api/places/resolve."""
    def __init__(self, *a, **k):
        pass

    def is_configured(self):
        return True

    async def place_details(self, **_kw):
        _CountingGP.calls += 1
        return None
_CountingGP.calls = 0


class _CountingAI:
    calls = 0

    @staticmethod
    def interpret(_text, context_date=None, context_entity=None, user_interests=None):
        _CountingAI.calls += 1
        raise ValueError("API_KEY_MISSING")   # bail before any real model call


def _reset_counters():
    _CountingGP.calls = 0
    _CountingAI.calls = 0


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _set_limit(monkeypatch, attr, value):
    monkeypatch.setattr(main.sec, attr, value)


def _places_body(lat=_KLCC[0], lng=_KLCC[1], activity="futsal"):
    return {"activity": activity, "latitude": lat, "longitude": lng}


def _activity_body(lat=_KLCC[0], lng=_KLCC[1], activity_id="running"):
    return {"activity_id": activity_id,
            "origin": {"latitude": lat, "longitude": lng, "source": "device"}}


def _weather_body(lat=_KLCC[0], lng=_KLCC[1]):
    return {"origin": {"latitude": lat, "longitude": lng}}


def _resolve_body():
    return {"provider": "google", "provider_place_id": "ChIJ_abc_123",
            "name": "KLCC Park", "latitude": _KLCC[0], "longitude": _KLCC[1]}


# ============================ per-endpoint limit ============================
def test_places_search_limit_then_429(alice, monkeypatch):
    tc, _ = alice
    stub = _CountingPlaces()
    monkeypatch.setattr(main.places_provider, "get_provider", lambda *a, **k: stub)
    _set_limit(monkeypatch, "RL_PLACES_SEARCH_MAX", 3)

    for i in range(3):
        assert tc.post("/api/places/search", json=_places_body()).status_code == 200, i
    r = tc.post("/api/places/search", json=_places_body())
    assert r.status_code == 429
    # upstream was NOT invoked for the rejected request
    assert stub.nearby_calls == 3


def test_geocode_limit_then_429(alice, monkeypatch):
    tc, _ = alice
    stub = _CountingGeo()
    monkeypatch.setattr(main.geocode, "get_geo_provider", lambda *a, **k: stub)
    _set_limit(monkeypatch, "RL_GEOCODE_MAX", 2)

    assert tc.post("/api/geocode/search", json={"query": "Cyberjaya"}).status_code == 200
    assert tc.post("/api/geocode/search", json={"query": "Putrajaya"}).status_code == 200
    r = tc.post("/api/geocode/search", json={"query": "Bangi"})
    assert r.status_code == 429
    assert stub.calls == 2


def test_activities_limit_then_429(alice, monkeypatch):
    tc, _ = alice
    stub = _CountingPlaces(with_texts=True)
    monkeypatch.setattr(main.places_provider, "get_provider", lambda *a, **k: stub)
    _set_limit(monkeypatch, "RL_ACTIVITIES_MAX", 2)

    assert tc.post("/api/activities/search", json=_activity_body()).status_code == 200
    assert tc.post("/api/activities/search", json=_activity_body()).status_code == 200
    r = tc.post("/api/activities/search", json=_activity_body())
    assert r.status_code == 429
    nearby_after = stub.nearby_calls
    tc.post("/api/activities/search", json=_activity_body())     # still limited
    assert stub.nearby_calls == nearby_after                     # no new upstream


def test_weather_limit_then_429(alice, monkeypatch):
    tc, _ = alice
    calls = {"n": 0}

    async def _fake_forecast(prov, lat, lng, **k):
        calls["n"] += 1
        raise main.weather_core.WeatherProviderError(main.weather_core.WEATHER_UNAVAILABLE)

    monkeypatch.setattr(main.weather_provider, "get_forecast", _fake_forecast)
    _set_limit(monkeypatch, "RL_WEATHER_MAX", 2)

    tc.post("/api/weather/context", json=_weather_body())
    tc.post("/api/weather/context", json=_weather_body())
    r = tc.post("/api/weather/context", json=_weather_body())
    assert r.status_code == 429
    assert calls["n"] == 2


def test_process_input_limit_then_429(alice, monkeypatch):
    tc, _ = alice
    _reset_counters()
    monkeypatch.setattr(main.AIService, "interpret", _CountingAI.interpret)
    _set_limit(monkeypatch, "RL_AI_MAX", 3)

    for _ in range(3):
        assert tc.post("/api/process-input", json={"user_input": "log rm10 coffee"}).status_code == 200
    r = tc.post("/api/process-input", json={"user_input": "log rm10 coffee"})
    assert r.status_code == 429
    assert _CountingAI.calls == 3          # the rejected call never reached interpret


def test_places_resolve_limit_then_429(alice, monkeypatch):
    tc, _ = alice
    _reset_counters()
    monkeypatch.setattr(main.places_provider, "GooglePlacesProvider", _CountingGP)
    _set_limit(monkeypatch, "RL_PLACES_RESOLVE_MAX", 2)

    assert tc.post("/api/places/resolve", json=_resolve_body()).status_code == 200
    assert tc.post("/api/places/resolve", json=_resolve_body()).status_code == 200
    r = tc.post("/api/places/resolve", json=_resolve_body())
    assert r.status_code == 429
    assert _CountingGP.calls == 2


# ============================ isolation ============================
def test_limit_is_per_user(monkeypatch):
    tc_a, _ = new_authed_client("rl_a@example.com")
    tc_b, _ = new_authed_client("rl_b@example.com")
    stub = _CountingPlaces()
    monkeypatch.setattr(main.places_provider, "get_provider", lambda *a, **k: stub)
    _set_limit(monkeypatch, "RL_PLACES_SEARCH_MAX", 2)

    assert tc_a.post("/api/places/search", json=_places_body()).status_code == 200
    assert tc_a.post("/api/places/search", json=_places_body()).status_code == 200
    assert tc_a.post("/api/places/search", json=_places_body()).status_code == 429
    # B is completely unaffected by A hitting A's ceiling
    assert tc_b.post("/api/places/search", json=_places_body()).status_code == 200
    assert tc_b.post("/api/places/search", json=_places_body()).status_code == 200
    assert tc_b.post("/api/places/search", json=_places_body()).status_code == 429


def test_limit_is_per_route(alice, monkeypatch):
    tc, _ = alice
    pstub = _CountingPlaces()
    gstub = _CountingGeo()
    monkeypatch.setattr(main.places_provider, "get_provider", lambda *a, **k: pstub)
    monkeypatch.setattr(main.geocode, "get_geo_provider", lambda *a, **k: gstub)
    _set_limit(monkeypatch, "RL_PLACES_SEARCH_MAX", 2)
    _set_limit(monkeypatch, "RL_GEOCODE_MAX", 5)

    tc.post("/api/places/search", json=_places_body())
    tc.post("/api/places/search", json=_places_body())
    assert tc.post("/api/places/search", json=_places_body()).status_code == 429
    # geocode has its own bucket — exhausting places must not exhaust it
    assert tc.post("/api/geocode/search", json={"query": "Cyberjaya"}).status_code == 200


# ============================ cache-bypass / content-independence ============================
def test_varying_coordinates_does_not_bypass_places_limit(alice, monkeypatch):
    tc, _ = alice
    stub = _CountingPlaces()
    monkeypatch.setattr(main.places_provider, "get_provider", lambda *a, **k: stub)
    _set_limit(monkeypatch, "RL_PLACES_SEARCH_MAX", 3)

    # every request uses DIFFERENT coordinates — the limiter is user/route keyed
    for i in range(3):
        assert tc.post("/api/places/search",
                       json=_places_body(lat=1.0 + i * 0.5, lng=100.0 + i * 0.5)).status_code == 200
    assert tc.post("/api/places/search",
                   json=_places_body(lat=42.0, lng=-71.0)).status_code == 429


def test_varying_query_and_activity_does_not_bypass_limit(alice, monkeypatch):
    tc, _ = alice
    gstub = _CountingGeo()
    astub = _CountingPlaces(with_texts=True)
    monkeypatch.setattr(main.geocode, "get_geo_provider", lambda *a, **k: gstub)
    monkeypatch.setattr(main.places_provider, "get_provider", lambda *a, **k: astub)
    _set_limit(monkeypatch, "RL_GEOCODE_MAX", 2)
    _set_limit(monkeypatch, "RL_ACTIVITIES_MAX", 2)

    for q in ("aaa", "bbb"):
        assert tc.post("/api/geocode/search", json={"query": q}).status_code == 200
    assert tc.post("/api/geocode/search", json={"query": "ccc"}).status_code == 429

    for a in ("running", "cycling"):
        assert tc.post("/api/activities/search", json=_activity_body(activity_id=a)).status_code == 200
    assert tc.post("/api/activities/search", json=_activity_body(activity_id="futsal")).status_code == 429


# ============================ 429 contract ============================
def test_429_response_contract(alice, monkeypatch):
    tc, _ = alice
    monkeypatch.setattr(main.places_provider, "get_provider", lambda *a, **k: _CountingPlaces())
    _set_limit(monkeypatch, "RL_PLACES_SEARCH_MAX", 1)

    assert tc.post("/api/places/search", json=_places_body()).status_code == 200
    r = tc.post("/api/places/search", json=_places_body())
    assert r.status_code == 429
    body = r.json()
    assert body == {"detail": "Too many requests right now. Try again shortly."}
    # Retry-After present, a positive whole number of seconds
    ra = r.headers.get("Retry-After")
    assert ra is not None and ra.isdigit() and int(ra) >= 1
    # nothing sensitive leaked
    blob = (r.text + " " + " ".join(f"{k}:{v}" for k, v in r.headers.items())).lower()
    for bad in ("rl:", "user", "usr_", "quota", "openai", "google", "billing", "bucket", "3.1578", "101.7117"):
        assert bad not in blob


# ============================ AI input bound ============================
def _ai_ok(monkeypatch):
    _reset_counters()
    monkeypatch.setattr(main.AIService, "interpret", _CountingAI.interpret)
    _set_limit(monkeypatch, "RL_AI_MAX", 50)          # keep the rate limiter out of the way


def test_ai_input_normal_is_accepted(alice, monkeypatch):
    tc, _ = alice
    _ai_ok(monkeypatch)
    r = tc.post("/api/process-input", json={"user_input": "I spent RM45 on Shopee"})
    assert r.status_code == 200
    assert _CountingAI.calls == 1


def test_ai_input_exact_max_is_accepted(alice, monkeypatch):
    tc, _ = alice
    _ai_ok(monkeypatch)
    r = tc.post("/api/process-input", json={"user_input": "x" * main.sec.AI_INPUT_MAX_CHARS})
    assert r.status_code == 200
    assert _CountingAI.calls == 1


def test_ai_input_max_plus_one_is_rejected_before_model(alice, monkeypatch):
    tc, _ = alice
    _ai_ok(monkeypatch)
    r = tc.post("/api/process-input", json={"user_input": "x" * (main.sec.AI_INPUT_MAX_CHARS + 1)})
    assert r.status_code == 422
    assert _CountingAI.calls == 0                     # model never touched
    assert "too long" in r.json()["detail"].lower()


def test_ai_input_huge_is_rejected_before_model(alice, monkeypatch):
    tc, _ = alice
    _ai_ok(monkeypatch)
    r = tc.post("/api/process-input", json={"user_input": "A" * 5_000_000})
    assert r.status_code == 422
    assert _CountingAI.calls == 0


def test_ai_empty_input_behaviour_unchanged(alice, monkeypatch):
    # empty stays on the existing path (not a new 422 from the length guard)
    tc, _ = alice
    _ai_ok(monkeypatch)
    r = tc.post("/api/process-input", json={"user_input": ""})
    assert r.status_code == 200                       # reaches interpret as before
    assert _CountingAI.calls == 1


# ============================ activity fan-out bound ============================
# B9.1: the adaptive nearby-search is a TRUE staged provider search — one real
# request per radius rung (8 km, then 12 km, then 20 km). The upstream fan-out is
# therefore bounded by len(ACTIVITY_RADIUS_LADDER_M) == 3 sequential
# search_nearby calls, each with at most SEARCH_TEXTS_MAX_QUERIES text queries —
# never a 4th rung, never an unbounded storm. Unrelated per-user / per-route rate
# limiting is unchanged.
def test_one_activity_request_has_a_bounded_staged_upstream_fanout(alice, monkeypatch):
    tc, _ = alice
    stub = _CountingPlaces(with_texts=True)
    monkeypatch.setattr(main.places_provider, "get_provider", lambda *a, **k: stub)
    _set_limit(monkeypatch, "RL_ACTIVITIES_MAX", 50)

    # running == a public-geography profile -> triggers the text fan-out; the
    # counting stub returns [] so all three rungs run.
    assert tc.post("/api/activities/search", json=_activity_body(activity_id="running")).status_code == 200
    cap = main.places_provider.SEARCH_TEXTS_MAX_QUERIES
    rungs = len(main.places.ACTIVITY_RADIUS_LADDER_M)     # 3
    assert rungs == 3
    assert stub.nearby_calls <= rungs
    assert stub.text_query_calls <= rungs * cap
    assert stub.nearby_calls + stub.text_query_calls <= rungs * (1 + cap)


def test_oversized_profile_query_list_cannot_exceed_staged_fanout_bound(alice, monkeypatch):
    tc, _ = alice
    stub = _CountingPlaces(with_texts=True)
    monkeypatch.setattr(main.places_provider, "get_provider", lambda *a, **k: stub)
    _set_limit(monkeypatch, "RL_ACTIVITIES_MAX", 50)

    prof = main.activity_core.ACTIVITY_PROFILES["running"]
    monkeypatch.setattr(prof, "google_text_queries", [f"q{i}" for i in range(25)])
    tc.post("/api/activities/search", json=_activity_body(activity_id="running"))
    rungs = len(main.places.ACTIVITY_RADIUS_LADDER_M)
    assert stub.text_query_calls <= rungs * main.places_provider.SEARCH_TEXTS_MAX_QUERIES


# ============================ limiter storage bounds ============================
def test_rate_limit_hit_evicts_when_over_bucket_cap(monkeypatch):
    monkeypatch.setattr(auth_core.cfg, "RL_MAX_BUCKETS", 5)
    auth_core._attempts.clear()
    for i in range(12):
        auth_core.rate_limit_hit(f"rl:test:{i}", 3, 60)
    assert len(auth_core._attempts) <= 5


def test_rate_limit_sweep_drops_idle_buckets(monkeypatch):
    auth_core._attempts.clear()
    # a stale bucket: last hit far in the past (monotonic seconds)
    from collections import deque
    auth_core._attempts["rl:stale:u"] = deque([1.0])
    monkeypatch.setattr(auth_core, "_RL_SWEEP_EVERY", 1)
    monkeypatch.setattr(auth_core, "_RL_STALE_AFTER_S", 1)
    auth_core.rate_limit_hit("rl:fresh:u", 5, 60)     # triggers a sweep
    assert "rl:stale:u" not in auth_core._attempts
    assert "rl:fresh:u" in auth_core._attempts


def test_zero_or_negative_limit_blocks_everything():
    ok, retry = auth_core.rate_limit_hit("rl:zero:u", 0, 60)
    assert ok is False and retry >= 1


def test_retry_after_is_within_window(monkeypatch):
    auth_core._attempts.clear()
    auth_core.rate_limit_hit("rl:w:u", 1, 30)
    ok, retry = auth_core.rate_limit_hit("rl:w:u", 1, 30)
    assert ok is False and 1 <= retry <= 31


# ============================ auth limiter still works ============================
def test_auth_rate_limit_still_enforced(client, monkeypatch):
    monkeypatch.setattr(auth_core.cfg, "AUTH_RATE_MAX_ATTEMPTS", 3)
    codes = [client.post("/api/auth/login",
                         json={"email": f"none{i}@x.com", "password": "wrongpass1"}).status_code
             for i in range(5)]
    assert codes.count(429) >= 1
    assert codes[0] == 401            # first attempts are normal auth failures
