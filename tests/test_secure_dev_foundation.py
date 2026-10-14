"""Phase 5A — secure development foundation.

Covers the configuration surface that makes a single trusted HTTPS dev origin
possible WITHOUT weakening Phase 2 production security:

  * COREOS_COOKIE_SECURE override + `_env_bool` parsing
  * production `validate_startup()` still rejects every unsafe combination, and
    now also rejects a non-Secure session cookie in production
  * the development CORS allowlist includes the HTTPS proxy origin and never a
    wildcard; CORS stays credential-safe and explicit
  * no browser-facing hardcoded http:// backend URL remains in frontend source
  * the frontend defaults to same-origin and carries a mixed-content guard
  * Next.js proxies /api to the backend server-side (same-origin API)

Nothing here starts Caddy or a real HTTPS listener — those are manual steps
documented in docs/secure-development.md. The physical iPhone secure-context
test happens later in Phase 5.
"""
import os
import pathlib
import re
import subprocess
import sys

import pytest

import main
import security_config as sec

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------- #
# _env_bool
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,default,expect", [
    (None, False, False), (None, True, True),
    ("", False, False), ("   ", True, True),
    ("1", False, True), ("true", False, True), ("YES", False, True), ("On", False, True),
    ("0", True, False), ("false", True, False), ("no", True, False), ("OFF", True, False),
    ("banana", True, True), ("banana", False, False),
])
def test_env_bool(monkeypatch, raw, default, expect):
    if raw is None:
        monkeypatch.delenv("COREOS_TEST_BOOL", raising=False)
    else:
        monkeypatch.setenv("COREOS_TEST_BOOL", raw)
    assert sec._env_bool("COREOS_TEST_BOOL", default) is expect


# --------------------------------------------------------------------------- #
# current (development / test) cookie + CORS state
# --------------------------------------------------------------------------- #
def test_dev_cookie_defaults_are_unchanged():
    assert sec.COOKIE_NAME == "coreos_session"
    assert sec.COOKIE_SAMESITE == "lax"
    # test env is development and does not set COREOS_COOKIE_SECURE
    assert sec.COOKIE_SECURE is False


def test_allowed_origins_never_wildcard_and_are_well_formed():
    assert "*" not in sec.ALLOWED_ORIGINS
    assert sec.ALLOWED_ORIGINS, "an allowlist must exist in development"
    for o in sec.ALLOWED_ORIGINS:
        assert isinstance(o, str) and o.startswith(("http://", "https://"))
        assert not o.endswith("/")


def test_cors_middleware_is_explicit_and_credential_safe():
    cors = next(m for m in main.app.user_middleware if "CORSMiddleware" in str(m.cls))
    opts = getattr(cors, "kwargs", None) or getattr(cors, "options", {})
    assert opts.get("allow_credentials") is True
    origins = opts.get("allow_origins", [])
    assert "*" not in origins and origins == sec.ALLOWED_ORIGINS


