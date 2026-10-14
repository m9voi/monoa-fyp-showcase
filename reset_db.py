"""Bring the CoreOS database schema up to date (non-destructive).

Historically this hand-created tables. Since Phase 1 the schema is owned by
Alembic; since Phase 2 there is no user seeding here either. This script does
NOT drop or wipe anything — it just runs `alembic upgrade head`. The name is
kept only for muscle-memory / older docs.

Run:  python reset_db.py
"""
from setup_db import run_migrations, DB_PATH

if __name__ == "__main__":
    run_migrations()
    print(f"OK: {DB_PATH} schema is at Alembic head (no data was deleted).")
