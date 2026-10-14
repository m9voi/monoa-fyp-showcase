"""CoreOS Finance core (Phase 3).

Single source of truth for:
  * money representation & arithmetic (integer sen — no float accumulation)
  * the canonical transaction category taxonomy + normalization
  * timezone-aware (Asia/Kuala_Lumpur) period boundaries
  * the ONE authoritative finance snapshot used by every Finance endpoint,
    the dashboard, and the AI spending summary.

Nothing here trusts client input for ownership; callers pass an already
authenticated user_id.
"""

import datetime
import math
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

import db as db_layer

# --------------------------------------------------------------------------- #
# money — everything internal is integer sen (1 MYR = 100 sen)
# --------------------------------------------------------------------------- #
MAX_MAJOR = Decimal("100000000.00")      # RM 100,000,000.00 hard ceiling per value
MAX_SEN = int(MAX_MAJOR * 100)


class MoneyError(ValueError):
    pass


def to_sen(value) -> int:
    """Parse a user/AI supplied major-unit amount into integer sen.

    Rejects NaN / Infinity / non-numeric / out-of-range. Rounds half-up to 2dp.
    """
    if isinstance(value, bool):  # bool is an int subclass — refuse it explicitly
        raise MoneyError("Amount must be a number.")
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise MoneyError("Amount must be a real, finite number.")
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise MoneyError("Amount must be a valid number.")
    if d.is_nan() or d.is_infinite():
        raise MoneyError("Amount must be a real, finite number.")
    if abs(d) > MAX_MAJOR:
        raise MoneyError("Amount is out of range.")
    try:
        sen = int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, OverflowError, ValueError):
        raise MoneyError("Amount is out of range.")
    if sen > MAX_SEN or sen < -MAX_SEN:
        raise MoneyError("Amount is out of range.")
    return sen


def from_sen(sen: int) -> float:
    """Sen -> major-unit float for the API boundary. Exactly 2dp, never -0.0."""
    sen = int(sen)
    if sen == 0:
        return 0.0
    return float(Decimal(sen) / Decimal(100))


def fmt(sen: int) -> str:
    """Sen -> '1,234.56' (no currency symbol)."""
    neg = sen < 0
    d = (Decimal(abs(int(sen))) / Decimal(100)).quantize(Decimal("0.01"))
    return ("-" if neg else "") + f"{d:,.2f}"


def row_amount_sen(row) -> int:
    """Authoritative per-row amount in sen: prefer amount_sen, fall back to amount."""
    v = None
    try:
        v = row["amount_sen"]
    except Exception:
        v = None
    if v is None:
        return to_sen(row["amount"] if row["amount"] is not None else 0)
    return int(v)


# --------------------------------------------------------------------------- #
# categories — one canonical list; AI / manual / charts all normalize to it
# --------------------------------------------------------------------------- #
CANONICAL_CATEGORIES = [
    "Food", "Transport", "Shopping", "Personal Care", "Bills",
    "Entertainment", "Health", "Education", "Travel", "Other",
]
_CAT_LOOKUP = {c.lower(): c for c in CANONICAL_CATEGORIES}
# a few common aliases the model / users produce
_CAT_ALIASES = {
    "groceries": "Food", "dining": "Food", "restaurant": "Food", "cafe": "Food",
    "transportation": "Transport", "commute": "Transport", "fuel": "Transport",
    "petrol": "Transport", "grab": "Transport", "taxi": "Transport",
    "shop": "Shopping", "retail": "Shopping",
    "utilities": "Bills", "utility": "Bills", "rent": "Bills", "subscription": "Bills",
    "entertain": "Entertainment", "movies": "Entertainment", "games": "Entertainment",
    "medical": "Health", "pharmacy": "Health", "fitness": "Health", "gym": "Health",
    "school": "Education", "tuition": "Education", "books": "Education",
    "trip": "Travel", "flight": "Travel", "hotel": "Travel",
    "misc": "Other", "miscellaneous": "Other", "general": "Other",
}

TRANSACTION_TYPES = ("expense", "income")


def normalize_category(raw) -> str:
    if not raw or not isinstance(raw, str):
        return "Other"
    key = raw.strip().lower()
    return _CAT_LOOKUP.get(key) or _CAT_ALIASES.get(key) or "Other"


def normalize_type(raw) -> str:
    if isinstance(raw, str) and raw.strip().lower() in TRANSACTION_TYPES:
        return raw.strip().lower()
    return "expense"


# --------------------------------------------------------------------------- #
# time — Malaysia is UTC+08:00, no DST. All period math is done in MYT.
# --------------------------------------------------------------------------- #
MYT = datetime.timezone(datetime.timedelta(hours=8))


def myt_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).astimezone(MYT)


def normalize_iso_datetime(raw, *, default_now: bool = True) -> str:
    """Return an ISO-8601 string in the MYT offset (+08:00).

    Accepts full datetimes, plain dates ('2026-09-01'), or 'now'/empty.
    Raises MoneyError-style ValueError on garbage so callers can 400 it.
    """
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        if default_now:
            return myt_now().isoformat()
        raise ValueError("A date is required.")
    if isinstance(raw, str) and raw.strip().lower() == "now":
        return myt_now().isoformat()
    s = str(raw).strip()
    # plain date -> midday MYT (avoids day-boundary ambiguity)
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        try:
            d = datetime.date.fromisoformat(s)
        except ValueError:
            raise ValueError("Date is not valid ISO-8601.")
        return datetime.datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=MYT).isoformat()
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Date is not valid ISO-8601.")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MYT)
    return dt.astimezone(MYT).isoformat()


