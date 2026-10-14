"""CoreOS Planning & Schedule core (Phase 4).

Deterministic domain logic for the schedule engine:
  * MYT (Asia/Kuala_Lumpur) time handling  -> reused from finance_core
  * event interval overlap / conflict detection  (half-open [start, end))
  * bounded recurrence expansion within a query window
  * per-day density counts for the month calendar

The AI may explain or suggest; it is NEVER the authority on whether two
timestamps overlap or on what a plan contains. That authority lives here.
"""

import datetime
import re
from typing import Optional

from finance_core import MYT, myt_now, normalize_iso_datetime  # single tz strategy
import place_ref  # Phase 5G: provider-neutral linked-place snapshot (pure, no cycle)

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

RECURRENCE_KINDS = ("none", "daily", "weekly", "weekdays")
EVENT_STATUSES = ("confirmed", "cancelled")

# hard safety bound: a single query window can never expand more than this many
# recurring occurrences (protects the month/week endpoints).
MAX_WINDOW_DAYS = 62


# --------------------------------------------------------------------------- #
# time helpers
# --------------------------------------------------------------------------- #
def _parse(dt_str: Optional[str]) -> Optional[datetime.datetime]:
    if not dt_str:
        return None
    try:
        d = datetime.datetime.fromisoformat(str(dt_str).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=MYT)
    return d.astimezone(MYT)


def myt_day_bounds(date_str: str) -> tuple[str, str]:
    """'2026-09-04' -> ('2026-09-04T00:00:00+08:00', '2026-09-05T00:00:00+08:00')."""
    d = datetime.date.fromisoformat(date_str)
    start = datetime.datetime(d.year, d.month, d.day, tzinfo=MYT)
    return start.isoformat(), (start + datetime.timedelta(days=1)).isoformat()


def myt_month_bounds(year: int, month: int) -> tuple[str, str]:
    start = datetime.datetime(year, month, 1, tzinfo=MYT)
    end = (datetime.datetime(year + 1, 1, 1, tzinfo=MYT) if month == 12
           else datetime.datetime(year, month + 1, 1, tzinfo=MYT))
    return start.isoformat(), end.isoformat()


def myt_week_bounds(date_str: str) -> tuple[str, str]:
    """Monday 00:00 MYT .. next Monday 00:00 MYT for the week containing date_str."""
    d = datetime.date.fromisoformat(date_str)
    monday = d - datetime.timedelta(days=d.weekday())
    start = datetime.datetime(monday.year, monday.month, monday.day, tzinfo=MYT)
    return start.isoformat(), (start + datetime.timedelta(days=7)).isoformat()


def resolve_window_params(date_from, date_to) -> tuple[str, str]:
    """Resolve the GET /api/schedule ``date_from`` / ``date_to`` query params into
    a half-open ISO window ``[from_iso, to_iso)`` in Asia/Kuala_Lumpur.

    A **date-only** value (``YYYY-MM-DD``) is treated as a CALENDAR DATE in MYT —
    never routed through generic datetime normalization:

      * ``date_from`` -> that calendar day at ``00:00:00+08:00`` (inclusive start)
      * ``date_to``   -> the calendar day AFTER it at ``00:00:00+08:00``
        (so ``date_to`` is an *inclusive* user-facing end date backed by a
        half-open range, matching the rest of the CoreOS interval model)

    e.g. ``date_from=2026-09-07`` + ``date_to=2026-09-13`` ->
    ``[2026-09-07T00:00:00+08:00, 2026-09-14T00:00:00+08:00)``.

    A **full datetime** value is normalized to the MYT offset unchanged
    (``normalize_iso_datetime``), preserving the endpoint's datetime behavior.

    Raises ``ValueError`` on malformed input so the caller can return 422.
    """
    def _one(raw, *, is_end: bool) -> str:
        s = str(raw).strip()
        if _DATE_ONLY_RE.match(s):
            try:
                d = datetime.date.fromisoformat(s)
            except ValueError:
                raise ValueError("date must be a valid ISO calendar date (YYYY-MM-DD).")
            if is_end:
                d = d + datetime.timedelta(days=1)
            return datetime.datetime(d.year, d.month, d.day, tzinfo=MYT).isoformat()
        return normalize_iso_datetime(s, default_now=False)

    from_iso = _one(date_from, is_end=False)
    to_iso = _one(date_to, is_end=True)
    if datetime.datetime.fromisoformat(to_iso) <= datetime.datetime.fromisoformat(from_iso):
        raise ValueError("date_to must be on or after date_from.")
    return from_iso, to_iso


def clamp_window(from_iso: str, to_iso: str) -> tuple[datetime.datetime, datetime.datetime]:
    a = _parse(from_iso) or myt_now()
    b = _parse(to_iso) or (a + datetime.timedelta(days=1))
    if b <= a:
        b = a + datetime.timedelta(days=1)
    if (b - a).days > MAX_WINDOW_DAYS:
        b = a + datetime.timedelta(days=MAX_WINDOW_DAYS)
    return a, b


