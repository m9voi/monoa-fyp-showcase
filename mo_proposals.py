"""CoreOS / MONOA — Mo human-in-the-loop proposal core (B2).

PURE module: registry, TTL policy, state predicates and safe display/formatting.
No DB, no FastAPI, no `main` import — unit-testable in isolation. The durable
row lifecycle and the canonical domain execution live in main.py (which already
owns AppServices / _validate_* / the idempotency helpers), exactly like
execute_action does today.

Contract (see main.py for the endpoints):

  POST /api/process-input            -> understand only. Never mutates domain data.
      kinds: answer | proposal_required | clarification_required | unsupported | error
  POST /api/process-input/{id}/confirm  -> execute the stored proposal exactly once
                                           through the SAME canonical service Plan/Wallet use.
  POST /api/process-input/{id}/cancel   -> mark cancelled; can never execute afterwards.
  GET  /api/process-input/pending       -> the caller's own actionable proposal, if any.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json as _json
import uuid
from typing import Any, Optional

# --------------------------------------------------------------------------- #
# policy
# --------------------------------------------------------------------------- #

# A pending proposal lapses after this. Lazily enforced on every read / confirm
# (no background job). Long enough to read + decide, short enough that a stale
# proposal never silently executes later.
PROPOSAL_TTL_SECONDS = 30 * 60

# Bounded "show the final result on Today / Mo" window. After a confirmed
# proposal is older than this it stops being surfaced by GET /pending, so an old
# executed result can never permanently dominate the OmniBar — Wallet / Plan
# remain the source of truth for the row itself.
EXECUTED_VISIBLE_SECONDS = 10 * 60

# B8.1 — bounded "show the truthful stale acknowledgement" window. A mutating
# suggestion whose proposal went STALE (the task changed / completed / was
# deleted / lost its slot / passed its deadline) is surfaced by GET /pending for
# this long so a refresh restores the honest "no longer current, nothing changed"
# card instead of silently vanishing. After the window it stops being surfaced.
# Short — it is only an acknowledgement, never an actionable proposal.
STALE_VISIBLE_SECONDS = 2 * 60

# Which stored statuses a confirm may still act on (idempotently). "executing"
# and "failed" are included so a crash / a transient DB error mid-confirm can be
# recovered by pressing Confirm again — the canonical service's
# (user_id, exec_token) uniqueness guarantees at-most-one row regardless.
# "stale" is NOT here: it is a terminal, non-user-caused state (see STATUS_MODEL).
CONFIRM_ALLOWED_FROM = ("pending", "executing", "failed")

# The full mo_proposals.status vocabulary (mo_proposals.status is unconstrained
# TEXT — migration 0012 adds no CHECK — so "stale" is added as a value only, no
# migration):
#   pending    awaiting the user's Confirm / Cancel
#   executing  a Confirm is in flight
#   executed   applied exactly once via a canonical service (terminal)
#   cancelled  the USER explicitly cancelled it (terminal); copy may say so
#   expired    the 30-min TTL lapsed while pending (terminal)
#   failed     execution hit a transient error; Confirm may be retried
#   stale      (B8.1) the underlying data no longer matches the suggestion —
#              task edited / completed / deleted / slot taken / past deadline.
#              Terminal, NOT user-caused, NOT retryable. Confirm on a stale row
#              returns kind:"stale" every time, never executes, never builds a
#              replacement. Copy must say "no longer current / nothing changed"
#              and must never say the user cancelled it.
STATUS_MODEL = ("pending", "executing", "executed", "cancelled", "expired", "failed", "stale")

# B2 create actions + B8 mutating-suggestion actions on an EXISTING owned task
# + edit/delete actions on an existing owned task or event (Mo capability
# closure — update/delete reuse the SAME canonical column-update / delete path
# the manual Plan UI uses; see _mo_exec_update_task / _mo_exec_delete_task /
# _mo_exec_update_event / _mo_exec_delete_event in main.py).
ACTION_TYPES = (
    "create_transaction", "create_task", "create_event",
    "schedule_task_block", "complete_task",
    "update_task", "delete_task", "update_event", "delete_event",
    "batch_operations",
)
_RESULT_KIND = {
    "create_transaction": "transaction",
    "create_task": "task",
    "create_event": "event",
    "schedule_task_block": "task",
    "complete_task": "task",
    "update_task": "task",
    "delete_task": "task",
    "update_event": "event",
    "delete_event": "event",
    "batch_operations": "batch",
}


def result_kind_for(action_type: str) -> str:
    return _RESULT_KIND[action_type]


# --------------------------------------------------------------------------- #
# B8 — mutating suggested actions
# --------------------------------------------------------------------------- #
# Mo may, AFTER a real schedule-summary answer, surface a SMALL curated set of
# contextual mutating recommendations. Each is server-computed, owner-bound and
# short-lived, and is carried to the browser only as an OPAQUE SIGNED TOKEN
# (HMAC-SHA256 over the deployment session secret). The browser echoes the token
# back on click; it can never author the task id, the times, or the action type.
# Selecting a suggestion builds a normal B2 proposal — nothing mutates until the
# user Confirms, through the SAME canonical task services the manual UI uses.
MUTATING_SUGGESTION_KINDS = ("schedule_task_block", "complete_task")

# The suggestion token lifetime. Deliberately short — a stale click just asks the
# user to try again; the resulting proposal keeps its own 30-minute TTL.
SUGGESTION_TOKEN_TTL_SECONDS = 10 * 60

# Default focused-work block a "Block time for <task>" suggestion proposes.
TASK_BLOCK_DEFAULT_MINUTES = 45

# Hard cap on mutating pills in one answer (prefer 1 primary).
MAX_MUTATING_SUGGESTIONS = 2

_TOKEN_VERSION = 1


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(txt: str) -> bytes:
    pad = "=" * (-len(txt) % 4)
    return base64.urlsafe_b64decode(txt + pad)


def new_suggestion_nonce() -> str:
    return uuid.uuid4().hex


def sign_suggestion(
    *,
    kind: str,
    user_id: str,
    task_id: str,
    now_iso: str,
    secret: str,
    nonce: Optional[str] = None,
    block_minutes: Optional[int] = None,
    target_date: Optional[str] = None,
) -> tuple[str, str]:
    """Return ``(opaque_token, nonce)`` for an allow-listed mutating suggestion.

    The token is ``<b64url(json payload)>.<b64url(hmac_sha256)>``. The payload
    carries the action kind, the owner id, the target task id, an expiry instant
    and a random nonce (which also becomes the proposal's ``client_token`` so a
    double-click collapses to one proposal). ``block_minutes`` is only set for
    ``schedule_task_block``.

    ``target_date`` (context fidelity — "MO INTERACTION + ACTION UX CLOSURE"
    P0 fix): the conversational date this suggestion was generated FOR (e.g.
    the "tomorrow" the user was just discussing when a schedule summary
    produced this "Block 45 min for X" suggestion), YYYY-MM-DD, ``schedule_task_block``
    only. Carried in the signed token itself — never re-derived from
    short-term mo_context at click time, which may have already expired or
    moved on to a different topic by then — so clicking the suggestion later
    still targets the exact day it was offered for, never silently "today".
    Optional: a suggestion built with no active date context (e.g. a plain
    "what's my schedule" with no explicit day) omits it, and candidate-slot
    selection falls back to its existing today-anchored default.
    """
    if kind not in MUTATING_SUGGESTION_KINDS:
        raise ValueError(f"not an allow-listed mutating suggestion: {kind!r}")
    n = nonce or new_suggestion_nonce()
    exp = (_parse(now_iso) + datetime.timedelta(seconds=SUGGESTION_TOKEN_TTL_SECONDS)).isoformat()
    payload: dict[str, Any] = {"v": _TOKEN_VERSION, "k": kind, "u": user_id,
                               "t": task_id, "exp": exp, "n": n}
    if kind == "schedule_task_block":
        payload["d"] = int(block_minutes or TASK_BLOCK_DEFAULT_MINUTES)
        if target_date:
            payload["td"] = target_date
    body = _b64u_encode(_json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    sig = _b64u_encode(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}", n


def verify_suggestion(token: str, *, secret: str, now_iso: str) -> Optional[dict]:
    """Validate an opaque suggestion token. Returns the payload dict when the
    signature matches, the version + kind are known, and it has not expired;
    otherwise ``None``. NEVER raises on malformed input. Ownership (payload["u"]
    == session user) is checked by the caller."""
    if not token or not isinstance(token, str) or token.count(".") != 1:
        return None
    body, sig = token.split(".", 1)
    try:
        expect = _b64u_encode(
            hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
        )
    except Exception:
        return None
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        payload = _json.loads(_b64u_decode(body).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("v") != _TOKEN_VERSION or payload.get("k") not in MUTATING_SUGGESTION_KINDS:
        return None
    for key in ("u", "t", "n", "exp"):
        if not payload.get(key):
            return None
    exp, now = _parse(payload["exp"]), _parse(now_iso)
    if exp is None or now is None or now >= exp:
        return None
    return payload


# --------------------------------------------------------------------------- #
# suggested-action registry — ALLOW-LISTED navigation only
# --------------------------------------------------------------------------- #
# Every entry is a real, existing authenticated route (verified in B1's shared
# navigation spine). Mo may only ever offer keys from this map, so a rendered
# suggestion can never be a dead pill. A suggested *mutating* action is out of
# scope for B2 — it would itself have to come back through this proposal flow and
# require its own confirmation; B2 ships navigation suggestions only.
SUGGESTION_REGISTRY: dict[str, dict[str, str]] = {
    "open_today": {"label": "Open Today", "route": "/"},
    "open_plan": {"label": "Open Plan", "route": "/schedule"},
    "open_wallet": {"label": "Open Wallet", "route": "/finance"},
    "open_activity": {"label": "Open Activity", "route": "/activity"},
}


def resolve_suggestions(keys: Optional[list[str]]) -> list[dict[str, str]]:
    """Map suggestion keys to `{key,label,route}`. Unknown keys are dropped, so a
    caller can never emit a suggestion that doesn't resolve to a real route."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for k in keys or []:
        if k in SUGGESTION_REGISTRY and k not in seen:
            seen.add(k)
            out.append({"key": k, **SUGGESTION_REGISTRY[k]})
    return out


