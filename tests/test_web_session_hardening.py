"""Phase 5H-B4 — web / session hardening.

Covers the runtime-observable behaviour of this pass:
  * M3  backend exposure — FastAPI binds loopback; the front door (Caddy) is
        the only browser/LAN entry point and it sanitises X-Forwarded-For
        (verified against the real Caddy v2.11.4 binary + dev/Caddyfile).
  * M4  browser Origin defense-in-depth on authenticated mutations
  * M5  X-Content-Type-Options on API responses (document headers -> Next.js)
  * L1  provider website URLs restricted to http/https
  * 422 privacy: RequestValidationError body no longer echoes submitted values
  * L3  docs routing
"""
import pytest
from fastapi.testclient import TestClient

import main
import security_config as sec


# --------------------------------------------------------------------------- #
# M3 — backend exposure. There is no CoreOS-side proxy-header parser: the
# real Caddy front door discards client-supplied X-Forwarded-For and uvicorn's
# default (trust 127.0.0.1) adopts Caddy's value. The hardening here is that
# FastAPI is not LAN-reachable at all, so a client cannot bypass Caddy.
# --------------------------------------------------------------------------- #
def test_backend_binds_loopback_by_default(tmp_path):
    # The CODE default must be loopback (a fresh clone with no COREOS_HOST set
    # is not LAN-exposed). Run in a clean process with COREOS_HOST unset and no
    # .env on the cwd, so a developer's local override cannot mask the default.
    import subprocess, sys, os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {k: v for k, v in os.environ.items() if not k.startswith("COREOS_")}
    env["PYTHONPATH"] = root
    env["COREOS_ENV"] = "development"
    env["COREOS_DB_PATH"] = str(tmp_path / "hostcheck.db")
    r = subprocess.run(
        [sys.executable, "-c", "import main; print('HOST', main.HOST)"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    assert "HOST 127.0.0.1" in r.stdout, r.stdout


def test_client_ip_helper_is_the_socket_peer_unchanged():
    # No custom forwarded-header parsing was added; the helper is the plain
    # request.client.host (which, behind Caddy, is the sanitised true client).
    class _C:  # minimal stand-in
        host = "203.0.113.10"

    class _R:
        client = _C()
        headers = {"x-forwarded-for": "1.2.3.4"}  # must be irrelevant here

    assert main._client_ip(_R()) == "203.0.113.10"

    class _R2:
        client = None
        headers = {}

    assert main._client_ip(_R2()) == "unknown"


# --------------------------------------------------------------------------- #
# M4 — Origin defense-in-depth
# --------------------------------------------------------------------------- #
_ALLOWED_ORIGIN = "http://localhost:3000"  # a dev-default allowed origin


def test_mutation_with_allowed_origin_passes(alice):
    tc, _ = alice
    r = tc.post("/api/transactions",
                json={"amount": 4.5, "description": "coffee"},
                headers={"origin": _ALLOWED_ORIGIN})
    assert r.status_code == 201, r.text


def test_mutation_with_disallowed_origin_is_403_and_no_row(alice):
    tc, _ = alice
    before = tc.get("/api/finance").json()
    r = tc.post("/api/transactions",
                json={"amount": 99.0, "description": "evil"},
                headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    assert "evil.example" not in r.text  # never echo the rejected origin
    after = tc.get("/api/finance").json()
    assert before == after  # mutation did not happen


def test_mutation_with_malformed_origin_is_403(alice):
    tc, _ = alice
    bad_origins = [
        "null",
        "not a url",
        "http://",
        "javascript:alert(1)",
        "ftp://localhost:3000",
        # comma-joined / duplicated header value
        "http://localhost:3000, https://evil.example",
        # userinfo confusion: real host is evil, authority host is an allowed one
        "http://localhost:3000@evil.example",
        "http://evil.example@localhost:3000",
        # trailing junk / path (Origin must be scheme://host[:port] only)
        "http://localhost:3000/evil",
    ]
    for bad in bad_origins:
        r = tc.post("/api/transactions",
                    json={"amount": 1.0, "description": "x"},
                    headers={"origin": bad})
        assert r.status_code == 403, f"{bad!r} was not rejected"


def test_options_preflight_is_not_blocked_by_origin_guard(client):
    # CORS preflight for a mutation carries a cross-origin Origin by design.
    r = client.options(
        "/api/transactions",
        headers={
            "origin": "https://evil.example",
            "access-control-request-method": "POST",
        },
    )
    assert r.status_code != 403  # handled by CORSMiddleware, not our guard


def test_mutation_without_origin_is_allowed(alice):
    # native / server-to-server / test client: no Origin header at all
    tc, _ = alice
    r = tc.post("/api/transactions", json={"amount": 2.0, "description": "cli"})
    assert r.status_code == 201, r.text


def test_safe_get_is_unaffected_by_bad_origin(alice):
    tc, _ = alice
    r = tc.get("/api/finance", headers={"origin": "https://evil.example"})
    assert r.status_code == 200


def test_login_still_works_with_same_site_origin(client):
    client.post("/api/auth/register",
                json={"email": "org@example.com", "password": "password123"},
                headers={"origin": _ALLOWED_ORIGIN})
    r = client.post("/api/auth/login",
                    json={"email": "org@example.com", "password": "password123"},
                    headers={"origin": _ALLOWED_ORIGIN})
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# M5 — baseline API response header
# --------------------------------------------------------------------------- #
def test_api_responses_carry_nosniff(client):
    assert client.get("/api/finance/meta").headers.get("x-content-type-options") == "nosniff"


# --------------------------------------------------------------------------- #
# 422 privacy — no echoed input
# --------------------------------------------------------------------------- #
def test_validation_error_body_does_not_echo_submitted_values(client):
    r = client.post("/api/auth/login",
                    json={"email": 123, "password": {"secret": "SUPERSECRET-PW"}})
    assert r.status_code == 422
    body = r.json()
    assert "SUPERSECRET-PW" not in r.text
    for item in body["detail"]:
        assert set(item.keys()) <= {"type", "loc", "msg"}
        assert "input" not in item and "ctx" not in item
    # still useful: the offending field is named
    locs = [".".join(str(p) for p in it["loc"]) for it in body["detail"]]
    assert any("password" in loc for loc in locs)


def test_validation_error_still_422_for_omni_and_finance(alice):
    tc, _ = alice
    r = tc.post("/api/process-input", json={"not_user_input": "x"})
    assert r.status_code == 422
    assert "not_user_input" not in r.text or "input" not in r.json()["detail"][0]


# --------------------------------------------------------------------------- #
# L1 — provider website scheme allowlist (backend trust boundary)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [
    "javascript:alert(1)", "data:text/html,<script>1</script>",
    "file:///etc/passwd", "vbscript:msgbox(1)", "  ", "notaurl", "://nohost",
])
def test_places_core_drops_non_web_website(bad):
    import places_core
    p = places_core.build_place(
        provider="osm", provider_place_id="x1", name="Test Cafe",
        latitude=3.15, longitude=101.7, origin_lat=3.15, origin_lng=101.7,
        activity="cafe", activity_match="exact", website=bad,
    )
    assert p is not None
    assert p["website"] is None
    assert p["evidence"]["contact"]["website"] is None


@pytest.mark.parametrize("good", [
    "https://example.com", "http://example.com/path?q=1", "https://sub.example.co.uk/",
])
def test_places_core_keeps_web_website(good):
    import places_core
    p = places_core.build_place(
        provider="osm", provider_place_id="x2", name="Test Cafe",
        latitude=3.15, longitude=101.7, origin_lat=3.15, origin_lng=101.7,
        activity="cafe", activity_match="exact", website=good,
    )
    assert p["website"] == good


# --------------------------------------------------------------------------- #
# L3 — docs routing + server header
# --------------------------------------------------------------------------- #
def test_docs_are_served_under_api_prefix_in_dev(client):
    # dev test env -> EXPOSE_DOCS is True and mounted under /api/*
    assert client.get("/api/openapi.json").status_code == 200
    assert client.get("/openapi.json").status_code == 404  # not at the bare path