# --------------------------------------------------------------------------- #
# isolated subprocess checks: dev defaults + production validate_startup()
# --------------------------------------------------------------------------- #
def _run_config(tmp_path, overrides):
    """Import security_config in a clean process (no inherited COREOS_* env, no
    .env on the cwd path) and print its resolved state, or fail on RuntimeError."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("COREOS_")}
    env["PYTHONPATH"] = ROOT
    env.update(overrides)
    code = (
        "import security_config as s;"
        "print('OK', s.COOKIE_SECURE, '|', ','.join(s.ALLOWED_ORIGINS))"
    )
    return subprocess.run([sys.executable, "-c", code], cwd=str(tmp_path),
                          env=env, capture_output=True, text=True)


def test_dev_defaults_include_https_proxy_origin_no_wildcard(tmp_path):
    r = _run_config(tmp_path, {"COREOS_ENV": "development"})
    assert r.returncode == 0, r.stderr
    assert "OK False |" in r.stdout
    assert "https://coreos.local" in r.stdout
    assert "*" not in r.stdout.split("|", 1)[1]


def test_dev_cookie_secure_opt_in(tmp_path):
    r = _run_config(tmp_path, {"COREOS_ENV": "development", "COREOS_COOKIE_SECURE": "1"})
    assert r.returncode == 0, r.stderr
    assert "OK True |" in r.stdout


def test_dev_custom_proxy_host_is_allowlisted(tmp_path):
    r = _run_config(tmp_path, {"COREOS_ENV": "development", "COREOS_DEV_HOST": "my-pc.local"})
    assert r.returncode == 0, r.stderr
    assert "https://my-pc.local" in r.stdout


_STRONG = "x" * 48

@pytest.mark.parametrize("overrides,ok,needle", [
    # a fully valid production config imports cleanly
    ({"COREOS_ENV": "production", "COREOS_SESSION_SECRET": _STRONG,
      "COREOS_ALLOWED_ORIGINS": "https://app.example.com", "COREOS_COOKIE_SECURE": "1"}, True, None),
    # weak / missing secret  (Phase 2 rule preserved)
    ({"COREOS_ENV": "production", "COREOS_SESSION_SECRET": "short",
      "COREOS_ALLOWED_ORIGINS": "https://app.example.com", "COREOS_COOKIE_SECURE": "1"}, False, "SESSION_SECRET"),
    # no origins  (Phase 2 rule preserved)
    ({"COREOS_ENV": "production", "COREOS_SESSION_SECRET": _STRONG,
      "COREOS_COOKIE_SECURE": "1"}, False, "ALLOWED_ORIGINS"),
    # public http origin  (Phase 2 rule preserved)
    ({"COREOS_ENV": "production", "COREOS_SESSION_SECRET": _STRONG,
      "COREOS_ALLOWED_ORIGINS": "http://app.example.com", "COREOS_COOKIE_SECURE": "1"}, False, "non-HTTPS public origin"),
    # NEW: production may not disable the Secure cookie
    ({"COREOS_ENV": "production", "COREOS_SESSION_SECRET": _STRONG,
      "COREOS_ALLOWED_ORIGINS": "https://app.example.com", "COREOS_COOKIE_SECURE": "0"}, False, "COREOS_COOKIE_SECURE"),
])
def test_production_validate_startup(tmp_path, overrides, ok, needle):
    r = _run_config(tmp_path, overrides)
    if ok:
        assert r.returncode == 0, r.stderr
        assert "OK True |" in r.stdout
    else:
        assert r.returncode != 0, f"expected failure, got:\n{r.stdout}"
        assert needle in r.stderr


# --------------------------------------------------------------------------- #
# Phase 5H-B2: the abuse-limit env vars are parsed with a bounded guard
# (_int_env). A malformed / unsafe explicit value must FAIL FAST at startup,
# never silently disable a control or clamp a dangerous value.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,default,mn,mx,ok,expect", [
    (None, 10, 1, 100, True, 10),        # unset -> default
    ("", 10, 1, 100, True, 10),          # empty -> default
    ("  ", 10, 1, 100, True, 10),        # whitespace -> default
    ("25", 10, 1, 100, True, 25),        # valid
    (" 25 ", 10, 1, 100, True, 25),      # valid, trimmed
    ("1", 10, 1, 100, True, 1),          # lower bound inclusive
    ("100", 10, 1, 100, True, 100),      # upper bound inclusive
])
def test_int_env_valid(monkeypatch, raw, default, mn, mx, ok, expect):
    if raw is None:
        monkeypatch.delenv("COREOS_TEST_INT", raising=False)
    else:
        monkeypatch.setenv("COREOS_TEST_INT", raw)
    assert sec._int_env("COREOS_TEST_INT", default, min_value=mn, max_value=mx) == expect


@pytest.mark.parametrize("raw,needle", [
    ("abc", "base-10 integer"),
    ("10.5", "base-10 integer"),
    ("0x1F", "base-10 integer"),
    ("1e6", "base-10 integer"),
    ("0", "out of the allowed range"),
    ("-1", "out of the allowed range"),
    ("101", "out of the allowed range"),
    ("999999999", "out of the allowed range"),
])
def test_int_env_rejects(monkeypatch, raw, needle):
    monkeypatch.setenv("COREOS_TEST_INT", raw)
    with pytest.raises(RuntimeError) as ei:
        sec._int_env("COREOS_TEST_INT", 10, min_value=1, max_value=100)
    assert "COREOS_TEST_INT" in str(ei.value)
    assert needle in str(ei.value)


@pytest.mark.parametrize("var,bad,needle", [
    # non-integer -> fail fast
    ("COREOS_RL_AI_MAX", "abc", "base-10 integer"),
    ("COREOS_RL_WINDOW", "sixty", "base-10 integer"),
    ("COREOS_AI_INPUT_MAX_CHARS", "2k", "base-10 integer"),
    # zero / negative -> would silently disable the control -> fail fast
    ("COREOS_RL_AI_MAX", "0", "out of the allowed range"),
    ("COREOS_RL_PLACES_SEARCH_MAX", "-5", "out of the allowed range"),
    ("COREOS_RL_WINDOW", "0", "out of the allowed range"),
    ("COREOS_AI_INPUT_MAX_CHARS", "0", "out of the allowed range"),
    # absurd -> "configured" yet ineffective -> fail fast
    ("COREOS_RL_AI_MAX", "999999999", "out of the allowed range"),
    ("COREOS_RL_WEATHER_MAX", "5000", "out of the allowed range"),
    ("COREOS_RL_WINDOW", "999999999", "out of the allowed range"),
    ("COREOS_AI_INPUT_MAX_CHARS", "100000000", "out of the allowed range"),
    # bucket cap below a usable minimum -> fail fast
    ("COREOS_RL_MAX_BUCKETS", "5", "out of the allowed range"),
    ("COREOS_RL_MAX_BUCKETS", "0", "out of the allowed range"),
    ("COREOS_RL_MAX_BUCKETS", "50000000", "out of the allowed range"),
])
def test_abuse_limit_env_fails_fast_on_bad_config(tmp_path, var, bad, needle):
    r = _run_config(tmp_path, {"COREOS_ENV": "development", var: bad})
    assert r.returncode != 0, f"expected startup failure for {var}={bad!r}, got:\n{r.stdout}"
    assert var in r.stderr and needle in r.stderr


@pytest.mark.parametrize("overrides,checks", [
    ({"COREOS_RL_AI_MAX": "25", "COREOS_AI_INPUT_MAX_CHARS": "500"},
     [("RL_AI_MAX", 25), ("AI_INPUT_MAX_CHARS", 500)]),
    ({"COREOS_RL_WINDOW": "120", "COREOS_RL_MAX_BUCKETS": "1000"},
     [("RL_WINDOW_SECONDS", 120), ("RL_MAX_BUCKETS", 1000)]),
    ({"COREOS_RL_WEATHER_MAX": "2000", "COREOS_RL_ACTIVITIES_MAX": "1"},
     [("RL_WEATHER_MAX", 2000), ("RL_ACTIVITIES_MAX", 1)]),
])
def test_abuse_limit_env_valid_override_imports_clean(tmp_path, overrides, checks):
    code = "import security_config as s; " + "; ".join(
        f"assert s.{name} == {val}, s.{name}" for name, val in checks
    ) + '; print("OK")'
    env = {k: v for k, v in os.environ.items() if not k.startswith("COREOS_")}
    env["PYTHONPATH"] = ROOT
    env["COREOS_ENV"] = "development"
    env.update(overrides)
    r = subprocess.run([sys.executable, "-c", code], cwd=str(tmp_path),
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_abuse_limit_defaults_are_sane():
    # every ceiling is a real positive number, and no default is absurd
    for name in ("RL_AI_MAX", "RL_PLACES_SEARCH_MAX", "RL_PLACES_RESOLVE_MAX",
                 "RL_GEOCODE_MAX", "RL_ACTIVITIES_MAX", "RL_WEATHER_MAX"):
        v = getattr(sec, name)
        assert 1 <= v <= 2000, (name, v)
    assert 1 <= sec.RL_WINDOW_SECONDS <= 3600
    assert 100 <= sec.RL_MAX_BUCKETS <= 1_000_000
    assert 1 <= sec.AI_INPUT_MAX_CHARS <= 100_000
    # the activity ceiling is set below plain places search (it fans out)
    assert sec.RL_ACTIVITIES_MAX <= sec.RL_PLACES_SEARCH_MAX


# --------------------------------------------------------------------------- #
# Phase 5H-B4: the pre-existing session / auth-throttle integers are now under
# the SAME fail-fast _int_env guard, and COREOS_PORT (main.py) too.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("var,bad,needle", [
    ("COREOS_SESSION_TTL_SECONDS", "forever", "base-10 integer"),
    ("COREOS_SESSION_TTL_SECONDS", "0", "out of the allowed range"),
    ("COREOS_SESSION_TTL_SECONDS", "-1", "out of the allowed range"),
    ("COREOS_SESSION_TTL_SECONDS", "999999999", "out of the allowed range"),
    ("COREOS_AUTH_RATE_MAX", "lots", "base-10 integer"),
    ("COREOS_AUTH_RATE_MAX", "0", "out of the allowed range"),
    ("COREOS_AUTH_RATE_MAX", "-3", "out of the allowed range"),
    ("COREOS_AUTH_RATE_MAX", "100000", "out of the allowed range"),
    ("COREOS_AUTH_RATE_WINDOW", "5m", "base-10 integer"),
    ("COREOS_AUTH_RATE_WINDOW", "0", "out of the allowed range"),
    ("COREOS_AUTH_RATE_WINDOW", "99999999", "out of the allowed range"),
])
def test_session_and_auth_int_env_fail_fast(tmp_path, var, bad, needle):
    r = _run_config(tmp_path, {"COREOS_ENV": "development", var: bad})
    assert r.returncode != 0, f"expected startup failure for {var}={bad!r}, got:\n{r.stdout}"
    assert var in r.stderr and needle in r.stderr


@pytest.mark.parametrize("var,bad,needle", [
    ("COREOS_PORT", "eighty", "base-10 integer"),
    ("COREOS_PORT", "0", "out of the allowed range"),
    ("COREOS_PORT", "70000", "out of the allowed range"),
])
def test_port_int_env_fail_fast(tmp_path, var, bad, needle):
    # main.py resolves PORT via sec.int_env; import it in a clean process.
    code = "import main"  # noqa
    env = {k: v for k, v in os.environ.items() if not k.startswith("COREOS_")}
    env["PYTHONPATH"] = ROOT
    env["COREOS_ENV"] = "development"
    env["COREOS_DB_PATH"] = str(tmp_path / "port_check.db")
    env[var] = bad
    r = subprocess.run([sys.executable, "-c", code], cwd=str(tmp_path),
                       env=env, capture_output=True, text=True)
    assert r.returncode != 0, f"expected failure for {var}={bad!r}, got:\n{r.stdout}"
    assert var in r.stderr and needle in r.stderr


def test_session_and_auth_defaults_unchanged():
    assert sec.SESSION_TTL_SECONDS == 604800
    assert sec.AUTH_RATE_MAX_ATTEMPTS == 10
    assert sec.AUTH_RATE_WINDOW_SECONDS == 300


@pytest.mark.parametrize("overrides,expect", [
    ({"COREOS_ENV": "development"}, "True"),
    ({"COREOS_ENV": "production", "COREOS_SESSION_SECRET": _STRONG,
      "COREOS_ALLOWED_ORIGINS": "https://app.example.com",
      "COREOS_COOKIE_SECURE": "1"}, "False"),
    ({"COREOS_ENV": "production", "COREOS_SESSION_SECRET": _STRONG,
      "COREOS_ALLOWED_ORIGINS": "https://app.example.com",
      "COREOS_COOKIE_SECURE": "1", "COREOS_EXPOSE_DOCS": "1"}, "True"),
])
def test_docs_exposure_flag(tmp_path, overrides, expect):
    code = "import security_config as s; print('DOCS', s.EXPOSE_DOCS)"
    env = {k: v for k, v in os.environ.items() if not k.startswith("COREOS_")}
    env["PYTHONPATH"] = ROOT
    env.update(overrides)
    r = subprocess.run([sys.executable, "-c", code], cwd=str(tmp_path),
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert f"DOCS {expect}" in r.stdout


# --------------------------------------------------------------------------- #
# frontend: same-origin default + no browser-facing http backend URL
# --------------------------------------------------------------------------- #
_APP = pathlib.Path(ROOT) / "app"
_PRIVATE_HTTP = re.compile(
    r"""https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|"""
    r"""10\.\d+|192\.168\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+)"""
)


def _runtime_lines(path: pathlib.Path):
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        s = line.strip()
        if s.startswith("//") or s.startswith("*") or s.startswith("/*"):
            continue
        yield i, line


def test_no_browser_facing_http_backend_url_in_app_source():
    offenders = []
    for f in list(_APP.rglob("*.ts")) + list(_APP.rglob("*.tsx")):
        for i, line in _runtime_lines(f):
            if _PRIVATE_HTTP.search(line):
                offenders.append(f"{f.relative_to(ROOT)}:{i}: {line.strip()}")
    assert not offenders, "hardcoded browser-facing backend URL(s):\n" + "\n".join(offenders)


def test_api_client_defaults_to_same_origin_with_mixed_content_guard():
    src = (_APP / "lib" / "api.ts").read_text(encoding="utf-8")
    assert "http://localhost:8080" not in src            # old hardcoded fallback gone
    assert '|| ""' in src                                # blank -> same-origin relative
    assert 'protocol === "https:"' in src and "http://" in src  # guard branch
    assert "mixed content" in src.lower()


def test_next_config_proxies_api_same_origin():
    src = (pathlib.Path(ROOT) / "next.config.ts").read_text(encoding="utf-8")
    assert "rewrites" in src
    assert "/api/:path*" in src
    assert "COREOS_BACKEND_ORIGIN" in src


# --------------------------------------------------------------------------- #
# Phase 5H-B6: cookie SameSite is validated (not handed raw to the cookie
# layer), and the reminder poll interval is under the same fail-fast int guard.
# --------------------------------------------------------------------------- #
def test_cookie_samesite_default_is_lax():
    assert sec.COOKIE_SAMESITE == "lax"


@pytest.mark.parametrize("value", ["lax", "strict", "none"])
def test_cookie_samesite_accepts_the_known_set(tmp_path, value):
    ov = {"COREOS_ENV": "development", "COREOS_COOKIE_SAMESITE": value}
    if value == "none":
        ov["COREOS_COOKIE_SECURE"] = "1"  # None requires Secure
    r = _run_config(tmp_path, ov)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("bad", ["bogus", "Lax; Domain=evil", "1"])
def test_cookie_samesite_rejects_unknown_values(tmp_path, bad):
    r = _run_config(tmp_path, {"COREOS_ENV": "development", "COREOS_COOKIE_SAMESITE": bad})
    assert r.returncode != 0, f"expected failure for SameSite={bad!r}, got:\n{r.stdout}"
    assert "COREOS_COOKIE_SAMESITE" in r.stderr


def test_cookie_samesite_none_requires_secure(tmp_path):
    # SameSite=None without Secure is a misconfiguration in every environment.
    r = _run_config(tmp_path, {"COREOS_ENV": "development", "COREOS_COOKIE_SAMESITE": "none"})
    assert r.returncode != 0, f"expected failure, got:\n{r.stdout}"
    assert "COREOS_COOKIE_SECURE" in r.stderr


def _run_import(tmp_path, module, overrides):
    env = {k: v for k, v in os.environ.items() if not k.startswith("COREOS_")}
    env["PYTHONPATH"] = ROOT
    env["COREOS_ENV"] = "development"
    env["COREOS_DB_PATH"] = str(tmp_path / "b6_check.db")
    env.update(overrides)
    code = f"import {module} as x; print('OK', getattr(x, 'POLL_SECONDS', None))"
    return subprocess.run([sys.executable, "-c", code], cwd=str(tmp_path),
                          env=env, capture_output=True, text=True)


def test_reminder_poll_seconds_default_is_60(tmp_path):
    r = _run_import(tmp_path, "scheduler", {})
    assert r.returncode == 0, r.stderr
    assert "OK 60" in r.stdout


@pytest.mark.parametrize("bad,needle", [
    ("abc", "base-10 integer"),
    ("0", "out of the allowed range"),
    ("-5", "out of the allowed range"),
    ("999999", "out of the allowed range"),
])
def test_reminder_poll_seconds_fails_fast(tmp_path, bad, needle):
    r = _run_import(tmp_path, "scheduler", {"COREOS_REMINDER_POLL_SECONDS": bad})
    assert r.returncode != 0, f"expected failure for {bad!r}, got:\n{r.stdout}"
    assert "COREOS_REMINDER_POLL_SECONDS" in r.stderr and needle in r.stderr
