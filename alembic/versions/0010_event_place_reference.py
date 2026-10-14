"""event <-> real place reference (Phase 5G: Map <-> Schedule)

Revision ID: 0010_event_place_reference
Revises: 0009_schedule_engine
Create Date: 2026-09-03

Additive, backward-safe. Existing schedule_events rows stay valid — every new
column is NULLABLE and an event with no linked place simply leaves them NULL.

schedule_events  (additive ALTER — SQLite adds a nullable column without a table
rebuild, so there is zero data risk):
  + place_provider     TEXT   ('google' | 'osm' | future)   provider-neutral
  + place_provider_id  TEXT   the provider's stable id (e.g. 'ChIJ...', 'node:1')
  + place_lat          REAL   snapshot latitude
  + place_lng          REAL   snapshot longitude
  + place_address      TEXT   snapshot formatted address (rarely changes)
  + client_token       TEXT   idempotency key for POST /api/events — a retried
                              create with the same (user_id, client_token) returns
                              the existing event instead of a duplicate.

The existing `location TEXT` column continues to hold the human place NAME.

Only STABLE identity + a small snapshot are stored. Volatile provider facts
(rating, open_now, today's hours, business status) are deliberately NOT persisted
— they are re-fetched from the provider when a linked place is viewed.

Index:
  idx_schedule_events_user_client_token  UNIQUE (user_id, client_token)
    WHERE client_token IS NOT NULL   -> enforces idempotency at the DB level.

downgrade() drops the columns (SQLite 3.35+ DROP COLUMN) and the index. It is
best-effort so it cannot fail a rollback on an older SQLite.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0010_event_place_reference"
down_revision: Union[str, Sequence[str], None] = "0009_schedule_engine"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _cols(bind, table):
    return {row[1] for row in bind.exec_driver_sql(f"PRAGMA table_info({table})")}


def _add(bind, table, col, ddl):
    if col not in _cols(bind, table):
        op.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


_NEW_COLS = (
    ("place_provider", "place_provider TEXT"),
    ("place_provider_id", "place_provider_id TEXT"),
    ("place_lat", "place_lat REAL"),
    ("place_lng", "place_lng REAL"),
    ("place_address", "place_address TEXT"),
    ("client_token", "client_token TEXT"),
)


def upgrade() -> None:
    bind = op.get_bind()
    for col, ddl in _NEW_COLS:
        _add(bind, "schedule_events", col, ddl)
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_schedule_events_user_client_token "
        "ON schedule_events(user_id, client_token) WHERE client_token IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_schedule_events_user_client_token")
    for col, _ddl in reversed(_NEW_COLS):
        try:
            op.execute(f"ALTER TABLE schedule_events DROP COLUMN {col}")
        except Exception:
            pass