# Contextual default suggestions per response kind / action type.
def default_suggestions(kind: str, action_type: Optional[str] = None) -> list[str]:
    if kind == "proposal_required":
        return {
            "create_transaction": ["open_wallet"],
            "create_task": ["open_plan"],
            "create_event": ["open_plan"],
            "schedule_task_block": ["open_plan"],
            "complete_task": ["open_plan"],
            "update_task": ["open_plan"],
            "delete_task": ["open_plan"],
            "update_event": ["open_plan"],
            "delete_event": ["open_plan"],
            "batch_operations": ["open_plan"],
        }.get(action_type or "", [])
    if kind == "answer":
        return []  # the caller passes a specific pair (spending -> wallet, schedule -> plan)
    if kind in ("unsupported", "error"):
        return ["open_today", "open_plan", "open_wallet", "open_activity"]
    return []


# --------------------------------------------------------------------------- #
# expiry / state
# --------------------------------------------------------------------------- #
def _parse(iso: str) -> Optional[datetime.datetime]:
    try:
        return datetime.datetime.fromisoformat(iso)
    except Exception:
        return None


def is_expired(status: str, expires_at: str, now_iso: str) -> bool:
    """A proposal is effectively expired when it is still pending but past its
    expiry instant. `executing` is NOT auto-expired (a confirm is in flight)."""
    if status != "pending":
        return False
    exp, now = _parse(expires_at), _parse(now_iso)
    if exp is None or now is None:
        return False
    return now >= exp


