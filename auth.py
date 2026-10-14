"""Phase 2 authentication core.

- Argon2id password hashing (argon2-cffi) — no custom crypto.
- Opaque random session tokens; only an HMAC-SHA256 hash of the token is
  stored server-side, so a DB leak cannot be replayed as a live session.
- Small per-process rate limiter for the auth endpoints (documented baseline;
  production needs a shared store — see README / report).

This module is transport-agnostic: it does not touch cookies or FastAPI.
main.py wires the cookie <-> session-token binding.
"""

import hmac
import hashlib
import secrets
import datetime
import threading
import time
from collections import deque

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
from email_validator import validate_email, EmailNotValidError

import db as db_layer
import security_config as cfg

_ph = PasswordHasher()  # argon2id defaults (sane, modern)

_TOKEN_BYTES = 32  # 256-bit session token


# --------------------------------------------------------------------------- #
# passwords
# --------------------------------------------------------------------------- #
def hash_password(plain: str) -> str:
    return _ph.hash(plain)


def verify_password(stored_hash: str, plain: str) -> bool:
    if not stored_hash:
        return False
    try:
        _ph.verify(stored_hash, plain)
        return True
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _ph.check_needs_rehash(stored_hash)
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# email
# --------------------------------------------------------------------------- #
def normalize_and_validate_email(raw: str) -> str:
    """Return the normalized email or raise ValueError with a safe message."""
    try:
        info = validate_email(raw, check_deliverability=False)
    except EmailNotValidError:
        raise ValueError("Please enter a valid email address.")
    return info.normalized.lower()


def validate_password(plain: str) -> None:
    if plain is None or len(plain) < cfg.PASSWORD_MIN_LENGTH:
        raise ValueError(f"Password must be at least {cfg.PASSWORD_MIN_LENGTH} characters.")
    if len(plain) > cfg.PASSWORD_MAX_LENGTH:
        raise ValueError(f"Password must be at most {cfg.PASSWORD_MAX_LENGTH} characters.")
    if not plain.strip():
        raise ValueError("Password cannot be blank.")


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #
def _hash_token(raw_token: str) -> str:
    return hmac.new(cfg.SESSION_SECRET.encode(), raw_token.encode(), hashlib.sha256).hexdigest()


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt) -> str:
    return dt.astimezone().isoformat()


