"""Mo short-term conversational context (final correctness patch)

Revision ID: 0018_mo_context
Revises: 0017_task_event_conflicts
Create Date: 2026-09-15

Additive, backward-safe. Creates ONE new table, `mo_context`, and touches no
existing table or row.

MOTIVATION
  Mo's multi-operation closure (B9) derived its short-term "what day are we
  talking about" hint by re-reading the caller's most recent mo_proposals row.
  That only works when the PREVIOUS turn happened to create a proposal. A pure
  discussion turn ("Let's look at Monday") answers a question and creates no
  proposal row at all, so the very next turn ("Move gym from 8 to 7 PM") had
  no way to recover which day "gym" meant. mo_context fixes that by recording
  the resolved date after EVERY understood turn that produces one (read or
  proposal alike), not only proposal-creating ones.

mo_context
  user_id        TEXT  PK    owner — ALWAYS from the session, never the client
  context_date   TEXT  NOT NULL   YYYY-MM-DD, the most recently discussed day
  updated_at     TEXT  NOT NULL   for the existing 10-minute bounded-recency rule

Exactly one row per user (upserted in place), so this can never grow
unbounded. A HINT ONLY: it never decides which database row Mo acts on —
every target resolver still requires a real, structural, deterministic DB
match before anything is proposed (see _mo_recent_context_date /
_mo_resolve_task_target / _mo_resolve_event_target in main.py).

downgrade() drops the table. No existing data is affected in either direction.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0018_mo_context"
down_revision: Union[str, Sequence[str], None] = "0017_task_event_conflicts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS mo_context (
            user_id       TEXT PRIMARY KEY,
            context_date  TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS mo_context")
