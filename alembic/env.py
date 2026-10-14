"""Alembic environment for CoreOS.

The database is local SQLite. The file path comes from COREOS_DB_PATH
(same variable the app and tests use), defaulting to ./coreos.db.

Batch mode (render_as_batch=True) is enabled because SQLite cannot ALTER
existing constraints in place; Alembic emulates it via table copy.
"""

import os
import sys

from sqlalchemy import engine_from_config, pool

from alembic import context

config = context.config

# NOTE: we deliberately do NOT call logging.config.fileConfig here. This env
# is also imported in-process by main.init_db() at app startup, and fileConfig
# would reconfigure the root logger and swallow uvicorn's logs.

# Make the project root importable (for prepend_sys_path safety).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = os.getenv("COREOS_DB_PATH", "coreos.db")
DB_URL = f"sqlite:///{os.path.abspath(DB_PATH)}"

# No ORM metadata: migrations are explicit, no autogenerate.
target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=DB_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = DB_URL
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