def create_session(user_id: str, user_agent: str | None = None) -> tuple[str, str]:
    """Create a session row. Returns (raw_token, expires_at_iso).
    Only the token hash is persisted."""
    raw_token = secrets.token_urlsafe(_TOKEN_BYTES)
    sid = secrets.token_hex(16)
    now = _now()
    expires = now + datetime.timedelta(seconds=cfg.SESSION_TTL_SECONDS)
    with db_layer.transaction() as conn:
        conn.execute(
            """INSERT INTO sessions
               (id, user_id, token_hash, created_at, expires_at, last_used_at, user_agent)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (sid, user_id, _hash_token(raw_token), _iso(now), _iso(expires),
             _iso(now), (user_agent or "")[:255]),
        )
    return raw_token, _iso(expires)


def resolve_session(raw_token: str) -> dict | None:
    """Return the user dict for a valid, unexpired session, else None.
    Also lazily deletes the row if expired."""
    if not raw_token:
        return None
    token_hash = _hash_token(raw_token)
    with db_layer.transaction() as conn:
        row = conn.execute(
            """SELECT s.id AS sid, s.expires_at AS expires_at,
                      u.id AS id, u.email AS email, u.name AS name, u.is_active AS is_active
                 FROM sessions s JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = ?""",
            (token_hash,),
        ).fetchone()
        if row is None:
            return None
        if not row["is_active"]:
            return None
        try:
            expires = datetime.datetime.fromisoformat(row["expires_at"])
        except Exception:
            expires = _now() - datetime.timedelta(seconds=1)
        if expires <= _now():
            conn.execute("DELETE FROM sessions WHERE id = ?", (row["sid"],))
            return None
        conn.execute("UPDATE sessions SET last_used_at = ? WHERE id = ?",
                     (_iso(_now()), row["sid"]))
        return {"id": row["id"], "email": row["email"], "name": row["name"]}


def revoke_session(raw_token: str) -> None:
    if not raw_token:
        return
    with db_layer.transaction() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(raw_token),))


def revoke_all_sessions(user_id: str) -> None:
    with db_layer.transaction() as conn:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def purge_expired_sessions() -> int:
    with db_layer.transaction() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (_iso(_now()),))
        return cur.rowcount or 0


# --------------------------------------------------------------------------- #
# rate limiting (per-process baseline)
# --------------------------------------------------------------------------- #
# ONE in-memory sliding-window store shared by the auth limiter and the Phase
# 5H-B2 provider-cost limiter. Keys are opaque, caller-namespaced strings
# (e.g. "login:<ip>", "rl:places_search:<user_id>"); this module never inspects
# them and they must never contain a secret, a raw coordinate or free-text.
# Single-process only — a shared store for multi-worker production is a
# release-readiness item (see the Phase 2 / 5H reports).
_attempts: dict[str, deque] = {}
_rl_lock = threading.Lock()

# Opportunistic housekeeping so the store stays genuinely bounded even without a
# background task: every _RL_SWEEP_EVERY hits, drop buckets idle longer than
# _RL_STALE_AFTER_S; and never let the store exceed cfg.RL_MAX_BUCKETS.
_RL_SWEEP_EVERY = 1000
_RL_STALE_AFTER_S = 3600
_rl_hits_since_sweep = 0


def _rl_sweep(now: float) -> None:
    stale = [k for k, dq in _attempts.items()
             if not dq or dq[-1] <= now - _RL_STALE_AFTER_S]
    for k in stale:
        _attempts.pop(k, None)


def rate_limit_hit(key: str, max_calls: int, window_s: int) -> tuple[bool, int]:
    """Generic sliding-window limiter.

    Records one call against `key` and returns ``(allowed, retry_after_s)``.
    ``retry_after_s`` is 0 when allowed, otherwise a deterministic whole-second
    estimate of when the oldest in-window call expires (always >= 1).

    Thread-safe (a lock guards the multi-step read-modify-write). Empty / idle
    buckets are swept; the store is hard-capped at cfg.RL_MAX_BUCKETS with
    least-recently-active eviction.
    """
    global _rl_hits_since_sweep
    if max_calls <= 0:
        return False, max(int(window_s), 1)
    now = time.monotonic()
    with _rl_lock:
        _rl_hits_since_sweep += 1
        if _rl_hits_since_sweep >= _RL_SWEEP_EVERY:
            _rl_hits_since_sweep = 0
            _rl_sweep(now)

        dq = _attempts.get(key)
        if dq is None:
            cap = getattr(cfg, "RL_MAX_BUCKETS", 50000)
            if len(_attempts) >= cap:
                oldest = min(_attempts, key=lambda k: _attempts[k][-1] if _attempts[k] else 0.0)
                _attempts.pop(oldest, None)
            dq = deque()
            _attempts[key] = dq

        while dq and dq[0] <= now - window_s:
            dq.popleft()

        if len(dq) >= max_calls:
            retry = int(dq[0] + window_s - now) + 1
            return False, max(retry, 1)
        dq.append(now)
        return True, 0


def rate_limit_forget(key: str) -> None:
    with _rl_lock:
        _attempts.pop(key, None)


def rate_limit_check(key: str) -> bool:
    """Auth-endpoint limiter (register / login). Thin wrapper over
    rate_limit_hit using the auth window/ceiling. Signature unchanged."""
    allowed, _ = rate_limit_hit(key, cfg.AUTH_RATE_MAX_ATTEMPTS, cfg.AUTH_RATE_WINDOW_SECONDS)
    return allowed


def rate_limit_reset(key: str) -> None:
    rate_limit_forget(key)
