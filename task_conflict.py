"""Task <-> event schedule conflict core (B7) — pure, deterministic.

A TASK becomes eligible for a schedule overlap only when it represents a real
scheduled work window:

  * an explicit tz-aware start   (``planned_at``)
  * an explicit end              (``scheduled_end`` — added by migration 0017)
  * ``end > start``

A ``due_date`` / deadline alone is NOT a time-block conflict. An untimed task is
NOT a conflict. A ``reminder_at`` (a notification instant) is NOT a conflict.
No duration is ever inferred — a ``planned_at`` with no ``scheduled_end`` stays a
point marker.

This module has no DB, no HTTP, no AI. ``schedule_core`` supplies tz-correct
parsing + the half-open overlap test; ``idempotency`` supplies the opaque hash.
"""
from __future__ import annotations

import datetime
from typing import Optional

import schedule_core as _sc
from idempotency import request_fingerprint

_MYT = _sc.MYT
_MOVE_LEAD_MINUTES = 60
CONFLICT_TYPE = "task_event_overlap"

_WD = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MON = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# --------------------------------------------------------------------------- #
# eligibility
# --------------------------------------------------------------------------- #
def task_window(task_row: dict) -> Optional[tuple[datetime.datetime, datetime.datetime]]:
    """``(start, end)`` for a task that has a REAL scheduled work window, else
    ``None``. Requires ``planned_at`` and ``scheduled_end`` both present, both
    parseable to an aware datetime WITH a time-of-day, and ``end > start``."""
    s_raw = task_row.get("planned_at")
    e_raw = task_row.get("scheduled_end")
    if not s_raw or not e_raw:
        return None
    s = _sc._parse(s_raw)
    e = _sc._parse(e_raw)
    if not s or not e:
        return None
    if e <= s:
        return None
    return s, e


def has_deadline_only(task_row: dict) -> bool:
    """True when the task has a ``due_date`` (or ``planned_at``) but no real
    window — used to prove a deadline never becomes a time-block conflict."""
    return task_window(task_row) is None and bool(
        task_row.get("due_date") or task_row.get("planned_at")
    )


# --------------------------------------------------------------------------- #
# stable identity
# --------------------------------------------------------------------------- #
def conflict_fingerprint(
    *,
    task_id: str,
    task_start_iso: str,
    task_end_iso: str,
    event_id: str,
    event_start_iso: str,
    event_end_iso: str,
    occurrence_date: Optional[str] = None,
) -> str:
    """Deterministic identity for one task-window / event-occurrence overlap.

    Built from the task id + its window, the event id + the concrete occurrence
    window, and the recurring occurrence date (or ``None``). A material task or
    event time edit changes the window -> a different fingerprint -> the old
    decision no longer matches and the recomputed overlap resurfaces truthfully.
    NOT derived from titles or any display text. Never reuses the B3/B6
    event-pair fingerprint scheme.
    """
    def _n(iso):
        d = _sc._parse(iso)
        return d.isoformat() if d else None

    fields = {
        "t": CONFLICT_TYPE,
        "task": {"id": task_id, "start": _n(task_start_iso), "end": _n(task_end_iso)},
        "event": {"id": event_id, "start": _n(event_start_iso), "end": _n(event_end_iso)},
    }
    if occurrence_date:
        fields["occ"] = str(occurrence_date)
    return request_fingerprint(fields)


# --------------------------------------------------------------------------- #
# candidate task windows  ("Find another time")
# --------------------------------------------------------------------------- #
def _clock(x: datetime.datetime) -> str:
    h = x.hour % 12 or 12
    ap = "am" if x.hour < 12 else "pm"
    return f"{h}:{x.minute:02d} {ap}" if x.minute else f"{h} {ap}"


def _summary(s: datetime.datetime, e: datetime.datetime) -> str:
    a, b = s.astimezone(_MYT), e.astimezone(_MYT)
    return f"{_WD[a.weekday()]} {a.day} {_MON[a.month - 1]} {a.year}, {_clock(a)} – {_clock(b)}"


# --------------------------------------------------------------------------- #
# reasonable-hours policy (B7.1)
# --------------------------------------------------------------------------- #
# A fallback USABILITY policy — not a learned/personal preference, not AI.
# "Ordinary" local hours are 07:00-22:00 (MYT). A task whose ORIGINAL work block
# starts inside that window only gets daytime candidates; a task whose original
# block starts outside it (deliberately after-hours) gets candidates in the
# complementary night band [22:00, 07:00 next day) instead. Both bands still
# obey the exact task duration, the future/lead-time rule, every collision, and
# the due_date cap.
DAY_START_MIN = 7 * 60          # 07:00 local
DAY_END_MIN = 22 * 60          # 22:00 local


def _local_min(dt: datetime.datetime) -> int:
    a = dt.astimezone(_MYT)
    return a.hour * 60 + a.minute


def band_of(dt: datetime.datetime) -> str:
    """'day' when the instant is in [07:00, 22:00) local, else 'night'."""
    return "day" if DAY_START_MIN <= _local_min(dt) < DAY_END_MIN else "night"


