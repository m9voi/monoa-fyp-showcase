"""Task <-> event schedule conflicts (B7)

Revision ID: 0017_task_event_conflicts
Revises: 0016_recurring_conflict_decisions
Create Date: 2026-09-11

Additive, backward-safe. Single new head after 0016.

MOTIVATION
  Before B7 a task carried three *point* timestamps: due_date (a deadline),
  planned_at ("when I plan to do it" — one instant, no end), and reminder_at
  (a notification instant). None of these is a real work window, so a task
  could not participate in a [start, end) schedule overlap.

  B7 adds the SMALLEST truthful additive model: an optional task END. A task has
  a real work window ONLY when BOTH planned_at AND scheduled_end are set, both
  are datetimes, and scheduled_end > planned_at. No duration is ever inferred; a
  planned_at with no scheduled_end stays a point marker exactly as before, and a
  due_date / reminder_at alone is never a time-block conflict.

  Reminders are notification-offset instants, not commitments occupying a window
  (verified against the schema + scheduler.py, which only flips
  reminder_status PENDING->DUE and delivers nothing), so reminder-overlap
  warnings are deliberately NOT modelled.

1. tasks + scheduled_end TEXT NULL
   The end of the optional task work window. Paired with the existing
   planned_at (the window start). NULL for every existing row and for any task
   the user does not explicitly schedule as a block.

2. task_event_conflict_decisions — the owner's recorded choice for ONE
   task-window / event-occurrence overlap. Kept SEPARATE from
   schedule_conflict_decisions so B3/B6 event-pair fingerprints are never
   reinterpreted.
     id PK ("ted_"+uuid4hex), user_id NOT NULL,
     fingerprint NOT NULL     stable identity: task id + task window +
                              event id + concrete occurrence date + event
                              occurrence window. A material task/event time
                              edit changes it -> the old decision no longer
                              matches and the (now different) overlap resurfaces.
     task_id NOT NULL, event_id NOT NULL,
     occurrence_date TEXT     MYT 'YYYY-MM-DD' of the recurring event
                              occurrence, or NULL for a one-off event.
     decision NOT NULL        'kept' | 'moved'
     original_json TEXT       task window + event window at decision time
     proposal_json TEXT       the candidate that was offered (moved only)
     applied_json TEXT        {task_id, start, end} actually written (moved only)
     exec_token NOT NULL      idempotency echo for the canonical task update
     created_at NOT NULL, decided_at NOT NULL, stale_at TEXT
   UNIQUE(user_id, fingerprint)   -> one decision per overlap + exactly-once
   INDEX(user_id, task_id).

3. task_move_proposals — transient server-owned candidate windows for
   "Find another time". The browser never authors the final times; it confirms
   by (proposal_id, candidate_index).
     id PK ("tmp_"+uuid4hex), user_id NOT NULL, fingerprint NOT NULL,
     task_id NOT NULL, event_id NOT NULL, occurrence_date TEXT,
     status NOT NULL          pending | confirmed | cancelled | expired
     candidates_json NOT NULL server-generated [{start,end,summary}]
     chosen_index INTEGER,
     task_window_json NOT NULL  the task window the candidates were computed for
     exec_token NOT NULL,
     created_at NOT NULL, expires_at NOT NULL, confirmed_at TEXT
   INDEX(user_id, status).

downgrade() drops both tables + the tasks.scheduled_end column.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0017_task_event_conflicts"
down_revision: Union[str, Sequence[str], None] = "0016_recurring_conflict_decisions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _cols(bind, table):
    return {row[1] for row in bind.exec_driver_sql(f"PRAGMA table_info({table})")}


def upgrade() -> None:
    bind = op.get_bind()

    if "scheduled_end" not in _cols(bind, "tasks"):
        op.execute("ALTER TABLE tasks ADD COLUMN scheduled_end TEXT")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS task_event_conflict_decisions (
            id               TEXT PRIMARY KEY,
            user_id          TEXT NOT NULL,
            fingerprint      TEXT NOT NULL,
            task_id          TEXT NOT NULL,
            event_id         TEXT NOT NULL,
            occurrence_date  TEXT,
            decision         TEXT NOT NULL,
            original_json    TEXT,
            proposal_json    TEXT,
            applied_json     TEXT,
            exec_token       TEXT NOT NULL,
            created_at       TEXT NOT NULL,
            decided_at       TEXT NOT NULL,
            stale_at         TEXT
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_ted_user_fp "
        "ON task_event_conflict_decisions(user_id, fingerprint)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_ted_user_task "
        "ON task_event_conflict_decisions(user_id, task_id)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS task_move_proposals (
            id               TEXT PRIMARY KEY,
            user_id          TEXT NOT NULL,
            fingerprint      TEXT NOT NULL,
            task_id          TEXT NOT NULL,
            event_id         TEXT NOT NULL,
            occurrence_date  TEXT,
            status           TEXT NOT NULL,
            candidates_json  TEXT NOT NULL,
            chosen_index     INTEGER,
            task_window_json TEXT NOT NULL,
            exec_token       TEXT NOT NULL,
            created_at       TEXT NOT NULL,
            expires_at       TEXT NOT NULL,
            confirmed_at     TEXT
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_tmp_user_status "
        "ON task_move_proposals(user_id, status)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_tmp_user_status")
    op.execute("DROP TABLE IF EXISTS task_move_proposals")
    op.execute("DROP INDEX IF EXISTS idx_ted_user_task")
    op.execute("DROP INDEX IF EXISTS idx_ted_user_fp")
    op.execute("DROP TABLE IF EXISTS task_event_conflict_decisions")
    bind = op.get_bind()
    if "scheduled_end" in _cols(bind, "tasks"):
        try:
            op.execute("ALTER TABLE tasks DROP COLUMN scheduled_end")
        except Exception:
            pass
