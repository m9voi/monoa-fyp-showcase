"""authentication: users auth columns + sessions table

Revision ID: 0006_authentication
Revises: 0005_foreign_keys
Create Date: 2026-09-01

Adds the minimum secure-auth schema:

  users  + email_normalized (UNIQUE, lowercased/trimmed)
         + password_hash      (Argon2id hash string; NULL = cannot log in yet)
         + is_active          (INTEGER 1/0, default 1)
         + created_at, updated_at

  sessions  (new)  id, user_id -> users(id) ON DELETE CASCADE,
                   token_hash (UNIQUE — HMAC-SHA256 of the opaque cookie token),
                   created_at, expires_at, last_used_at, user_agent

Existing data is preserved:
  * every existing user keeps its id, so ALL user-owned rows
    (transactions, tasks, schedule_events, ai_recommendations,
     user_preferences, user_budgets, user_financial_settings, map_locations)
    remain attached to the same owner.
  * email_normalized is backfilled from the existing email.
  * password_hash is left NULL for pre-existing accounts — they cannot log in
    until a password is set explicitly via `python bootstrap_user.py`
    (no password is invented, hardcoded, or committed).

Idempotent: each ALTER is guarded by a PRAGMA table_info check.
"""
from typing import Sequence, Union
import datetime

from alembic import op

revision: str = "0006_authentication"
down_revision: Union[str, Sequence[str], None] = "0005_foreign_keys"
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

    _add_col(bind, "users", "email_normalized", "email_normalized TEXT")
    _add_col(bind, "users", "password_hash", "password_hash TEXT")
    _add_col(bind, "users", "is_active", "is_active INTEGER NOT NULL DEFAULT 1")
    _add_col(bind, "users", "created_at", "created_at TEXT")
    _add_col(bind, "users", "updated_at", "updated_at TEXT")

    op.execute(
        "UPDATE users SET email_normalized = lower(trim(email)) "
        "WHERE (email_normalized IS NULL OR email_normalized = '') AND email IS NOT NULL"
    )
    op.execute(f"UPDATE users SET created_at = COALESCE(created_at, '{now}') WHERE created_at IS NULL")
    op.execute("UPDATE users SET updated_at = COALESCE(updated_at, created_at) WHERE updated_at IS NULL")
    op.execute("UPDATE users SET is_active = 1 WHERE is_active IS NULL")

    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_normalized "
        "ON users(email_normalized)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            last_used_at TEXT,
            user_agent TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_sessions_expires")
    op.execute("DROP INDEX IF EXISTS idx_sessions_user")
    op.execute("DROP TABLE IF EXISTS sessions")
    op.execute("DROP INDEX IF EXISTS idx_users_email_normalized")
    for col in ("updated_at", "created_at", "is_active", "password_hash", "email_normalized"):
        try:
            op.execute(f"ALTER TABLE users DROP COLUMN {col}")
        except Exception:
            pass
