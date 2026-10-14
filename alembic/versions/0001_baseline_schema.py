"""baseline schema (state at end of Phase 0)

Revision ID: 0001_baseline
Revises:
Create Date: 2026-09-01

This migration reproduces the schema exactly as it existed after Phase 0.
Every statement uses "IF NOT EXISTS" so it is safe to run against the
already-populated coreos.db (it becomes a no-op there) and also builds a
fresh database from nothing.

downgrade() is intentionally a no-op: we never tear the baseline back down.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0001_baseline"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


BASELINE_TABLES = [
    """CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY, name TEXT, email TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS user_preferences (
        user_id TEXT PRIMARY KEY, notifications BOOLEAN,
        location_services BOOLEAN, dark_mode BOOLEAN
    )""",
    """CREATE TABLE IF NOT EXISTS schedule_events (
        id TEXT PRIMARY KEY, user_id TEXT, title TEXT, location TEXT,
        start_time TEXT, end_time TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS transactions (
        id TEXT PRIMARY KEY, user_id TEXT, amount REAL, category TEXT, date TEXT,
        type TEXT DEFAULT 'expense', description TEXT, merchant TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS ai_recommendations (
        id TEXT PRIMARY KEY, user_id TEXT, module TEXT, type TEXT, message TEXT,
        status TEXT, target_id TEXT, suggested_action TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL,
        is_completed BOOLEAN DEFAULT 0, due_date TEXT, reminder_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS user_budgets (
        user_id TEXT PRIMARY KEY,
        monthly_budget REAL NOT NULL DEFAULT 0,
        weekly_budget REAL NOT NULL DEFAULT 0,
        daily_budget REAL NOT NULL DEFAULT 0,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS user_financial_settings (
        user_id TEXT PRIMARY KEY,
        starting_balance REAL NOT NULL DEFAULT 0,
        weekly_budget REAL NOT NULL DEFAULT 0,
        monthly_budget REAL NOT NULL DEFAULT 0,
        daily_budget REAL NOT NULL DEFAULT 0,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS map_locations (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        name TEXT NOT NULL,
        activity TEXT,
        latitude REAL,
        longitude REAL,
        address TEXT,
        distance REAL,
        created_at TEXT
    )""",
]

BASELINE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_map_locations_user ON map_locations(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_map_locations_user_activity ON map_locations(user_id, activity)",
]


def upgrade() -> None:
    for stmt in BASELINE_TABLES:
        op.execute(stmt)
    for stmt in BASELINE_INDEXES:
        op.execute(stmt)


def downgrade() -> None:
    # Baseline is the floor. Nothing to undo.
    pass
