"""planning & schedule engine schema (Phase 4)

Revision ID: 0009_schedule_engine
Revises: 0008_drop_user_budgets
Create Date: 2026-09-02

schedule_events  (rebuilt - the table is empty in the real DB, zero data risk):
  + notes            TEXT
  + all_day          INTEGER NOT NULL DEFAULT 0
  + status           TEXT NOT NULL DEFAULT 'confirmed'   ('confirmed'|'cancelled')
  + source_type      TEXT NOT NULL DEFAULT 'coreos'      (integration-ready)
  + external_id      TEXT                                 (integration-ready)
  + recurrence       TEXT NOT NULL DEFAULT 'none'        ('none'|'daily'|'weekly'|'weekdays')
  + recurrence_until TEXT
  + related_task_id  TEXT  ->  tasks(id) ON DELETE SET NULL
  FK user_id -> users(id) ON DELETE CASCADE  (kept)

tasks  (additive ALTER - 2 real rows, backfilled):
  + notes         TEXT
  + planned_at    TEXT   ("when I plan to do it", distinct from due_date deadline)
  + completed_at  TEXT

ai_recommendations  (additive ALTER):
  + snooze_until  TEXT   (contextual "Later" / snooze lifecycle)

Indexes for the calendar/agenda query patterns:
  idx_schedule_events_user_status_start (user_id, status, start_time)
  idx_schedule_events_related_task      (related_task_id)
  idx_tasks_user_completed             (user_id, is_completed)

downgrade() rebuilds schedule_events back to its 0008-era shape and drops the
added columns/indexes. Historical migrations are untouched.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0009_schedule_engine"
down_revision: Union[str, Sequence[str], None] = "0008_drop_user_budgets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _cols(bind, table):
    return {row[1] for row in bind.exec_driver_sql(f"PRAGMA table_info({table})")}


def _add(bind, table, col, ddl):
    if col not in _cols(bind, table):
        op.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


_SE_NEW = """
    id TEXT PRIMARY KEY,
    user_id TEXT,
    title TEXT,
    location TEXT,
    start_time TEXT,
    end_time TEXT,
    notes TEXT,
    all_day INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'confirmed',
    source_type TEXT NOT NULL DEFAULT 'coreos',
    external_id TEXT,
    recurrence TEXT NOT NULL DEFAULT 'none',
    recurrence_until TEXT,
    related_task_id TEXT,
    created_at TEXT,
    updated_at TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY(related_task_id) REFERENCES tasks(id) ON DELETE SET NULL
"""

_SE_OLD = """
    id TEXT PRIMARY KEY,
    user_id TEXT,
    title TEXT,
    location TEXT,
    start_time TEXT,
    end_time TEXT,
    created_at TEXT,
    updated_at TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
"""


def _rebuild_schedule_events(bind, body: str):
    old_cols = [r[1] for r in bind.exec_driver_sql("PRAGMA table_info(schedule_events)")]
    new_cols = [ln.strip().split()[0] for ln in body.strip().splitlines()
                if ln.strip() and not ln.strip().upper().startswith("FOREIGN")]
    shared = ", ".join(c for c in new_cols if c in old_cols)
    op.execute("ALTER TABLE schedule_events RENAME TO schedule_events__old")
    op.execute(f"CREATE TABLE schedule_events (\n{body}\n)")
    op.execute(f"INSERT INTO schedule_events ({shared}) SELECT {shared} FROM schedule_events__old")
    op.execute("DROP TABLE schedule_events__old")


def upgrade() -> None:
    bind = op.get_bind()

    _rebuild_schedule_events(bind, _SE_NEW)

    _add(bind, "tasks", "notes", "notes TEXT")
    _add(bind, "tasks", "planned_at", "planned_at TEXT")
    _add(bind, "tasks", "completed_at", "completed_at TEXT")

    _add(bind, "ai_recommendations", "snooze_until", "snooze_until TEXT")

    op.execute("CREATE INDEX IF NOT EXISTS idx_schedule_events_user_status_start "
               "ON schedule_events(user_id, status, start_time)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_schedule_events_related_task "
               "ON schedule_events(related_task_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_tasks_user_completed "
               "ON tasks(user_id, is_completed)")


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("DROP INDEX IF EXISTS idx_tasks_user_completed")
    op.execute("DROP INDEX IF EXISTS idx_schedule_events_related_task")
    op.execute("DROP INDEX IF EXISTS idx_schedule_events_user_status_start")

    _rebuild_schedule_events(bind, _SE_OLD)

    for col in ("completed_at", "planned_at", "notes"):
        try:
            op.execute(f"ALTER TABLE tasks DROP COLUMN {col}")
        except Exception:
            pass
    try:
        op.execute("ALTER TABLE ai_recommendations DROP COLUMN snooze_until")
    except Exception:
        pass
