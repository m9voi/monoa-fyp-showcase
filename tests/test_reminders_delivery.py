"""B11.1 — real in-app DUE reminder delivery + persistent acknowledgement.

The scheduler still owns ONLY PENDING -> DUE. GET /api/reminders/due returns the
caller's own DUE, unacknowledged, incomplete reminders (empty when the account's
`notifications` preference is off — data untouched). POST /api/reminders/{id}/ack
persists reminder_status DUE -> ACKNOWLEDGED (owner-scoped, idempotent). There is
NO push / service worker / VAPID / FCM / APNs / email / SMS anywhere.

Temp DB via conftest — the real coreos.db is never touched.
"""
import datetime

import scheduler
from conftest import new_authed_client


def _iso(dt: datetime.datetime) -> str:
    return dt.astimezone(datetime.timezone(datetime.timedelta(hours=8))).isoformat()


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _make_due(tc, title="Call the dentist", *, past_minutes=30):
    """Create a task with a reminder in the past, then run the scheduler once so
    it is promoted PENDING -> DUE exactly as in production."""
    r = tc.post("/api/tasks", json={
        "title": title,
        "reminder_at": _iso(_now() - datetime.timedelta(minutes=past_minutes)),
    })
    assert r.status_code in (200, 201), r.text
    task = r.json()["task"]
    assert task["reminder_status"] == "PENDING"
    promoted = scheduler.scan_due_reminders_once()
    assert promoted >= 1
    return task["id"]


def _due_titles(tc):
    b = tc.get("/api/reminders/due")
    assert b.status_code == 200, b.text
    j = b.json()
    return j, [x["title"] for x in j["reminders"]]


# --------------------------------------------------------------------------- #
# delivery + ownership
# --------------------------------------------------------------------------- #
def test_due_reminder_appears_for_its_owner_only():
    ac, _ = new_authed_client("rem_owner@example.com")
    bc, _ = new_authed_client("rem_other@example.com")
    tid = _make_due(ac, "Owner reminder")

    j, titles = _due_titles(ac)
    assert titles == ["Owner reminder"]
    assert j["notifications_enabled"] is True
    row = j["reminders"][0]
    assert row["task_id"] == tid and row["reminder_at"] and "reminder_at" in row

    # the other account sees nothing of it
    _, other_titles = _due_titles(bc)
    assert "Owner reminder" not in other_titles


def test_requires_auth():
    from fastapi.testclient import TestClient
    import main
    tc = TestClient(main.app)
    assert tc.get("/api/reminders/due").status_code == 401
    assert tc.post("/api/reminders/anything/ack").status_code == 401


def test_other_account_cannot_acknowledge_it():
    ac, _ = new_authed_client("rem_a1@example.com")
    bc, _ = new_authed_client("rem_b1@example.com")
    tid = _make_due(ac)

    assert bc.post(f"/api/reminders/{tid}/ack").status_code == 404      # not yours -> 404
    # still delivered to the real owner, untouched
    _, titles = _due_titles(ac)
    assert titles and titles[0] == "Call the dentist"


# --------------------------------------------------------------------------- #
# acknowledgement: persistent, idempotent, does not reappear
# --------------------------------------------------------------------------- #
def test_acknowledgement_persists_and_does_not_reappear():
    ac, user = new_authed_client("rem_ack@example.com")
    tid = _make_due(ac)

    assert ac.post(f"/api/reminders/{tid}/ack").status_code == 200
    _, titles = _due_titles(ac)
    assert titles == []                                                # gone from delivery

    # a fresh session ("restart" / re-login) for the same account still sees it
    # acknowledged — the state is on the task row, not in memory.
    from fastapi.testclient import TestClient
    import main
    ac2 = TestClient(main.app)
    assert ac2.post("/api/auth/login", json={"email": "rem_ack@example.com", "password": "password123"}).status_code == 200
    _, titles2 = _due_titles(ac2)
    assert titles2 == []

    # the scheduler re-running never re-promotes an ACKNOWLEDGED reminder
    scheduler.scan_due_reminders_once()
    _, titles3 = _due_titles(ac2)
    assert titles3 == []