def executed_still_visible(confirmed_at: Optional[str], now_iso: str) -> bool:
    if not confirmed_at:
        return False
    c, now = _parse(confirmed_at), _parse(now_iso)
    if c is None or now is None:
        return False
    return (now - c).total_seconds() <= EXECUTED_VISIBLE_SECONDS


def stale_still_visible(resolved_at: Optional[str], now_iso: str) -> bool:
    """B8.1 — whether a proposal that went STALE is still inside its bounded
    acknowledgement window. `resolved_at` is the instant it became stale (stored
    in `confirmed_at`). Outside the window GET /pending stops surfacing it."""
    if not resolved_at:
        return False
    c, now = _parse(resolved_at), _parse(now_iso)
    if c is None or now is None:
        return False
    return 0 <= (now - c).total_seconds() <= STALE_VISIBLE_SECONDS


# Generic, reason-free stale copy — used when the exact cause should not be
# exposed or a stored reason is missing.
STALE_GENERIC_MESSAGE = "This suggestion is no longer current. Nothing was changed."


# --------------------------------------------------------------------------- #
# safe display / effect text (never a secret, never raw location data)
# --------------------------------------------------------------------------- #
_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _fmt_day(iso: Optional[str]) -> Optional[str]:
    d = _parse(iso) if iso else None
    if d is None:
        return None
    return f"{d.day} {_MONTHS[d.month]}"


def _fmt_time(iso: Optional[str]) -> Optional[str]:
    d = _parse(iso) if iso else None
    if d is None:
        return None
    h = d.hour % 12 or 12
    return f"{h}:{d.minute:02d} {'am' if d.hour < 12 else 'pm'}"


