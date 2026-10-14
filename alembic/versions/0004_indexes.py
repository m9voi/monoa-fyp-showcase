"""add indexes for the real query patterns

Revision ID: 0004_indexes
Revises: 0003_timestamps
Create Date: 2026-09-01

Every index below maps to a concrete query in main.py. No speculative
indexes were added.

  idx_transactions_user_date
      -> get_finance_snapshot / get_dashboard_data / get_spending_summary
         all filter  WHERE user_id = ? AND date [LIKE|>=] ?
  idx_transactions_user_type
      -> get_finance_snapshot sums  WHERE user_id = ? AND type = 'expense'|'income'
  idx_tasks_user
      -> get_schedule:  SELECT ... FROM tasks WHERE user_id = ?
  idx_tasks_reminder_scan
      -> reminder scheduler:  WHERE reminder_status = 'PENDING'
                              AND reminder_at <= ? AND is_completed = 0
  idx_schedule_events_user_start
      -> get_schedule + get_dashboard_data:
         WHERE user_id = ? [AND start_time LIKE ?] ORDER BY start_time
  idx_ai_recommendations_user_module_status
      -> get_schedule / get_finance_data / get_activity_data:
         WHERE user_id = ? AND module = ? AND status = 'PENDING'
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0004_indexes"
down_revision: Union[str, Sequence[str], None] = "0003_timestamps"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEXES = [
    ("idx_transactions_user_date", "transactions(user_id, date)"),
    ("idx_transactions_user_type", "transactions(user_id, type)"),
    ("idx_tasks_user", "tasks(user_id)"),
    ("idx_tasks_reminder_scan", "tasks(reminder_status, reminder_at)"),
    ("idx_schedule_events_user_start", "schedule_events(user_id, start_time)"),
    ("idx_ai_recommendations_user_module_status",
     "ai_recommendations(user_id, module, status)"),
]


def upgrade() -> None:
    for name, target in INDEXES:
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {target}")


def downgrade() -> None:
    for name, _ in INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
