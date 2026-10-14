"""Per-occurrence conflict decisions for recurring events (B6)

Revision ID: 0016_recurring_conflict_decisions
Revises: 0015_weather_conflicts
Create Date: 2026-09-10

Additive, backward-safe. Single new head after 0015.

MOTIVATION
  Before B6 the recurrence model was *virtual expansion only*: one
  `schedule_events` row per series, materialised on the fly by
  `schedule_core.expand_event`. There was no per-occurrence storage and the only
  mutation path (`_exec_event_update`) rewrites the SERIES row. B3 and B5 both
  deliberately skipped recurring events because "resolve this overlap" / "move
  this outdoor occurrence" could not touch a single occurrence without also
  moving every other one.

  B6 adds the RFC 5545 RECURRENCE-ID pattern as a NEW ADDITIVE table:
  `schedule_occurrence_overrides`. The series row is never mutated for a
  single-occurrence resolution. `expand_event` consults the overrides and
  substitutes the new start/end for exactly one occurrence (keyed by its
  canonical MYT recurrence date). Nothing here converts a series to a one-off,
  deletes the recurrence rule, or duplicates occurrences.

1. schedule_occurrence_overrides — one row per occurrence that was moved out of
   its default recurring slot by an approved conflict resolution.
     id PK              ("soo_"+uuid4hex)
     user_id NOT NULL
     series_event_id NOT NULL   the recurring schedule_events row
     occurrence_date NOT NULL   MYT 'YYYY-MM-DD' of the occurrence's DEFAULT slot
                                — the canonical RECURRENCE-ID / stable key
     start_time NOT NULL        the moved occurrence's new start (MYT ISO)
     end_time  NOT NULL         the moved occurrence's new end   (MYT ISO)
     status NOT NULL            'moved'  (only value B6 writes; 'skipped'
                                reserved for a future per-occurrence delete)
     origin NOT NULL            'overlap' | 'weather'  (which decision surface
                                produced it — informational)
     exec_token NOT NULL        idempotency echo of the decision that wrote it
     created_at NOT NULL, updated_at NOT NULL
   UNIQUE(user_id, series_event_id, occurrence_date)
     -> at most ONE exception per occurrence; also the exactly-once gate for an
        occurrence move.
   INDEX (user_id, series_event_id).

2. schedule_conflict_decisions + occurrence_date TEXT NULL
   Informational for non-recurring decisions (stays NULL); for a recurring
   occurrence it records which concrete occurrence the decision belongs to so
   the lazy stale-stamp can re-expand precisely. The decision identity itself
   is still the fingerprint (now occurrence-aware for recurring instances).

3. weather_decisions + occurrence_date TEXT NULL          (same rationale)
4. weather_move_proposals + occurrence_date TEXT NULL     (same rationale)

downgrade() drops the table + the three columns (SQLite 3.35+ DROP COLUMN,
best-effort).
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0016_recurring_conflict_decisions"
down_revision: Union[str, Sequence[str], None] = "0015_weather_conflicts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _cols(bind, table):
    return {row[1] for row in bind.exec_driver_sql(f"PRAGMA table_info({table})")}


def upgrade() -> None:
    bind = op.get_bind()

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS schedule_occurrence_overrides (
            id               TEXT PRIMARY KEY,
            user_id          TEXT NOT NULL,
            series_event_id  TEXT NOT NULL,
            occurrence_date  TEXT NOT NULL,
            start_time       TEXT NOT NULL,
            end_time         TEXT NOT NULL,
            status           TEXT NOT NULL DEFAULT 'moved',
            origin           TEXT NOT NULL,
            exec_token       TEXT NOT NULL,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_occ_overrides_user_series_date "
        "ON schedule_occurrence_overrides(user_id, series_event_id, occurrence_date)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_occ_overrides_user_series "
        "ON schedule_occurrence_overrides(user_id, series_event_id)"
    )

    if "occurrence_date" not in _cols(bind, "schedule_conflict_decisions"):
        op.execute("ALTER TABLE schedule_conflict_decisions ADD COLUMN occurrence_date TEXT")
    if "occurrence_date" not in _cols(bind, "weather_decisions"):
        op.execute("ALTER TABLE weather_decisions ADD COLUMN occurrence_date TEXT")
    if "occurrence_date" not in _cols(bind, "weather_move_proposals"):
        op.execute("ALTER TABLE weather_move_proposals ADD COLUMN occurrence_date TEXT")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_occ_overrides_user_series")
    op.execute("DROP INDEX IF EXISTS idx_occ_overrides_user_series_date")
    op.execute("DROP TABLE IF EXISTS schedule_occurrence_overrides")
    bind = op.get_bind()
    for tbl in ("schedule_conflict_decisions", "weather_decisions", "weather_move_proposals"):
        if "occurrence_date" in _cols(bind, tbl):
            try:
                op.execute(f"ALTER TABLE {tbl} DROP COLUMN occurrence_date")
            except Exception:
                pass
