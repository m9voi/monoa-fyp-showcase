"""Plan conflict → decision core (B3).

Pure, deterministic domain logic for turning a detected schedule overlap into an
*actionable* item the owner can Accept or Keep:

  * ``pair_fingerprint``      — a stable identity for one overlap, derived only
    from the two events' decision-critical data (id, start, end, all_day,
    location). Unchanged data → identical fingerprint. Any relevant time / all-day
    / location edit → a different fingerprint → the old decision no longer
    matches and the (now different) overlap is recomputed truthfully.
  * ``propose_shift``         — the ONLY resolution generator. It moves the
    later-starting event to begin exactly when the earlier one ends, preserving
    the moved event's duration. It returns ``None`` (never a fabricated time)
    when the shift cannot be computed safely or would collide with a third item.
  * ``explain``               — a concise, human sentence about the overlap.

No DB, no FastAPI, no ``main`` import. ``schedule_core`` (itself pure) supplies
tz-correct parsing and the half-open overlap test; ``idempotency`` supplies the
opaque hash. The AI is never involved.
"""
from __future__ import annotations

import datetime
from typing import Optional

import schedule_core as _sc
from idempotency import request_fingerprint

CONFLICT_TYPE_EVENT_OVERLAP = "event_overlap"
CONFLICT_TYPE_RECURRING_OVERLAP = "recurring_overlap"

_DEFAULT_EVENT_HOURS = 1  # mirrors schedule_core.expand_event's "no end_time" default


# --------------------------------------------------------------------------- #
# normalization
# --------------------------------------------------------------------------- #
def _se(row: dict) -> dict:
    """Normalize a stored event row OR a find_conflicts occurrence dict to the
    fields that define an overlap's identity. tz-correct, order-independent.

    For a RECURRING occurrence dict (B6) the canonical occurrence identity
    (series id + default MYT date + recurrence rule + series base start) is added
    so the fingerprint is bound to that one occurrence of that one series
    version. A non-recurring event is untouched — its fingerprint is byte-for-
    byte what it was before B6.
    """
    start_raw = row.get("start") or row.get("start_time")
    end_raw = row.get("end") or row.get("end_time")
    start = _sc._parse(start_raw)
    end = _sc._parse(end_raw)
    if start and not end:
        end = start + datetime.timedelta(hours=_DEFAULT_EVENT_HOURS)
    out = {
        "id": row.get("id"),
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "all_day": 1 if row.get("all_day") else 0,
        "location": (row.get("location") or "").strip() or None,
    }
    ident = _sc.occ_identity(row)
    if ident is not None:
        out["occ"] = ident
    return out


def pair_fingerprint(a: dict, b: dict) -> str:
    """Deterministic identity for the overlap between events `a` and `b`.

    Accepts stored rows or occurrence dicts. Sorted by event id so the argument
    order never matters. Includes each event's start, end, all_day and location:
    a change to any of those (a manual reschedule, a move to all-day, a venue
    change) yields a different fingerprint, which is exactly how a kept decision
    becomes stale and the recomputed overlap re-surfaces.
    """
    items = sorted((_se(a), _se(b)), key=lambda x: (x["id"] or ""))
    return request_fingerprint({"t": CONFLICT_TYPE_EVENT_OVERLAP, "items": items})