def _fmt_money(amount_sen: Optional[int]) -> Optional[str]:
    if amount_sen is None:
        return None
    return f"RM {amount_sen / 100:.2f}"


def _fmt_duration(start_iso: Optional[str], end_iso: Optional[str]) -> Optional[str]:
    s, e = _parse(start_iso) if start_iso else None, _parse(end_iso) if end_iso else None
    if s is None or e is None or e <= s:
        return None
    mins = int((e - s).total_seconds() // 60)
    h, m = divmod(mins, 60)
    if h and m:
        return f"{h} hr {m} min"
    if h:
        return f"{h} hr"
    return f"{m} min"


def build_display(action_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Safe, human display fields for the proposal card — derived only from the
    already-validated payload. No ids, no tokens, no free-form location strings
    beyond the user's own event location."""
    if action_type == "create_transaction":
        return {
            "kind": "transaction",
            "amount": _fmt_money(payload.get("amount_sen")),
            "type": payload.get("type", "expense"),
            "category": payload.get("category", "Other"),
            "description": payload.get("description") or None,
            "date": _fmt_day(payload.get("date")),
        }
    if action_type == "create_task":
        return {
            "kind": "reminder" if payload.get("is_reminder") or payload.get("reminder_at") else "task",
            "title": payload.get("title"),
            "due": _fmt_day(payload.get("due_date")),
            "remind_at": (_fmt_day(payload.get("reminder_at")) or "") and
                         f"{_fmt_day(payload.get('reminder_at'))} {_fmt_time(payload.get('reminder_at')) or ''}".strip(),
        }
    if action_type == "create_event":
        return {
            "kind": "event",
            "title": payload.get("title"),
            "date": _fmt_day(payload.get("start_time")),
            "time": None if payload.get("all_day") else _fmt_time(payload.get("start_time")),
            "all_day": bool(payload.get("all_day")),
            "location": payload.get("location") or None,
        }
    if action_type == "schedule_task_block":
        s, e = payload.get("planned_at"), payload.get("scheduled_end")
        return {
            "kind": "schedule_task_block",
            "title": payload.get("task_title"),
            "date": _fmt_day(s),
            "time": f"{_fmt_time(s)} – {_fmt_time(e)}" if (_fmt_time(s) and _fmt_time(e)) else None,
            "duration": _fmt_duration(s, e),
            "deadline": _fmt_day(payload.get("due_date")),
            "deadline_unchanged": True,
        }
    if action_type == "complete_task":
        return {
            "kind": "complete_task",
            "title": payload.get("task_title"),
        }
    if action_type == "update_task":
        f = payload.get("fields") or {}
        out = {"kind": "update_task", "title": payload.get("task_title")}
        if "title" in f:
            out["new_title"] = f.get("title")
        if "due_date" in f:
            out["new_due"] = _fmt_day(f.get("due_date"))
        if "reminder_at" in f:
            out["new_remind_at"] = _fmt_day(f.get("reminder_at"))
        if "planned_at" in f:
            out["new_planned_at"] = _fmt_day(f.get("planned_at"))
        return out
    if action_type == "delete_task":
        return {"kind": "delete_task", "title": payload.get("task_title")}
    if action_type == "update_event":
        f = payload.get("fields") or {}
        out = {"kind": "update_event", "title": payload.get("event_title")}
        if "title" in f:
            out["new_title"] = f.get("title")
        if "start_time" in f:
            out["new_date"] = _fmt_day(f.get("start_time"))
            out["new_time"] = _fmt_time(f.get("start_time"))
        if "location" in f:
            out["new_location"] = f.get("location")
        return out
    if action_type == "delete_event":
        return {"kind": "delete_event", "title": payload.get("event_title")}
    if action_type == "batch_operations":
        # payload["items"] are already safe, human-formatted display entries
        # built by main.py's per-operation resolver — no ids, no raw payload.
        return {"kind": "batch_operations", "items": payload.get("items") or []}
    return {"kind": "unknown"}


def build_effect(action_type: str, payload: dict[str, Any]) -> str:
    """One plain sentence: exactly what Confirm will do. No scary jargon."""
    if action_type == "create_transaction":
        amt = _fmt_money(payload.get("amount_sen")) or "an amount"
        cat = payload.get("category", "Other")
        kind = "income" if payload.get("type") == "income" else "expense"
        when = _fmt_day(payload.get("date"))
        tail = f", dated {when}" if when else ""
        return f"Add {amt} {kind} in {cat}{tail}."
    if action_type == "create_task":
        title = payload.get("title") or "a task"
        if payload.get("reminder_at"):
            w = f"{_fmt_day(payload.get('reminder_at'))} {_fmt_time(payload.get('reminder_at')) or ''}".strip()
            return f"Set a reminder to “{title}” for {w}."
        if payload.get("due_date"):
            return f"Add the task “{title}”, due {_fmt_day(payload.get('due_date'))}."
        return f"Add the task “{title}”."
    if action_type == "create_event":
        title = payload.get("title") or "an event"
        if payload.get("all_day"):
            return f"Add an all-day event “{title}” on {_fmt_day(payload.get('start_time'))}."
        return f"Add “{title}” on {_fmt_day(payload.get('start_time'))} at {_fmt_time(payload.get('start_time'))}."
    if action_type == "schedule_task_block":
        title = payload.get("task_title") or "your task"
        dur = _fmt_duration(payload.get("planned_at"), payload.get("scheduled_end")) or "some time"
        when = _fmt_day(payload.get("planned_at"))
        at = _fmt_time(payload.get("planned_at"))
        tail = f" on {when} at {at}" if (when and at) else ""
        # Deadline is explicitly NOT moved — only the work block is scheduled.
        return (f"Set aside {dur} to work on “{title}”{tail}. "
                f"This only schedules the work block — the deadline doesn’t change.")
    if action_type == "complete_task":
        title = payload.get("task_title") or "your task"
        return f"Mark the task “{title}” complete."
    if action_type == "update_task":
        title = payload.get("task_title") or "the task"
        f = payload.get("fields") or {}
        changes = []
        if "title" in f:
            changes.append(f"rename it to “{f.get('title')}”")
        if "due_date" in f:
            changes.append(f"set its due date to {_fmt_day(f.get('due_date')) or 'none'}")
        if "reminder_at" in f:
            changes.append(f"set its reminder to {_fmt_day(f.get('reminder_at')) or 'none'}")
        if "planned_at" in f:
            changes.append(f"set its planned time to {_fmt_day(f.get('planned_at')) or 'none'}")
        return f"Update “{title}”" + (f": {', '.join(changes)}." if changes else ".")
    if action_type == "delete_task":
        title = payload.get("task_title") or "the task"
        return f"Delete the task “{title}”. This cannot be undone."
    if action_type == "update_event":
        title = payload.get("event_title") or "the event"
        f = payload.get("fields") or {}
        changes = []
        if "title" in f:
            changes.append(f"rename it to “{f.get('title')}”")
        if "start_time" in f:
            changes.append(f"move it to {_fmt_day(f.get('start_time'))} at {_fmt_time(f.get('start_time'))}")
        if "location" in f:
            changes.append(f"set its location to {f.get('location') or 'none'}")
        return f"Update “{title}”" + (f": {', '.join(changes)}." if changes else ".")
    if action_type == "delete_event":
        title = payload.get("event_title") or "the event"
        return f"Delete the event “{title}”. This cannot be undone."
    return "Make a change in MONOA."


def build_summary(action_type: str) -> str:
    return {
        "create_transaction": "Got it — here’s the expense to log.",
        "create_task": "Got it — here’s the task to add.",
        "create_event": "Got it — here’s the event to add.",
        "schedule_task_block": "Here’s a time to block for this task.",
        "complete_task": "Here’s the task to mark complete.",
        "update_task": "Here’s the update to this task.",
        "delete_task": "Here’s the task to delete.",
        "update_event": "Here’s the update to this event.",
        "delete_event": "Here’s the event to delete.",
    }.get(action_type, "Here’s what I understood.")


# --------------------------------------------------------------------------- #
# the client-facing projection of a stored row (NEVER payload_json / exec_token /
# client_request_hash)
# --------------------------------------------------------------------------- #
def public_proposal(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "proposal_id": row["id"],
        "status": row["status"],
        "action_type": row["action_type"],
        "result_kind": row.get("result_kind"),
        "result_ref": row.get("result_ref"),
        "summary": row["summary"],
        "effect": row["effect"],
        "display": _load_json(row.get("display_json")),
        "expires_at": row["expires_at"],
        "confirmed_at": row.get("confirmed_at"),
        "client_token": row.get("client_token"),
        "exec_message": row.get("exec_message"),
        "failure_reason": row.get("failure_reason"),
    }


def _load_json(s: Optional[str]) -> Any:
    import json

    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception:
        return {}
