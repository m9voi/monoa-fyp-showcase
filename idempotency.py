"""CoreOS — shared create-once idempotency (Phase 5H-B3).

A user-intended persistent CREATE (a transaction, a task, a schedule event) must
be applied AT MOST ONCE when the official CoreOS client retries the same logical
operation after a slow / lost response.

Mechanism (same as the Phase 5G Map->Schedule bridge, generalised):
  * the client sends one opaque `client_token` per logical form submission and
    reuses it verbatim on every retry of that submission;
  * the DATABASE is the authority — each idempotent table carries a nullable
    `client_token` column plus a partial unique index
    ``(user_id, client_token) WHERE client_token IS NOT NULL``;
  * a `client_request_hash` column stores a fingerprint of the *normalized
    persistent create intent*, so a retry that reuses a token with a MATERIALLY
    DIFFERENT payload is a truthful HTTP 409 conflict — never a silent no-op and
    never an overwrite.

This module is pure (no DB, no FastAPI). Token/hash values are opaque and carry
no transaction / task / event data.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional

# Opaque, URL-safe-ish. Accepts crypto.randomUUID() (36 chars, hex + hyphens),
# the "ct-<ts>-<rand>" fallback, and the short tokens used by the Phase 5G tests.
# Rejects control characters, whitespace, over-length and non-string values so a
# malformed token fails fast (HTTP 422) instead of being silently truncated.
TOKEN_MAX = 64
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~:\-]{1,%d}$" % TOKEN_MAX)


def normalize_token(raw: Any) -> Optional[str]:
    """Return a clean token string, or ``None`` when the client sent nothing.

    Raises ``ValueError`` (message safe for a 422 detail) on a present-but-
    malformed token. Never truncates.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("client_token must be a string.")
    s = raw.strip()
    if s == "":
        return None
    if not _TOKEN_RE.match(s):
        raise ValueError(
            f"client_token must be 1-{TOKEN_MAX} characters from [A-Za-z0-9._~:-]."
        )
    return s


def request_fingerprint(fields: dict) -> str:
    """Stable, opaque hash of the NORMALIZED persistent create intent.

    `fields` must be the already-normalized values that will be written (never
    raw request keys or volatile UI fields, and never a server-generated
    timestamp). Canonical JSON -> blake2s. The digest carries no readable
    payload data.
    """
    canon = json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.blake2s(canon.encode("utf-8"), digest_size=16).hexdigest()


class IdempotencyConflict(Exception):
    """A `client_token` was reused for a materially different create payload.

    Carries no payload detail — the HTTP layer maps it to a fixed 409 message
    that never echoes the existing row.
    """
