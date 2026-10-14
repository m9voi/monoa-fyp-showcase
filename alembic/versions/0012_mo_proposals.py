"""Mo human-in-the-loop proposals (B2)

Revision ID: 0012_mo_proposals
Revises: 0011_create_idempotency
Create Date: 2026-09-10

Additive, backward-safe. Creates ONE new table, `mo_proposals`, and touches no
existing table or row. `POST /api/process-input` no longer mutates domain data;
a supported mutating request produces a server-owned pending proposal row here,
and a later explicit confirm executes it exactly once through the SAME canonical
services Plan / Wallet use.

mo_proposals
  id                   TEXT  PK   "prop_" + uuid4 hex
  user_id              TEXT  NOT NULL  owner — ALWAYS from the session, never the client
  status               TEXT  NOT NULL  pending | executing | executed | cancelled | expired | failed
  action_type          TEXT  NOT NULL  create_transaction | create_task | create_event
  result_kind          TEXT            transaction | task | event   (set on execution)
  payload_json         TEXT  NOT NULL  server-VALIDATED executable fields (never client-authored)
  summary              TEXT  NOT NULL  Mo's concise human answer
  effect               TEXT  NOT NULL  one-line "exactly what will happen"
  display_json         TEXT  NOT NULL  safe display fields for the UI (amount/category/title/date...)
  client_token         TEXT            the submission's opaque token (nullable)
  client_request_hash  TEXT            fingerprint of the submission TEXT (token-reuse-with-different-input -> 409)
  exec_token           TEXT  NOT NULL  the token handed to the canonical service on confirm (exactly-once key)
  result_ref           TEXT            domain row id created on execution
  exec_message         TEXT            the canonical service's success line (shown on replay)
  failure_reason       TEXT            truthful, non-sensitive
  created_at           TEXT  NOT NULL
  expires_at           TEXT  NOT NULL  pending proposals lapse after this (lazily enforced)
  confirmed_at         TEXT            when execution completed

Indexes
  idx_mo_proposals_user_status   (user_id, status)                          — "my pending proposal" lookup
  idx_mo_proposals_user_token    UNIQUE (user_id, client_token)
                                 WHERE client_token IS NOT NULL             — one proposal per (user, token)

downgrade() drops the table. No existing data is affected in either direction.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0012_mo_proposals"
down_revision: Union[str, Sequence[str], None] = "0011_create_idempotency"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS mo_proposals (
            id                  TEXT PRIMARY KEY,
            user_id             TEXT NOT NULL,
            status              TEXT NOT NULL,
            action_type         TEXT NOT NULL,
            result_kind         TEXT,
            payload_json        TEXT NOT NULL,
            summary             TEXT NOT NULL,
            effect              TEXT NOT NULL,
            display_json        TEXT NOT NULL,
            client_token        TEXT,
            client_request_hash TEXT,
            exec_token          TEXT NOT NULL,
            result_ref          TEXT,
            exec_message        TEXT,
            failure_reason      TEXT,
            created_at          TEXT NOT NULL,
            expires_at          TEXT NOT NULL,
            confirmed_at        TEXT
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_mo_proposals_user_status "
        "ON mo_proposals(user_id, status)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_mo_proposals_user_token "
        "ON mo_proposals(user_id, client_token) WHERE client_token IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_mo_proposals_user_token")
    op.execute("DROP INDEX IF EXISTS idx_mo_proposals_user_status")
    op.execute("DROP TABLE IF EXISTS mo_proposals")
