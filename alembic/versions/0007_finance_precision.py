"""finance precision: transactions.amount_sen (integer minor units)

Revision ID: 0007_finance_precision
Revises: 0006_authentication
Create Date: 2026-09-02

Money was stored/summed as REAL (float), which accumulates rounding error.
This adds an authoritative integer column:

  transactions.amount_sen INTEGER   -- amount in sen (1 MYR = 100 sen)

Backfill: amount_sen = CAST(ROUND(amount * 100) AS INTEGER).

`amount` (REAL, major units) is KEPT and stays in sync on every write, so the
change is fully backward-compatible and reversible. All Phase 3 reads/sums use
`amount_sen`; `finance_core.row_amount_sen()` falls back to `amount` if a row's
`amount_sen` is somehow NULL.

Also: defensively backfill any `transactions.date` that lacks a timezone offset
to '+08:00' (Asia/Kuala_Lumpur), so lexical period comparisons are correct.
The existing real rows already carry '+08:00'; this is a no-op for them.

Idempotent (guarded ALTER). downgrade() drops the column.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0007_finance_precision"
down_revision: Union[str, Sequence[str], None] = "0006_authentication"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _cols(bind, table):
    return {row[1] for row in bind.exec_driver_sql(f"PRAGMA table_info({table})")}


def upgrade() -> None:
    bind = op.get_bind()
    if "amount_sen" not in _cols(bind, "transactions"):
        op.execute("ALTER TABLE transactions ADD COLUMN amount_sen INTEGER")

    # exact backfill: round(amount*100) to nearest integer
    op.execute(
        "UPDATE transactions "
        "SET amount_sen = CAST(ROUND(COALESCE(amount, 0) * 100) AS INTEGER) "
        "WHERE amount_sen IS NULL"
    )

    # normalize naive dates (no offset / no 'Z') to +08:00
    op.execute(
        "UPDATE transactions SET date = date || '+08:00' "
        "WHERE date IS NOT NULL AND length(date) >= 19 "
        "AND date NOT LIKE '%+%' AND date NOT LIKE '%Z' "
        "AND substr(date, 20) NOT LIKE '-%'"
    )

    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_transactions_user_type_date "
        "ON transactions(user_id, type, date)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_transactions_user_type_date")
    try:
        op.execute("ALTER TABLE transactions DROP COLUMN amount_sen")
    except Exception:
        pass
