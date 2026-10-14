"""Central database access layer for CoreOS.

Phase 1 goal: one place that owns the SQLite connection configuration
(path, row factory, foreign-key enforcement) and a safe transaction
helper with commit / rollback / close handling.

Deliberately NOT an ORM. Queries stay as plain parameterized SQL in the
endpoints; this module only standardizes how a connection is obtained
and how a unit of work is committed or rolled back.
"""

import os
import sqlite3
import datetime
from contextlib import contextmanager

# Single configuration source. Mirrors main.py so both agree even if one
# is imported without the other (e.g. Alembic env, pytest).
DB_PATH = os.getenv("COREOS_DB_PATH", "coreos.db")


def iso_now() -> str:
    """Timezone-aware ISO-8601 timestamp (local offset, e.g. +08:00)."""
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()


def get_connection() -> sqlite3.Connection:
    """Return a configured connection.

    - row_factory = sqlite3.Row  (dict-like rows, unchanged from before)
    - PRAGMA foreign_keys = ON   (SQLite needs this per-connection; it is
      off by default and must be enabled for FK constraints to be enforced)
    - PRAGMA busy_timeout        (wait instead of instantly erroring if the
      file is briefly locked by the reminder scheduler)
    """
    conn = sqlite3.connect(_resolve_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _resolve_path() -> str:
    # Read the env var lazily so tests can point at a temp DB after import.
    return os.getenv("COREOS_DB_PATH", DB_PATH)


@contextmanager
def transaction():
    """Unit of work. Commits on success, rolls back on any exception,
    and always closes the connection.

        with transaction() as conn:
            conn.execute("INSERT ...", (...))
    """
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def read_connection():
    """Read-only style helper: yields a connection and always closes it.
    No implicit commit (use for SELECT-only handlers)."""
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()
