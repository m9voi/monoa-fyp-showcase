"""Contextual weather-aware schedule conflicts (B5)

Revision ID: 0015_weather_conflicts
Revises: 0014_appearance_mode
Create Date: 2026-09-10

Additive, backward-safe. Single new head after 0014.

1. schedule_events + environment TEXT NOT NULL DEFAULT 'uncertain'
   Canonical: indoor | outdoor | uncertain. Existing rows get 'uncertain' (the
   safe default — never 'outdoor'). A material weather conflict is only ever
   created for an event explicitly classified 'outdoor'.

2. weather_decisions  — the owner's recorded choice for one material weather
   hazard on one outdoor event.
     id PK ("wxd_"+uuid4hex), user_id NOT NULL,
     event_id NOT NULL,
     fingerprint NOT NULL      blake2s of {event_id, start, end, environment,
                               place lat/lng @2dp, hazard_type, severity_tier} —
                               a STABLE hazard identity (never raw decimals), so an
                               immaterial forecast refresh does not resurface it and
                               a materially higher tier is a NEW fingerprint.
     hazard_type NOT NULL      thunderstorm | heavy_rain | heat | wind | cold
     severity_tier NOT NULL    1 | 2 | 3  (documented escalation rule)
     environment NOT NULL
     event_start NOT NULL, event_end NOT NULL     decision-critical event version
     forecast_provider TEXT, forecast_fetched_at TEXT
     evidence_json TEXT        minimal: hazard code/type, tier, a few measured
                               values+units, window, freshness — NEVER the raw
                               hourly array, NEVER device coordinates
     decision NOT NULL         'kept' | 'moved'
     applied_json TEXT         {start,end} actually written on a move
     move_from_json TEXT       {start,end} before a move
     exec_token NOT NULL       idempotency key for the canonical event update
     created_at NOT NULL, decided_at NOT NULL, stale_at TEXT
   UNIQUE(user_id, fingerprint)  — one decision per (user, hazard) + the
   exactly-once gate for a move.

3. weather_move_proposals — a transient server-owned candidate set for
   "Find another time". The browser never authors the times.
     id PK ("wxp_"+uuid4hex), user_id NOT NULL, event_id NOT NULL,
     fingerprint NOT NULL, hazard_type NOT NULL, severity_tier NOT NULL,
     status NOT NULL           pending | confirmed | cancelled | expired
     candidates_json NOT NULL  server-generated [{start,end,summary,basis}]
     chosen_index INTEGER
     event_start NOT NULL, event_end NOT NULL, forecast_fetched_at TEXT,
     exec_token NOT NULL,
     created_at NOT NULL, expires_at NOT NULL, confirmed_at TEXT
   INDEX (user_id, status).

downgrade() drops both tables + the column (SQLite 3.35+ DROP COLUMN, best-effort).
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0015_weather_conflicts"
down_revision: Union[str, Sequence[str], None] = "0014_appearance_mode"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _cols(bind, table):
    return {row[1] for row in bind.exec_driver_sql(f"PRAGMA table_info({table})")}


def upgrade() -> None:
    bind = op.get_bind()
    if "environment" not in _cols(bind, "schedule_events"):
        op.execute(
            "ALTER TABLE schedule_events ADD COLUMN environment TEXT NOT NULL DEFAULT 'uncertain'"
        )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS weather_decisions (
            id                TEXT PRIMARY KEY,
            user_id           TEXT NOT NULL,
            event_id          TEXT NOT NULL,
            fingerprint       TEXT NOT NULL,
            hazard_type       TEXT NOT NULL,
            severity_tier     INTEGER NOT NULL,
            environment       TEXT NOT NULL,
            event_start       TEXT NOT NULL,
            event_end         TEXT NOT NULL,
            forecast_provider TEXT,
            forecast_fetched_at TEXT,
            evidence_json     TEXT,
            decision          TEXT NOT NULL,
            applied_json      TEXT,
            move_from_json    TEXT,
            exec_token        TEXT NOT NULL,
            created_at        TEXT NOT NULL,
            decided_at        TEXT NOT NULL,
            stale_at          TEXT
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_weather_decisions_user_fp "
        "ON weather_decisions(user_id, fingerprint)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_weather_decisions_user_event "
        "ON weather_decisions(user_id, event_id)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS weather_move_proposals (
            id                  TEXT PRIMARY KEY,
            user_id             TEXT NOT NULL,
            event_id            TEXT NOT NULL,
            fingerprint         TEXT NOT NULL,
            hazard_type         TEXT NOT NULL,
            severity_tier       INTEGER NOT NULL,
            status              TEXT NOT NULL,
            candidates_json     TEXT NOT NULL,
            chosen_index        INTEGER,
            event_start         TEXT NOT NULL,
            event_end           TEXT NOT NULL,
            forecast_fetched_at TEXT,
            exec_token          TEXT NOT NULL,
            created_at          TEXT NOT NULL,
            expires_at          TEXT NOT NULL,
            confirmed_at        TEXT
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_weather_move_proposals_user_status "
        "ON weather_move_proposals(user_id, status)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_weather_move_proposals_user_status")
    op.execute("DROP TABLE IF EXISTS weather_move_proposals")
    op.execute("DROP INDEX IF EXISTS idx_weather_decisions_user_event")
    op.execute("DROP INDEX IF EXISTS idx_weather_decisions_user_fp")
    op.execute("DROP TABLE IF EXISTS weather_decisions")
    bind = op.get_bind()
    if "environment" in _cols(bind, "schedule_events"):
        try:
            op.execute("ALTER TABLE schedule_events DROP COLUMN environment")
        except Exception:
            pass