# --------------------------------------------------------------------------- #
# overlap / conflict
# --------------------------------------------------------------------------- #
def intervals_overlap(a_start, a_end, b_start, b_end) -> bool:
    """True iff [a_start, a_end) and [b_start, b_end) intersect.

    Touching endpoints do NOT overlap: 10:00-11:00 and 11:00-12:00 -> False.
    10:00-11:30 and 11:00-12:00 -> True.
    """
    a0, a1 = _parse(a_start), _parse(a_end)
    b0, b1 = _parse(b_start), _parse(b_end)
    if not (a0 and a1 and b0 and b1):
        return False
    if a1 <= a0 or b1 <= b0:
        return False
    return a0 < b1 and b0 < a1


def find_conflicts(occurrences: list[dict]) -> list[dict]:
    """Return a list of {event_ids: [id_a, id_b], a: <occ>, b: <occ>} for every
    overlapping pair. `occurrences` are dicts with start/end (+ id, all_day,
    status). Cancelled and all-day items are excluded from overlap checks."""
    active = [
        o for o in occurrences
        if o.get("status", "confirmed") != "cancelled"
        and not o.get("all_day")
        and o.get("start") and o.get("end")
    ]
    active.sort(key=lambda o: o["start"])
    out: list[dict] = []
    for i in range(len(active)):
        for j in range(i + 1, len(active)):
            if active[j]["start"] >= active[i]["end"]:
                break  # sorted -> no later item can overlap active[i]
            if intervals_overlap(active[i]["start"], active[i]["end"],
                                 active[j]["start"], active[j]["end"]):
                out.append({
                    "event_ids": sorted({active[i]["id"], active[j]["id"]}),
                    "a": active[i], "b": active[j],
                })
    return out


# --------------------------------------------------------------------------- #
# recurrence
# --------------------------------------------------------------------------- #
def _recurs_on(kind: str, base_date: datetime.date, day: datetime.date) -> bool:
    if day < base_date:
        return False
    if kind == "daily":
        return True
    if kind == "weekly":
        return (day - base_date).days % 7 == 0
    if kind == "weekdays":
        return day.weekday() < 5  # Mon-Fri
    return False


def occ_identity(occ: dict) -> Optional[dict]:
    """The canonical, deterministic identity fragment for a RECURRING occurrence
    (the RFC 5545 RECURRENCE-ID concept), or ``None`` for a non-recurring event.

    Used by the B3 overlap fingerprint and the B5 weather-hazard fingerprint so a
    per-occurrence decision is bound to exactly one occurrence of exactly one
    series version. It never uses display text — only the series id, the
    occurrence's default MYT calendar date, the recurrence rule + its end bound,
    and the series' base start instant.
    """
    if not occ.get("is_recurring_instance"):
        return None
    return {
        "series_id": occ.get("id"),
        "occ_date": occ.get("occurrence_date"),
        "rec": occ.get("recurrence"),
        "rec_until": occ.get("recurrence_until"),
        "series_start": occ.get("series_start"),
    }


def _override_times(ov: dict, fallback_duration: datetime.timedelta):
    """(start_dt, end_dt) for an occurrence-override row, or None if unusable."""
    s = _parse(ov.get("start") or ov.get("start_time"))
    if not s:
        return None
    e = _parse(ov.get("end") or ov.get("end_time")) or (s + fallback_duration)
    if e <= s:
        e = s + fallback_duration
    return s, e