def test_repeated_acknowledgement_is_idempotent():
    ac, _ = new_authed_client("rem_idem@example.com")
    tid = _make_due(ac)
    for _ in range(4):
        assert ac.post(f"/api/reminders/{tid}/ack").status_code == 200
    _, titles = _due_titles(ac)
    assert titles == []


def test_acknowledging_a_non_due_task_is_a_successful_noop():
    ac, _ = new_authed_client("rem_notdue@example.com")
    # a task with a FUTURE reminder is PENDING, never delivered
    r = ac.post("/api/tasks", json={
        "title": "Future", "reminder_at": _iso(_now() + datetime.timedelta(hours=6)),
    })
    tid = r.json()["task"]["id"]
    assert ac.post(f"/api/reminders/{tid}/ack").status_code == 200     # idempotent no-op
    _, titles = _due_titles(ac)
    assert titles == []


# --------------------------------------------------------------------------- #
# complete / delete / edit / reschedule / uncomplete
# --------------------------------------------------------------------------- #
def test_completing_the_task_removes_it_from_delivery():
    ac, _ = new_authed_client("rem_complete@example.com")
    tid = _make_due(ac)
    assert ac.post(f"/api/tasks/{tid}/complete").status_code == 200
    _, titles = _due_titles(ac)
    assert titles == []


def test_deleting_the_task_removes_it_from_delivery():
    ac, _ = new_authed_client("rem_delete@example.com")
    tid = _make_due(ac)
    assert ac.delete(f"/api/tasks/{tid}").status_code == 200
    _, titles = _due_titles(ac)
    assert titles == []


def test_uncompleting_a_task_with_a_past_reminder_recomputes_to_due():
    ac, _ = new_authed_client("rem_uncomplete@example.com")
    tid = _make_due(ac, "Water plants")
    ac.post(f"/api/reminders/{tid}/ack")                # acknowledged
    ac.post(f"/api/tasks/{tid}/complete")               # -> reminder_status NONE
    _, titles = _due_titles(ac)
    assert titles == []
    # uncompleting recomputes truthfully: the reminder time is in the past -> DUE
    assert ac.post(f"/api/tasks/{tid}/uncomplete").status_code == 200
    _, titles2 = _due_titles(ac)
    assert titles2 == ["Water plants"]                  # a fresh, truthful DUE episode


def test_editing_the_reminder_time_clears_acknowledgement_and_re_promotes():
    ac, _ = new_authed_client("rem_edit@example.com")
    tid = _make_due(ac, "Submit form")
    ac.post(f"/api/reminders/{tid}/ack")
    _, titles = _due_titles(ac)
    assert titles == []

    # reschedule to another PAST time -> reminder_status resets to PENDING
    r = ac.patch(f"/api/tasks/{tid}", json={
        "reminder_at": _iso(_now() - datetime.timedelta(minutes=5)),
    })
    assert r.status_code == 200 and r.json()["task"]["reminder_status"] == "PENDING"
    _, mid = _due_titles(ac)
    assert mid == []                                    # PENDING is not delivered yet

    scheduler.scan_due_reminders_once()                 # promote the new time
    _, after = _due_titles(ac)
    assert after == ["Submit form"]                     # re-delivered, no longer acknowledged


def test_clearing_the_reminder_removes_it_entirely():
    ac, _ = new_authed_client("rem_clear@example.com")
    tid = _make_due(ac, "Old reminder")
    r = ac.patch(f"/api/tasks/{tid}", json={"reminder_at": None})
    assert r.status_code == 200 and r.json()["task"]["reminder_status"] == "NONE"
    _, titles = _due_titles(ac)
    assert titles == []