def _fits_band(s: datetime.datetime, e: datetime.datetime, band: str) -> bool:
    """Whole [s, e) window lies inside one instance of ``band`` (local MYT)."""
    sa, ea = s.astimezone(_MYT), e.astimezone(_MYT)
    sm, em = sa.hour * 60 + sa.minute, ea.hour * 60 + ea.minute
    same_day = sa.date() == ea.date()
    if band == "day":
        return same_day and sm >= DAY_START_MIN and em <= DAY_END_MIN and em > sm
    # night band = [22:00, 07:00 next day)
    if same_day:
        # both in the late slice (>=22:00) or both in the early slice (<=07:00)
        return (sm >= DAY_END_MIN and em > sm) or (em <= DAY_START_MIN and em > sm)
    # crosses exactly one midnight: start in the late slice, end in the early slice
    return sm >= DAY_END_MIN and em <= DAY_START_MIN


def occurrence_date_label(iso_date: Optional[str]) -> Optional[str]:
    if not iso_date:
        return None
    try:
        d = datetime.date.fromisoformat(iso_date)
    except (TypeError, ValueError):
        return None
    return f"{_WD[d.weekday()]} {d.day} {_MON[d.month - 1]}"


def propose_task_slots(
    *,
    duration: datetime.timedelta,
    busy_intervals: list[tuple[str, str]],
    now_utc: datetime.datetime,
    horizon_end_utc: datetime.datetime,
    original_start: datetime.datetime,
    due_end: Optional[datetime.datetime] = None,
    max_slots: int = 3,
) -> list[dict]:
    """Real candidate windows for the TASK (B7.1 quality policy). Every candidate:
      * preserves the task's exact ``duration``;
      * starts at least ``_MOVE_LEAD_MINUTES`` from now and is not in the past;
      * ends on or before ``due_end`` when a deadline is given (work is NEVER
        scheduled past its deadline; ``due_end`` is not modified);
      * ends within ``horizon_end_utc``;
      * lies wholly inside the task's reasonable-hours band — daytime
        [07:00, 22:00) local when the ORIGINAL block starts in ordinary hours,
        otherwise the night band [22:00, 07:00) — see ``band_of`` / ``_fits_band``;
      * does NOT overlap any ``busy_intervals`` entry (the owner's timed events,
        recurring event occurrences, and OTHER scheduled task windows).

    All valid candidates in range are collected, then ranked DETERMINISTICALLY:
      1. before the deadline (all returned are — kept as a guard key);
      2. closest reasonable start to the original work block;
      3. earliest start as the tie-breaker.
    Returns up to ``max_slots``. Empty list -> no suitable slot; the caller must
    NOT fabricate one and must NOT mutate anything.
    """
    if duration <= datetime.timedelta(0):
        return []
    band = band_of(original_start)
    earliest = now_utc + datetime.timedelta(minutes=_MOVE_LEAD_MINUTES)
    minute = 0 if earliest.minute == 0 else (30 if earliest.minute <= 30 else 60)
    cur = earliest.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(minutes=minute)

    hard_end = horizon_end_utc
    if due_end is not None and due_end < hard_end:
        hard_end = due_end

    def _busy(s: datetime.datetime, e: datetime.datetime) -> bool:
        si, ei = s.isoformat(), e.isoformat()
        for bs, be in busy_intervals:
            if _sc.intervals_overlap(si, ei, bs, be):
                return True
        return False

    valid: list[tuple[datetime.datetime, datetime.datetime]] = []
    step = datetime.timedelta(minutes=30)
    guard = 0
    while cur + duration <= hard_end and guard < 4000:
        guard += 1
        s_utc, e_utc = cur, cur + duration
        cur = cur + step
        if s_utc < earliest:
            continue
        if due_end is not None and e_utc > due_end:
            continue
        if not _fits_band(s_utc, e_utc, band):
            continue
        if _busy(s_utc, e_utc):
            continue
        valid.append((s_utc, e_utc))

    valid.sort(key=lambda p: (
        0 if (due_end is None or p[1] <= due_end) else 1,
        abs((p[0] - original_start).total_seconds()),
        p[0],
    ))
    return [
        {"start": s.astimezone(_MYT).isoformat(),
         "end": e.astimezone(_MYT).isoformat(),
         "summary": _summary(s, e)}
        for s, e in valid[:max_slots]
    ]


# --------------------------------------------------------------------------- #
# explanation
# --------------------------------------------------------------------------- #
def _fmt(iso: Optional[str]) -> str:
    d = _sc._parse(iso)
    if not d:
        return "?"
    h = d.hour % 12 or 12
    ap = "am" if d.hour < 12 else "pm"
    return f"{h}:{d.minute:02d} {ap}" if d.minute else f"{h} {ap}"


def explain(task_title: Optional[str], event_title: Optional[str],
            task_win: tuple, event_win: tuple, occ_label: Optional[str] = None) -> str:
    tt = task_title or "A task"
    et = event_title or "an event"
    ts = f"{_fmt(task_win[0].isoformat())}–{_fmt(task_win[1].isoformat())}"
    es = f"{_fmt(event_win[0].isoformat())}–{_fmt(event_win[1].isoformat())}"
    lead = f"On {occ_label}: " if occ_label else ""
    return (f"{lead}your scheduled task “{tt}” ({ts}) runs into "
            f"“{et}” ({es}).")
