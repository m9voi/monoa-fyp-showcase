"""drop unused legacy tables: finances, schedule, user_memory

Revision ID: 0002_drop_legacy
Revises: 0001_baseline
Create Date: 2026-09-01

Evidence these are unused by the running application (checked project-wide):
  * finances    - only ever created by setup_db.py / reset_db.py; no
                  SELECT/INSERT/UPDATE anywhere in main.py; superseded by
                  `transactions`.
  * schedule    - same; superseded by `schedule_events`.
  * user_memory - created historically; no endpoint or query references it.

A timestamped backup of coreos.db is taken before this migration runs
(see backups/ and main.init_db / the Phase 1 rollout script).

downgrade() recreates the three tables (empty) with their original DDL so
the migration is reversible in structure. The original demo rows are NOT
restored on downgrade - they only exist in the pre-migration backup.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002_drop_legacy"
down_revision: Union[str, Sequence[str], None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS finances")
    op.execute("DROP TABLE IF EXISTS schedule")
    op.execute("DROP TABLE IF EXISTS user_memory")


def downgrade() -> None:
    op.execute(
        """CREATE TABLE IF NOT EXISTS finances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item TEXT, amount REAL, category TEXT, date TEXT
        )"""
    )
    op.execute(
        """CREATE TABLE IF NOT EXISTS schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT, type TEXT, location TEXT, time TEXT
        )"""
    )
    op.execute(
        """CREATE TABLE IF NOT EXISTS user_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            insight TEXT, context TEXT, date_learned TEXT
        )"""
    )
