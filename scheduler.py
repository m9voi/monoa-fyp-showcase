"""Background reminder scheduler (Phase 1).

Responsibility: periodically scan the `tasks` table and move reminders whose
time has arrived from reminder_status = 'PENDING' to 'DUE'. That is all.

Explicitly NOT in scope for this phase:
  * no push notifications / Web Push / email / SMS
  * no in-memory reminder queue - the DATABASE is the single source of truth

Design points:
  * only PENDING -> DUE transitions, so a reminder is processed exactly once
  * completed tasks (is_completed = 1) are ignored
  * safe to run repeatedly and safe across backend restarts: on restart the
    still-PENDING reminders are simply re-scanned
  * a failure in here must never take down the API (all callers guard it,
    and start() swallows its own errors)
"""

import os
import datetime

import db as db_layer
import security_config as sec

_scheduler = None  # APScheduler BackgroundScheduler instance, or None

# Guarded (Phase 5H-B6): a non-integer / zero / negative / absurd value is a
# fail-fast startup error rather than a bare int() ValueError traceback or a
# poll interval that silently disables the reminder promotion (0/negative is
# rejected by APScheduler and swallowed by start()'s guard). 5s .. 1h.
POLL_SECONDS = sec.int_env(
    "COREOS_REMINDER_POLL_SECONDS", 60, min_value=5, max_value=3600
)
ENABLED = os.getenv("COREOS_SCHEDULER_ENABLED", "1") not in ("0", "false", "False", "")


def scan_due_reminders_once(now_iso: str | None = None) -> int:
    """Promote every PENDING reminder whose time has passed to DUE.

    Returns the number of reminders promoted. Synchronous and idempotent -
    also used directly by the test-suite.
    """
    now_iso = now_iso or db_layer.iso_now()
    with db_layer.transaction() as conn:
        cur = conn.execute(
            """
            UPDATE tasks
               SET reminder_status = 'DUE',
                   updated_at = ?
             WHERE reminder_at IS NOT NULL
               AND reminder_at <> ''
               AND reminder_at <= ?
               AND reminder_status = 'PENDING'
               AND (is_completed IS NULL OR is_completed = 0)
            """,
            (now_iso, now_iso),
        )
        promoted = cur.rowcount if cur.rowcount is not None else 0
    if promoted:
        print(f"SYSTEM: reminder scheduler marked {promoted} reminder(s) as DUE.")
    return promoted


def _job():
    try:
        scan_due_reminders_once()
    except Exception as e:  # never let the job thread die
        print(f"SYSTEM WARNING: reminder scan failed ({e}).")


def start():
    """Start the background scheduler. Best-effort and idempotent."""
    global _scheduler
    if not ENABLED:
        print("SYSTEM: reminder scheduler disabled via COREOS_SCHEDULER_ENABLED.")
        return
    if _scheduler is not None:
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler

        _scheduler = BackgroundScheduler(daemon=True, timezone="UTC")
        _scheduler.add_job(
            _job,
            trigger="interval",
            seconds=POLL_SECONDS,
            id="reminder_scan",
            next_run_time=datetime.datetime.now(datetime.timezone.utc),  # run once at boot
            max_instances=1,
            coalesce=True,
        )
        _scheduler.start()
        print(f"SYSTEM: reminder scheduler started (every {POLL_SECONDS}s).")
    except Exception as e:
        _scheduler = None
        print(f"SYSTEM WARNING: could not start reminder scheduler ({e}). "
              f"Reminders will still persist; they just won't be auto-marked DUE.")


def shutdown():
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None