def expand_event(
    row: dict,
    win_start: datetime.datetime,
    win_end: datetime.datetime,
    overrides: Optional[dict] = None,
) -> list[dict]:
    """Materialize an event row into concrete occurrences intersecting the window.

    Non-recurring events yield 0 or 1 occurrence. Recurring events yield one per
    matching day within [win_start, win_end) and on/before recurrence_until.
    Each occurrence carries the parent event id + occurrence_date; the stored
    row is never modified.

    ``overrides`` (B6) is an optional ``{occurrence_date -> {start, end, status}}``
    map ALREADY scoped to this series (``schedule_occurrence_overrides`` rows).
    A ``'moved'`` override replaces that one occurrence's start/end (its
    ``occurrence_date`` — the canonical key — is unchanged); a ``'skipped'``
    override omits it. The recurrence rule and every other occurrence are
    untouched.
    """
    start = _parse(row.get("start_time"))
    if not start:
        return []
    end = _parse(row.get("end_time")) or (start + datetime.timedelta(hours=1))
    kind = (row.get("recurrence") or "none").lower()
    if kind not in RECURRENCE_KINDS:
        kind = "none"

    place = place_ref.place_out(row)

    def _occ(occ_start, occ_end, canonical_date=None, overridden=False):
        return {
            "id": row["id"],
            "title": row.get("title"),
            "location": row.get("location"),
            "place": place,                       # Phase 5G: linked real place (or None)
            "notes": row.get("notes"),
            "all_day": bool(row.get("all_day")),
            "status": row.get("status") or "confirmed",
            "source_type": row.get("source_type") or "coreos",
            "external_id": row.get("external_id"),
            "related_task_id": row.get("related_task_id"),
            "recurrence": kind,
            "recurrence_until": row.get("recurrence_until"),
            "start": occ_start.isoformat(),
            "end": occ_end.isoformat(),
            "occurrence_date": (canonical_date
                                or occ_start.astimezone(MYT).date().isoformat()),
            "is_recurring_instance": kind != "none",
            "series_start": start.isoformat(),
            "overridden": bool(overridden),
        }

    if kind == "none":
        if end > win_start and start < win_end:
            return [_occ(start, end)]
        return []

    duration = end - start
    until = _parse(row.get("recurrence_until"))
    base_date = start.astimezone(MYT).date()
    day = max(win_start.astimezone(MYT).date(), base_date)
    last = (win_end - datetime.timedelta(seconds=1)).astimezone(MYT).date()
    if until:
        last = min(last, until.astimezone(MYT).date())

    ov_map = overrides or {}
    consumed: set[str] = set()
    out: list[dict] = []
    while day <= last:
        if _recurs_on(kind, base_date, day):
            dkey = day.isoformat()
            ov = ov_map.get(dkey)
            if ov is not None:
                consumed.add(dkey)
                if (ov.get("status") or "moved") == "skipped":
                    day += datetime.timedelta(days=1)
                    continue
                t = _override_times(ov, duration)
                if t is not None:
                    occ_start, occ_end = t
                    if occ_end > win_start and occ_start < win_end:
                        out.append(_occ(occ_start, occ_end, canonical_date=dkey, overridden=True))
                    day += datetime.timedelta(days=1)
                    continue
                # unusable override -> fall through to the default slot
            occ_start = datetime.datetime(
                day.year, day.month, day.day, start.hour, start.minute, start.second, tzinfo=MYT
            )
            occ_end = occ_start + duration
            if occ_end > win_start and occ_start < win_end:
                out.append(_occ(occ_start, occ_end, canonical_date=dkey))
        day += datetime.timedelta(days=1)

    # A 'moved' override whose DEFAULT day fell outside this window but whose new
    # time lands inside it: emit it once, only if that default day is a genuine
    # occurrence of this series version.
    for dkey, ov in ov_map.items():
        if dkey in consumed or (ov.get("status") or "moved") == "skipped":
            continue
        try:
            d = datetime.date.fromisoformat(dkey)
        except ValueError:
            continue
        if not _recurs_on(kind, base_date, d):
            continue
        if until and d > until.astimezone(MYT).date():
            continue
        t = _override_times(ov, duration)
        if t is None:
            continue
        occ_start, occ_end = t
        if occ_end > win_start and occ_start < win_end:
            out.append(_occ(occ_start, occ_end, canonical_date=dkey, overridden=True))
    return out


def build_agenda(
    event_rows: list[dict],
    win_start_iso: str,
    win_end_iso: str,
    overrides_by_series: Optional[dict] = None,
) -> tuple[list[dict], list[dict]]:
    """Return (occurrences, conflicts) for the window. Deterministic + tz-correct.

    ``overrides_by_series`` (B6): ``{series_event_id -> {occurrence_date -> ov}}``.
    """
    win_start, win_end = clamp_window(win_start_iso, win_end_iso)
    obs = overrides_by_series or {}
    occurrences: list[dict] = []
    for row in event_rows:
        row = dict(row)
        occurrences.extend(expand_event(row, win_start, win_end, obs.get(row["id"])))
    occurrences.sort(key=lambda o: (o["start"], o.get("title") or ""))
    return occurrences, find_conflicts(occurrences)


def day_counts(occurrences: list[dict], task_rows: list[dict]) -> dict[str, dict]:
    """Per-MYT-day density for the month calendar: {'2026-09-04': {'events': n, 'tasks': m}}."""
    counts: dict[str, dict] = {}
    for o in occurrences:
        if o.get("status") == "cancelled":
            continue
        d = o["occurrence_date"]
        counts.setdefault(d, {"events": 0, "tasks": 0})["events"] += 1
    for t in task_rows:
        anchor = t.get("planned_at") or t.get("due_date") or t.get("reminder_at")
        dt = _parse(anchor)
        if not dt:
            continue
        d = dt.astimezone(MYT).date().isoformat()
        counts.setdefault(d, {"events": 0, "tasks": 0})["tasks"] += 1
    return counts
