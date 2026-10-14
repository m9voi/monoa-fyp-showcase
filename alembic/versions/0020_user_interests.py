"""Who Am I — persistent user interests (Product Completion Batch 02)

Revision ID: 0020_user_interests
Revises: 0019_mo_context_entity
Create Date: 2026-09-16

Additive, backward-safe. Creates ONE new table, `user_interests`, and touches
no existing table or row.

MOTIVATION
  "Who Am I?" must be a real, persistent product feature — not a decorative
  preferences screen. This table stores interests the user EXPLICITLY
  selected in the app (Games / Sports / Movies & TV / General), scoped per
  account, so they survive across sessions and devices and can be consumed
  as a soft personalization signal by Mo (see main.py: _get_user_interests,
  _mo_interests_context_line). This is deliberately NOT the same thing as
  mo_context (0018/0019) — mo_context is a short-term, 10-minute-bounded
  conversational hint that is wiped/ignored quickly; user_interests is
  durable, explicit, user-owned data with no expiry.

user_interests
  id           TEXT  PK    "int_" + uuid4 hex
  user_id      TEXT  NOT NULL   owner — ALWAYS from the session, never the client
  category     TEXT  NOT NULL   "game" | "sport" | "movie_tv" | "general"
  value        TEXT  NOT NULL   one interest label, from a bounded server-side
                                 allow-list per category (never free text)
  created_at   TEXT  NOT NULL

  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE

Indexes
  idx_user_interests_user             (user_id)                       — "all of my interests"
  idx_user_interests_user_category    (user_id, category)             — "my Games interests"
  idx_user_interests_unique           UNIQUE (user_id, category, value) — dedup guard

downgrade() drops the table. No existing data is affected in either direction.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0020_user_interests"
down_revision: Union[str, Sequence[str], None] = "0019_mo_context_entity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_interests (
            id          TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            category    TEXT NOT NULL,
            value       TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_interests_user "
        "ON user_interests(user_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_interests_user_category "
        "ON user_interests(user_id, category)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_interests_unique "
        "ON user_interests(user_id, category, value)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_user_interests_unique")
    op.execute("DROP INDEX IF EXISTS idx_user_interests_user_category")
    op.execute("DROP INDEX IF EXISTS idx_user_interests_user")
    op.execute("DROP TABLE IF EXISTS user_interests")
