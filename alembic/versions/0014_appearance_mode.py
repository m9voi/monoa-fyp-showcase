"""appearance mode preference (B4)

Revision ID: 0014_appearance_mode
Revises: 0013_schedule_conflict_decisions
Create Date: 2026-09-10

Additive, backward-safe. Adds ONE nullable column, `user_preferences.appearance_mode`,
holding the canonical user-facing appearance choice: 'system' | 'light' | 'dark'.
Existing rows keep NULL and are treated as "no account preference" — the client
then follows this device's saved choice, otherwise System.

The legacy `user_preferences.dark_mode` boolean is NOT removed and NOT migrated:
it was inert (the app shipped a dark-only theme and the Settings screen labelled
it as such), so it no longer drives appearance. It stays in the column for
backward compatibility; a later batch may retire it.

downgrade() drops the new column (SQLite 3.35+ DROP COLUMN, best-effort).
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0014_appearance_mode"
down_revision: Union[str, Sequence[str], None] = "0013_schedule_conflict_decisions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _cols(bind, table):
    return {row[1] for row in bind.exec_driver_sql(f"PRAGMA table_info({table})")}


def upgrade() -> None:
    bind = op.get_bind()
    if "appearance_mode" not in _cols(bind, "user_preferences"):
        op.execute("ALTER TABLE user_preferences ADD COLUMN appearance_mode TEXT")


def downgrade() -> None:
    bind = op.get_bind()
    if "appearance_mode" in _cols(bind, "user_preferences"):
        try:
            op.execute("ALTER TABLE user_preferences DROP COLUMN appearance_mode")
        except Exception:
            pass  # older SQLite without DROP COLUMN — leave the nullable column in place
