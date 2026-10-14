"""create-once idempotency for transactions / tasks / events (Phase 5H-B3)

Revision ID: 0011_create_idempotency
Revises: 0010_event_place_reference
Create Date: 2026-09-03

Additive, backward-safe. Every new column is NULLABLE; existing rows stay valid
with NULL and are never deduplicated (an operation with no client_token is never
idempotent). SQLite adds a nullable column without a table rebuild, so there is
zero data risk.

transactions  + client_token         TEXT   idempotency key for POST /api/transactions
              + client_request_hash  TEXT   fingerprint of the normalized create
                                            intent (for retry-with-different-payload
                                            -> HTTP 409, not a silent no-op)
tasks         + client_token         TEXT   idempotency key for POST /api/tasks
              + client_request_hash  TEXT

schedule_events + client_request_hash TEXT  (its client_token already exists from
                                            0010_event_place_reference)

Indexes (enforce "at most once per user+token" at the DB level, mirroring
0010's idx_schedule_events_user_client_token):
  idx_transactions_user_client_token  UNIQUE (user_id, client_token)
    WHERE client_token IS NOT NULL
  idx_tasks_user_client_token         UNIQUE (user_id, client_token)
    WHERE client_token IS NOT NULL

downgrade() drops the two new indexes and the new columns (SQLite 3.35+ DROP
COLUMN). It is best-effort so it cannot fail a rollback on an older SQLite.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0011_create_idempotency"
down_revision: Union[str, Sequence[str], None] = "0010_event_place_reference"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _cols(bind, table):
    return {row[1] for row in bind.exec_driver_sql(f"PRAGMA table_info({table})")}


def _add(bind, table, col, ddl):
    if col not in _cols(bind, table):
        op.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


# (table, column, DDL) — order matters for the reversed drop in downgrade().
_NEW_COLS = (
    ("transactions", "client_token", "client_token TEXT"),
    ("transactions", "client_request_hash", "client_request_hash TEXT"),
    ("tasks", "client_token", "client_token TEXT"),
    ("tasks", "client_request_hash", "client_request_hash TEXT"),
    ("schedule_events", "client_request_hash", "client_request_hash TEXT"),
)

_NEW_INDEXES = (
    ("idx_transactions_user_client_token",
     "CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_user_client_token "
     "ON transactions(user_id, client_token) WHERE client_token IS NOT NULL"),
    ("idx_tasks_user_client_token",
     "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_user_client_token "
     "ON tasks(user_id, client_token) WHERE client_token IS NOT NULL"),
)


def upgrade() -> None:
    bind = op.get_bind()
    for table, col, ddl in _NEW_COLS:
        _add(bind, table, col, ddl)
    for _name, ddl in _NEW_INDEXES:
        op.execute(ddl)


def downgrade() -> None:
    for name, _ddl in _NEW_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
    for table, col, _ddl in reversed(_NEW_COLS):
        try:
            op.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
        except Exception:
            pass
