"""Plan conflict decisions (B3)

Revision ID: 0013_schedule_conflict_decisions
Revises: 0012_mo_proposals
Create Date: 2026-09-10

Additive, backward-safe. Creates ONE new table, `schedule_conflict_decisions`,
and touches no existing table or row. A detected schedule overlap can now be
resolved by the owner: ACCEPT applies the exact server-generated reschedule
through the SAME canonical event-update path manual Plan edits use; KEEP records
that the user chose to keep both items so the unchanged overlap stops resurfacing
as unresolved. Every decision is scoped to one authenticated owner and one
deterministic conflict fingerprint; if the relevant event time / all-day flag /
location changes, the fingerprint no longer matches and the decision is lazily
marked stale so the (now different) overlap is recomputed truthfully on reload.

schedule_conflict_decisions
  id             TEXT  PK   "scd_" + uuid4 hex
  user_id        TEXT  NOT NULL  owner — ALWAYS from the session, never the client
  fingerprint    TEXT  NOT NULL  blake2s of the conflict identity + decision-critical
                                 fields of BOTH events (sorted id, start, end,
                                 all_day, location). Deterministic for unchanged data.
  conflict_type  TEXT  NOT NULL  'event_overlap'
  event_a_id     TEXT  NOT NULL  the two events in the overlap (sorted)
  event_b_id     TEXT  NOT NULL
  original_json  TEXT  NOT NULL  {a:{id,start,end,all_day,location}, b:{...}} at decision time
  proposal_json  TEXT            server-generated reschedule captured at decision time
                                 (NULL for a KEEP or a no-safe-resolution KEEP)
  decision       TEXT  NOT NULL  'accepted' | 'kept'
  applied_event_id TEXT          event row updated on ACCEPT (the later-starting one)
  applied_json   TEXT            {start,end} actually written on ACCEPT
  exec_token     TEXT  NOT NULL  idempotency key for the canonical event update
  created_at     TEXT  NOT NULL
  decided_at     TEXT  NOT NULL
  stale_at       TEXT            set lazily once the fingerprint no longer matches
                                 live event data (edited / deleted after the decision)

Indexes
  idx_sched_conflict_dec_user        (user_id, conflict_type)          — "my decisions" lookup
  idx_sched_conflict_dec_user_fp     UNIQUE (user_id, fingerprint)     — one decision per (user, conflict);
                                     also the exactly-once gate for ACCEPT under repeated clicks / retries

downgrade() drops the table. No existing data is affected in either direction.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0013_schedule_conflict_decisions"
down_revision: Union[str, Sequence[str], None] = "0012_mo_proposals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS schedule_conflict_decisions (
            id               TEXT PRIMARY KEY,
            user_id          TEXT NOT NULL,
            fingerprint      TEXT NOT NULL,
            conflict_type    TEXT NOT NULL,
            event_a_id       TEXT NOT NULL,
            event_b_id       TEXT NOT NULL,
            original_json    TEXT NOT NULL,
            proposal_json    TEXT,
            decision         TEXT NOT NULL,
            applied_event_id TEXT,
            applied_json     TEXT,
            exec_token       TEXT NOT NULL,
            created_at       TEXT NOT NULL,
            decided_at       TEXT NOT NULL,
            stale_at         TEXT
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_sched_conflict_dec_user "
        "ON schedule_conflict_decisions(user_id, conflict_type)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sched_conflict_dec_user_fp "
        "ON schedule_conflict_decisions(user_id, fingerprint)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_sched_conflict_dec_user_fp")
    op.execute("DROP INDEX IF EXISTS idx_sched_conflict_dec_user")
    op.execute("DROP TABLE IF EXISTS schedule_conflict_decisions")
