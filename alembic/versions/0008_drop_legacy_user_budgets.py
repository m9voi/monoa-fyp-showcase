"""drop the dead user_budgets table

Revision ID: 0008_drop_user_budgets
Revises: 0007_finance_precision
Create Date: 2026-09-02

`user_budgets` was a second budget store. Since Phase 3, budgets live ONLY in
`user_financial_settings`:
  * no runtime code reads user_budgets,
  * no runtime code writes user_budgets (the last writer, save_finance_settings,
    was removed in Phase 3),
  * its contents are a strict subset of user_financial_settings (daily/weekly/
    monthly), which additionally holds starting_balance.

Historical migrations 0001 (create) and 0005 (FK rebuild) are left untouched.

downgrade() recreates the table with its post-0005 structure (PK + FK to users
+ NOT NULL/DEFAULT 0 budget columns) and backfills it from
user_financial_settings, so a downgrade restores an equivalent, populated table.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0008_drop_user_budgets"
down_revision: Union[str, Sequence[str], None] = "0007_finance_precision"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS user_budgets")


def downgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_budgets (
            user_id TEXT PRIMARY KEY,
            monthly_budget REAL NOT NULL DEFAULT 0,
            weekly_budget REAL NOT NULL DEFAULT 0,
            daily_budget REAL NOT NULL DEFAULT 0,
            updated_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        INSERT OR IGNORE INTO user_budgets
            (user_id, monthly_budget, weekly_budget, daily_budget, updated_at)
        SELECT user_id, monthly_budget, weekly_budget, daily_budget, updated_at
        FROM user_financial_settings
        """
    )