# --------------------------------------------------------------------------- #
# resolution generator  (deterministic; no AI, no fabricated times)
# --------------------------------------------------------------------------- #
def propose_shift(
    a_row: dict,
    b_row: dict,
    other_intervals: Optional[list[tuple[str, str]]] = None,
) -> Optional[dict]:
    """Return the one safe reschedule for an event↔event overlap, or ``None``.

    Rule: keep the earlier-starting event where it is; move the later-starting
    event so it begins exactly when the earlier one ends, preserving the moved
    event's original duration. Returns ``None`` (Accept is then unavailable and
    the UI offers Keep only) when:

      * either event is all-day, cancelled, or has an unparseable start;
      * the two do not actually overlap;
      * the moved slot would not satisfy ``end > start``;
      * the moved slot would overlap any interval in ``other_intervals``
        (the owner's other timed, non-cancelled events) — i.e. Accept would
        knowingly create a new collision.
    """
    a, b = dict(a_row), dict(b_row)
    if a.get("all_day") or b.get("all_day"):
        return None
    if (a.get("status") or "confirmed") == "cancelled" or (b.get("status") or "confirmed") == "cancelled":
        return None

    a_start = _sc._parse(a.get("start") or a.get("start_time"))
    b_start = _sc._parse(b.get("start") or b.get("start_time"))
    if not a_start or not b_start:
        return None
    a_end = _sc._parse(a.get("end") or a.get("end_time")) or (a_start + datetime.timedelta(hours=_DEFAULT_EVENT_HOURS))
    b_end = _sc._parse(b.get("end") or b.get("end_time")) or (b_start + datetime.timedelta(hours=_DEFAULT_EVENT_HOURS))

    if not _sc.intervals_overlap(a_start.isoformat(), a_end.isoformat(),
                                 b_start.isoformat(), b_end.isoformat()):
        return None

    if a_start <= b_start:
        earlier_end, mover, mv_start, mv_end = a_end, b, b_start, b_end
    else:
        earlier_end, mover, mv_start, mv_end = b_end, a, a_start, a_end

    duration = mv_end - mv_start
    if duration <= datetime.timedelta(0):
        return None
    new_start = earlier_end
    new_end = new_start + duration
    if new_end <= new_start:
        return None

    for os_iso, oe_iso in (other_intervals or []):
        if _sc.intervals_overlap(new_start.isoformat(), new_end.isoformat(), os_iso, oe_iso):
            return None

    out = {
        "event_id": mover.get("id"),
        "event_title": mover.get("title"),
        "current_start": mv_start.isoformat(),
        "current_end": mv_end.isoformat(),
        "start": new_start.isoformat(),
        "end": new_end.isoformat(),
    }
    if mover.get("is_recurring_instance"):
        # The move targets ONE occurrence of a recurring series — the resolution
        # is written as an occurrence override, never as a series edit.
        out["is_recurring_instance"] = True
        out["occurrence_date"] = mover.get("occurrence_date")
        out["scope"] = "occurrence"
    return out


# --------------------------------------------------------------------------- #
# explanation
# --------------------------------------------------------------------------- #
def _fmt_time(iso: Optional[str]) -> str:
    d = _sc._parse(iso)
    if not d:
        return "?"
    h = d.hour % 12 or 12
    ap = "am" if d.hour < 12 else "pm"
    return f"{h}:{d.minute:02d} {ap}" if d.minute else f"{h} {ap}"


def _overlap_minutes(a: dict, b: dict) -> int:
    a0 = _sc._parse(a.get("start") or a.get("start_time"))
    b0 = _sc._parse(b.get("start") or b.get("start_time"))
    a1 = _sc._parse(a.get("end") or a.get("end_time")) or (a0 + datetime.timedelta(hours=_DEFAULT_EVENT_HOURS) if a0 else None)
    b1 = _sc._parse(b.get("end") or b.get("end_time")) or (b0 + datetime.timedelta(hours=_DEFAULT_EVENT_HOURS) if b0 else None)
    if not (a0 and a1 and b0 and b1):
        return 0
    lo, hi = max(a0, b0), min(a1, b1)
    return max(0, int((hi - lo).total_seconds() // 60))


_WD = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MON = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def occurrence_date_label(iso_date: Optional[str]) -> Optional[str]:
    """'2026-09-21' -> 'Mon 21 Sep'. None-safe."""
    if not iso_date:
        return None
    try:
        d = datetime.date.fromisoformat(iso_date)
    except (TypeError, ValueError):
        return None
    return f"{_WD[d.weekday()]} {d.day} {_MON[d.month - 1]}"


def explain(a: dict, b: dict) -> str:
    """One concise sentence naming both items, their times, and the overlap.

    When one side is a recurring occurrence, the concrete occurrence date is
    named first so the owner knows exactly which day is affected.
    """
    at = a.get("title") or "An event"
    bt = b.get("title") or "another event"
    mins = _overlap_minutes(a, b)
    span_a = f"{_fmt_time(a.get('start') or a.get('start_time'))}–{_fmt_time(a.get('end') or a.get('end_time'))}"
    span_b = f"{_fmt_time(b.get('start') or b.get('start_time'))}–{_fmt_time(b.get('end') or b.get('end_time'))}"
    tail = f" — they overlap by {mins} min." if mins else " — their times overlap."
    occ_label = occurrence_date_label(
        a.get("occurrence_date") if a.get("is_recurring_instance")
        else (b.get("occurrence_date") if b.get("is_recurring_instance") else None)
    )
    lead = f"On {occ_label}: " if occ_label else ""
    return f"{lead}“{at}” ({span_a}) and “{bt}” ({span_b}) are on top of each other{tail}"