def _period_bounds(now: datetime.datetime):
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + datetime.timedelta(days=1)
    week_start = day_start - datetime.timedelta(days=now.weekday())  # Monday
    week_end = week_start + datetime.timedelta(days=7)
    month_start = day_start.replace(day=1)
    if month_start.month == 12:
        month_end = month_start.replace(year=month_start.year + 1, month=1)
    else:
        month_end = month_start.replace(month=month_start.month + 1)
    return {
        "day": (day_start.isoformat(), day_end.isoformat()),
        "week": (week_start.isoformat(), week_end.isoformat()),
        "month": (month_start.isoformat(), month_end.isoformat()),
    }


# --------------------------------------------------------------------------- #
# authoritative snapshot
# --------------------------------------------------------------------------- #
def _iso_now() -> str:
    return db_layer.iso_now()


def ensure_financial_settings(conn, user_id: str) -> dict:
    row = conn.execute(
        "SELECT starting_balance, weekly_budget, monthly_budget, daily_budget "
        "FROM user_financial_settings WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO user_financial_settings "
            "(user_id, starting_balance, weekly_budget, monthly_budget, daily_budget, updated_at) "
            "VALUES (?, 0, 0, 0, 0, ?) ON CONFLICT(user_id) DO NOTHING",
            (user_id, _iso_now()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT starting_balance, weekly_budget, monthly_budget, daily_budget "
            "FROM user_financial_settings WHERE user_id = ?", (user_id,)
        ).fetchone()
    if row is None:
        return {"starting_balance_sen": 0, "weekly_sen": 0, "monthly_sen": 0, "daily_sen": 0}
    return {
        "starting_balance_sen": to_sen(row["starting_balance"] or 0),
        "weekly_sen": to_sen(row["weekly_budget"] or 0),
        "monthly_sen": to_sen(row["monthly_budget"] or 0),
        "daily_sen": to_sen(row["daily_budget"] or 0),
    }


def _pct(used_sen: int, budget_sen: int) -> float:
    if budget_sen <= 0:
        return 0.0
    return round(used_sen / budget_sen * 100, 1)


def get_snapshot(user_id: str) -> dict:
    """The one authoritative Finance computation. All money in sen internally;
    the returned dict exposes both major-unit floats (2dp) and *_sen ints."""
    conn = db_layer.get_connection()
    try:
        s = ensure_financial_settings(conn, user_id)
        rows = conn.execute(
            "SELECT amount, amount_sen, type, category, date FROM transactions WHERE user_id = ?",
            (user_id,),
        ).fetchall()

        bounds = _period_bounds(myt_now())
        (d0, d1) = bounds["day"]
        (w0, w1) = bounds["week"]
        (m0, m1) = bounds["month"]

        total_income = total_expense = 0
        today = week = month = 0
        cat_month: dict[str, int] = {}

        for r in rows:
            amt = row_amount_sen(r)
            typ = normalize_type(r["type"])
            dt = r["date"] or ""
            if typ == "income":
                total_income += amt
                continue
            total_expense += amt
            if d0 <= dt < d1:
                today += amt
            if w0 <= dt < w1:
                week += amt
            if m0 <= dt < m1:
                month += amt
                cat = normalize_category(r["category"])
                cat_month[cat] = cat_month.get(cat, 0) + amt

        available = s["starting_balance_sen"] + total_income - total_expense

        breakdown = sorted(
            ({"category": c, "amount": from_sen(v), "amount_sen": v,
              "pct": round(v / month * 100, 1) if month > 0 else 0.0}
             for c, v in cat_month.items()),
            key=lambda x: x["amount_sen"], reverse=True,
        )

        return {
            "currency": "MYR",
            "period_label": "This month",
            "starting_balance": from_sen(s["starting_balance_sen"]),
            "total_income": from_sen(total_income),
            "total_expenses": from_sen(total_expense),
            "available_balance": from_sen(available),
            "available_balance_sen": available,
            "daily_budget": from_sen(s["daily_sen"]),
            "weekly_budget": from_sen(s["weekly_sen"]),
            "monthly_budget": from_sen(s["monthly_sen"]),
            "today_spending": from_sen(today),
            "week_spending": from_sen(week),
            "month_spending": from_sen(month),
            "daily_remaining": from_sen(s["daily_sen"] - today),
            "weekly_remaining": from_sen(s["weekly_sen"] - week),
            "monthly_remaining": from_sen(s["monthly_sen"] - month),
            "daily_used_pct": _pct(today, s["daily_sen"]),
            "weekly_used_pct": _pct(week, s["weekly_sen"]),
            "monthly_used_pct": _pct(month, s["monthly_sen"]),
            "daily_over_budget": s["daily_sen"] > 0 and today > s["daily_sen"],
            "weekly_over_budget": s["weekly_sen"] > 0 and week > s["weekly_sen"],
            "monthly_over_budget": s["monthly_sen"] > 0 and month > s["monthly_sen"],
            "category_breakdown": breakdown,
        }
    finally:
        conn.close()
