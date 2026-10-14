"""Initialise / upgrade the CoreOS database schema.

Phase 2: the schema is owned entirely by Alembic (alembic/versions). This
script does NOT create tables and does NOT seed a hardcoded user any more —
accounts are created through the real registration flow (POST /api/auth/register
in the app), and an existing/legacy account gets a password via
`python bootstrap_user.py`.

Run:  python setup_db.py
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("COREOS_DB_PATH", os.path.join(ROOT, "coreos.db"))


def run_migrations() -> None:
    env = dict(os.environ)
    env.setdefault("COREOS_DB_PATH", DB_PATH)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT, env=env,
    )
    if result.returncode != 0:
        raise SystemExit(f"alembic upgrade failed (exit {result.returncode})")


if __name__ == "__main__":
    run_migrations()
    print(f"OK: {DB_PATH} schema is at Alembic head.")
    print("Next: create an account in the app (Sign up), or set a password for an")
    print("existing account with:  python bootstrap_user.py --email <you@example.com>")
