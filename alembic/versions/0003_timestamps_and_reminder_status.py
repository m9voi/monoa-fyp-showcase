"""add created_at / updated_at and tasks.reminder_status

Revision ID: 0003_timestamps
Revises: 0002_drop_legacy
Create Date: 2026-09-01

Adds timezone-aware ISO-8601 audit timestamps to the mutable entities and
a dedicated reminder lifecycle column on tasks:

  transactions        + created_at, updated_at
  tasks               + created_at, updated_at, reminder_status
  schedule_events     + created_at, updated_at
  ai_recommendations  + created_at, updated_at

reminder_status values:
  'NONE'    - task has no reminder_at
  'PENDING' - reminder_at set and not yet reached / processed
  'DUE'     - the background scheduler has detected the reminder as due
  'DISMISSED' / 'DONE' - set when the task is dismissed or completed

Each ALTER is guarded by a PRAGMA table_info check so the migration is
idempotent even if main.init_db() already added a column at runtime.
Existing historical timestamps (transactions.date, tasks.due_date, ...)
are NOT modified; they are only *read* to backfill created_at.
"""
from typing import Sequence, Union
import datetime

from alembic import op

revision: str = "0003_timestamps"
down_revision: Union[str, Sequence[str], None] = "0002_drop_legacy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _cols(bind, table):
    return {row[1] for row in bind.exec_driver_sql(f"PRAGMA table_info({table})")}


def _add_col(bind, table, col, ddl):
    if col not in _cols(bind, table):
        op.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()

    # --- transactions ---
    _add_col(bind, "transactions", "created_at", "created_at TEXT")
    _add_col(bind, "transactions", "updated_at", "updated_at TEXT")
    op.execute(
        f"UPDATE transactions SET created_at = COALESCE(created_at, date, '{now}') "
        f"WHERE created_at IS NULL"
    )
    op.execute(
        "UPDATE transactions SET updated_at = COALESCE(updated_at, created_at) "
        "WHERE updated_at IS NULL"
    )

    # --- tasks ---
    _add_col(bind, "tasks", "created_at", "created_at TEXT")
    _add_col(bind, "tasks", "updated_at", "updated_at TEXT")
    _add_col(bind, "tasks", "reminder_status", "reminder_status TEXT")
    op.execute(
        f"UPDATE tasks SET created_at = COALESCE(created_at, due_date, reminder_at, '{now}') "
        f"WHERE created_at IS NULL"
    )
    op.execute(
        "UPDATE tasks SET updated_at = COALESCE(updated_at, created_at) "
        "WHERE updated_at IS NULL"
    )
    op.execute(
        "UPDATE tasks SET reminder_status = "
        "CASE WHEN reminder_at IS NULL OR reminder_at = '' THEN 'NONE' ELSE 'PENDING' END "
        "WHERE reminder_status IS NULL OR reminder_status = ''"
    )

    # --- schedule_events ---
    _add_col(bind, "schedule_events", "created_at", "created_at TEXT")
    _add_col(bind, "schedule_events", "updated_at", "updated_at TEXT")
    op.execute(
        f"UPDATE schedule_events SET created_at = COALESCE(created_at, start_time, '{now}') "
        f"WHERE created_at IS NULL"
    )
    op.execute(
        "UPDATE schedule_events SET updated_at = COALESCE(updated_at, created_at) "
        "WHERE updated_at IS NULL"
    )

    # --- ai_recommendations ---
    _add_col(bind, "ai_recommendations", "created_at", "created_at TEXT")
    _add_col(bind, "ai_recommendations", "updated_at", "updated_at TEXT")
    op.execute(
        f"UPDATE ai_recommendations SET created_at = COALESCE(created_at, '{now}') "
        f"WHERE created_at IS NULL"
    )
    op.execute(
        "UPDATE ai_recommendations SET updated_at = COALESCE(updated_at, created_at) "
        "WHERE updated_at IS NULL"
    )


def downgrade() -> None:
    # Drop the added columns. SQLite (3.35+) supports DROP COLUMN.
    for table, col in [
        ("transactions", "created_at"),
        ("transactions", "updated_at"),
        ("tasks", "created_at"),
        ("tasks", "updated_at"),
        ("tasks", "reminder_status"),
        ("schedule_events", "created_at"),
        ("schedule_events", "updated_at"),
        ("ai_recommendations", "created_at"),
        ("ai_recommendations", "updated_at"),
    ]:
        try:
            op.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
        except Exception:
            pass
