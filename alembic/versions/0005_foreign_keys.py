"""add FOREIGN KEY(user_id) REFERENCES users(id) to user-owned tables

Revision ID: 0005_foreign_keys
Revises: 0004_indexes
Create Date: 2026-09-01

SQLite cannot add a constraint in place, so each table is rebuilt with the
documented "12-step" table-redefinition pattern: create new table with the
FK, copy rows, drop old, rename.

SAFETY GATE: before rebuilding any table this migration counts rows whose
user_id does not exist in `users`. If ANY orphan row is found the migration
raises immediately and changes nothing - per the Phase 1 instruction
"report the violation and stop that specific migration". A timestamped DB
backup is taken before migrations run.

ON DELETE CASCADE is used so a future account-deletion feature (Phase 2+)
removes a user's owned rows atomically.

downgrade() rebuilds the same tables without the FK.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0005_foreign_keys"
down_revision: Union[str, Sequence[str], None] = "0004_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# table -> (column DDL lines, list of CREATE INDEX statements to restore)
TABLES = {
    "transactions": (
        """id TEXT PRIMARY KEY,
           user_id TEXT,
           amount REAL,
           category TEXT,
           date TEXT,
           type TEXT DEFAULT 'expense',
           description TEXT,
           merchant TEXT,
           created_at TEXT,
           updated_at TEXT""",
        [
            "CREATE INDEX IF NOT EXISTS idx_transactions_user_date ON transactions(user_id, date)",
            "CREATE INDEX IF NOT EXISTS idx_transactions_user_type ON transactions(user_id, type)",
        ],
    ),
    "tasks": (
        """id TEXT PRIMARY KEY,
           user_id TEXT NOT NULL,
           title TEXT NOT NULL,
           is_completed BOOLEAN DEFAULT 0,
           due_date TEXT,
           reminder_at TEXT,
           created_at TEXT,
           updated_at TEXT,
           reminder_status TEXT""",
        [
            "CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_tasks_reminder_scan ON tasks(reminder_status, reminder_at)",
        ],
    ),
    "schedule_events": (
        """id TEXT PRIMARY KEY,
           user_id TEXT,
           title TEXT,
           location TEXT,
           start_time TEXT,
           end_time TEXT,
           created_at TEXT,
           updated_at TEXT""",
        [
            "CREATE INDEX IF NOT EXISTS idx_schedule_events_user_start ON schedule_events(user_id, start_time)",
        ],
    ),
    "ai_recommendations": (
        """id TEXT PRIMARY KEY,
           user_id TEXT,
           module TEXT,
           type TEXT,
           message TEXT,
           status TEXT,
           target_id TEXT,
           suggested_action TEXT,
           created_at TEXT,
           updated_at TEXT""",
        [
            "CREATE INDEX IF NOT EXISTS idx_ai_recommendations_user_module_status "
            "ON ai_recommendations(user_id, module, status)",
        ],
    ),
    "map_locations": (
        """id TEXT PRIMARY KEY,
           user_id TEXT NOT NULL,
           name TEXT NOT NULL,
           activity TEXT,
           latitude REAL,
           longitude REAL,
           address TEXT,
           distance REAL,
           created_at TEXT""",
        [
            "CREATE INDEX IF NOT EXISTS idx_map_locations_user ON map_locations(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_map_locations_user_activity ON map_locations(user_id, activity)",
        ],
    ),
    "user_preferences": (
        """user_id TEXT PRIMARY KEY,
           notifications BOOLEAN,
           location_services BOOLEAN,
           dark_mode BOOLEAN""",
        [],
    ),
    "user_budgets": (
        """user_id TEXT PRIMARY KEY,
           monthly_budget REAL NOT NULL DEFAULT 0,
           weekly_budget REAL NOT NULL DEFAULT 0,
           daily_budget REAL NOT NULL DEFAULT 0,
           updated_at TEXT""",
        [],
    ),
    "user_financial_settings": (
        """user_id TEXT PRIMARY KEY,
           starting_balance REAL NOT NULL DEFAULT 0,
           weekly_budget REAL NOT NULL DEFAULT 0,
           monthly_budget REAL NOT NULL DEFAULT 0,
           daily_budget REAL NOT NULL DEFAULT 0,
           updated_at TEXT""",
        [],
    ),
}

FK_LINE = "FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE"


def _col_names(bind, table):
    return [row[1] for row in bind.exec_driver_sql(f"PRAGMA table_info({table})")]


def _has_user_fk(bind, table):
    for row in bind.exec_driver_sql(f"PRAGMA foreign_key_list({table})"):
        # row: (id, seq, table, from, to, on_update, on_delete, match)
        if row[2] == "users" and row[3] == "user_id":
            return True
    return False


def _rebuild(bind, table, body, indexes, with_fk: bool):
    if with_fk and _has_user_fk(bind, table):
        return  # already has it - idempotent

    old_cols = _col_names(bind, table)
    new_cols = [line.strip().split()[0] for line in body.strip().splitlines()]
    shared = [c for c in new_cols if c in old_cols]
    collist = ", ".join(shared)

    inner = body.strip()
    if with_fk:
        inner = inner + ",\n           " + FK_LINE

    op.execute(f"ALTER TABLE {table} RENAME TO {table}__old")
    op.execute(f"CREATE TABLE {table} (\n           {inner}\n)")
    op.execute(f"INSERT INTO {table} ({collist}) SELECT {collist} FROM {table}__old")
    op.execute(f"DROP TABLE {table}__old")
    for stmt in indexes:
        op.execute(stmt)


def _guard_no_orphans(bind):
    problems = []
    for table in TABLES:
        n = bind.exec_driver_sql(
            f"SELECT COUNT(*) FROM {table} WHERE user_id IS NOT NULL "
            f"AND user_id NOT IN (SELECT id FROM users)"
        ).scalar()
        if n:
            problems.append(f"{table}: {n} row(s) with a user_id not present in users")
    if problems:
        raise RuntimeError(
            "0005_foreign_keys ABORTED - existing data violates the proposed "
            "FOREIGN KEY(user_id) -> users(id). No changes were made. "
            "Fix or reassign these rows, then re-run:\n  - " + "\n  - ".join(problems)
        )


def upgrade() -> None:
    bind = op.get_bind()
    _guard_no_orphans(bind)
    for table, (body, indexes) in TABLES.items():
        _rebuild(bind, table, body, indexes, with_fk=True)


def downgrade() -> None:
    bind = op.get_bind()
    for table, (body, indexes) in TABLES.items():
        # Force a rebuild without the FK even if present.
        op.execute(f"ALTER TABLE {table} RENAME TO {table}__old")
        old_cols = _col_names(bind, f"{table}__old")
        new_cols = [line.strip().split()[0] for line in body.strip().splitlines()]
        shared = [c for c in new_cols if c in old_cols]
        collist = ", ".join(shared)
        op.execute(f"CREATE TABLE {table} (\n           {body.strip()}\n)")
        op.execute(f"INSERT INTO {table} ({collist}) SELECT {collist} FROM {table}__old")
        op.execute(f"DROP TABLE {table}__old")
        for stmt in indexes:
            op.execute(stmt)
