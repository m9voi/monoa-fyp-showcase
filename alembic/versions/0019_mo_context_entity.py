"""Mo short-term context — active entity (Mo smartness + experience closure)

Revision ID: 0019_mo_context_entity
Revises: 0018_mo_context
Create Date: 2026-09-15

Additive, backward-safe. Adds TWO nullable columns to the existing
mo_context table (created by 0018, already applied to the real database —
this migration extends it rather than editing 0018 in place).

MOTIVATION
  0018's mo_context remembered only WHEN the user was last talking about
  (context_date). It has no way to answer "Actually make it 6:30" after
  "Move gym earlier" — a bare pronoun with no title at all. entity_type +
  entity_title remember WHAT was last discussed (an event or task title) the
  same way context_date remembers WHEN, so a follow-up with no explicit
  target_description can still be resolved — it is still only a HINT fed to
  the interpretation model; the actual mutation target is always found by a
  real, deterministic database lookup (see _mo_resolve_event_target /
  _mo_resolve_task_target in main.py), never by trusting context alone.

mo_context (existing table, altered)
  + entity_type   TEXT   "event" | "task", or NULL
  + entity_title  TEXT   the entity's own title, or NULL

Both NULL for every existing row and whenever no entity has been discussed
recently — fully backward compatible, no data migration needed.

downgrade() drops both columns. No existing data is affected in either
direction.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0019_mo_context_entity"
down_revision: Union[str, Sequence[str], None] = "0018_mo_context"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _cols(bind, table):
    return {row[1] for row in bind.exec_driver_sql(f"PRAGMA table_info({table})")}


def upgrade() -> None:
    bind = op.get_bind()
    existing = _cols(bind, "mo_context")
    if "entity_type" not in existing:
        op.execute("ALTER TABLE mo_context ADD COLUMN entity_type TEXT")
    if "entity_title" not in existing:
        op.execute("ALTER TABLE mo_context ADD COLUMN entity_title TEXT")


def downgrade() -> None:
    bind = op.get_bind()
    existing = _cols(bind, "mo_context")
    if "entity_title" in existing:
        try:
            op.execute("ALTER TABLE mo_context DROP COLUMN entity_title")
        except Exception:
            pass
    if "entity_type" in existing:
        try:
            op.execute("ALTER TABLE mo_context DROP COLUMN entity_type")
        except Exception:
            pass