# --------------------------------------------------------------------------- #
# notifications preference gates PRESENTATION only (data untouched)
# --------------------------------------------------------------------------- #
def test_notifications_off_hides_the_surface_without_deleting_data():
    ac, _ = new_authed_client("rem_prefoff@example.com")
    tid = _make_due(ac, "Keep me")

    # turn the in-app reminders preference OFF
    assert ac.post("/api/settings/update", json={
        "notifications": False, "location_services": True, "appearance_mode": "system",
    }).status_code == 200

    j, titles = _due_titles(ac)
    assert titles == [] and j["notifications_enabled"] is False        # suppressed

    # the DUE row is still there — turn the preference back ON
    assert ac.post("/api/settings/update", json={
        "notifications": True, "location_services": True, "appearance_mode": "system",
    }).status_code == 200
    j2, titles2 = _due_titles(ac)
    assert titles2 == ["Keep me"] and j2["notifications_enabled"] is True
    assert j2["reminders"][0]["task_id"] == tid


# --------------------------------------------------------------------------- #
# NO background-push claim / dependency anywhere
# --------------------------------------------------------------------------- #
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_BELL = (_ROOT / "app" / "components" / "RemindersBell.tsx").read_text(encoding="utf-8")
_CHROME = (_ROOT / "app" / "components" / "AppChrome.tsx").read_text(encoding="utf-8")
_NAV = (_ROOT / "app" / "lib" / "nav.ts").read_text(encoding="utf-8")


def test_no_background_push_claim_or_dependency():
    scheduler_src = (_ROOT / "scheduler.py").read_text(encoding="utf-8")
    for banned in ("web-push", "webpush", "pywebpush", "firebase", "onesignal",
                   "apns", "fcm ", "vapid", "serviceworker", "service worker"):
        assert banned not in scheduler_src.lower()
    assert "no background" in _BELL.lower() or "closed-app" in _BELL.lower()
    for banned in ("PushManager", "serviceWorker", "Notification.requestPermission",
                   "showNotification", "navigator.push", "workbox"):
        assert banned not in _BELL
    pkg = (_ROOT / "package.json").read_text(encoding="utf-8")
    for banned in ("web-push", "pywebpush", "firebase", "onesignal", "next-pwa", "workbox"):
        assert banned not in pkg.lower()


def test_reminders_surface_is_global_chrome_not_a_nav_tab():
    # rendered once, inside AuthGuard, next to the avatar — not a route/tab
    assert "import RemindersBell" in _CHROME and "<RemindersBell />" in _CHROME
    assert "/reminders" not in _NAV                       # never a navigation destination
    arr = _NAV[_NAV.index("export const NAV_ITEMS") : _NAV.index("];", _NAV.index("export const NAV_ITEMS"))]
    assert "reminder" not in arr.lower()


def test_reminders_bell_is_accessible_and_meaningful_only():
    # renders nothing unless there is at least one reminder AND the pref is on
    assert "if (!authed || !show) return null;" in _BELL
    assert "shouldShowReminders(" in _BELL
    # semantic button + dialog, keyboard + SR support, 44x44 targets
    assert 'type="button"' in _BELL and 'role="dialog"' in _BELL and 'aria-modal="true"' in _BELL
    assert 'aria-haspopup="dialog"' in _BELL and "aria-labelledby=" in _BELL
    assert 'aria-live="polite"' in _BELL
    assert 'e.key === "Escape"' in _BELL                   # keyboard dismiss
    assert "min-h-[44px]" in _BELL and "h-11 w-11" in _BELL
    assert "focus-visible:outline" in _BELL
    assert "env(safe-area-inset-top)" in _BELL             # safe-area aware
    # positioned clear of the avatar (avatar is right-3 / 40px wide)
    assert "right-[3.75rem]" in _BELL
    # truthful acknowledge action, persisted server-side (idempotent endpoint)
    assert "/api/reminders/" in _BELL and "/ack" in _BELL
    # no fake "push enabled" language
    for banned in ("push enabled", "Push enabled", "notifications enabled",
                   "background notification", "even when closed"):
        assert banned not in _BELL
