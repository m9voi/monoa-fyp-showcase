"""B1.1 — the contextual Activity -> internal Map affordance.

Source-level guards. The URL/identity logic itself is unit-tested in
app/schedule/__tests__/placeLink.test.ts (npm run test:node). These checks lock
in that:
  * Activity results expose a real "View on map" control that opens the internal
    /map route (not a second map, not an external map, not a dead click);
  * it reuses the ONE shared focus-URL contract (buildFocusUrl / activityFocusUrl
    in app/schedule/placeLink.ts) with from=activity;
  * nothing on that path hardcodes coordinates / a city / a fake place;
  * Map is still NOT a primary navigation destination.
"""
import os
import pathlib

ROOT = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ACT = (ROOT / "app" / "activity" / "page.tsx").read_text(encoding="utf-8")
_LINK = (ROOT / "app" / "schedule" / "placeLink.ts").read_text(encoding="utf-8")
_MAP = (ROOT / "app" / "map" / "page.tsx").read_text(encoding="utf-8")
_NAV = (ROOT / "app" / "lib" / "nav.ts").read_text(encoding="utf-8")


def test_activity_results_expose_a_view_on_map_control():
    # a real, labelled control
    assert "View on map" in _ACT
    # a Next <Link> to an internal route (semantic anchor, client nav), not <a href="http...">
    assert 'import Link from "next/link"' in _ACT
    assert "href={mapHref}" in _ACT
    # accessible: explicit label + a >=44px target + a visible focus ring
    assert "aria-label={`View ${c.name} on the map`}" in _ACT
    assert "min-h-[44px]" in _ACT
    assert "focus-visible:outline" in _ACT


def test_view_on_map_reuses_the_shared_focus_contract_only():
    # built through the shared helper — no second map implementation, no bespoke URL
    assert "activityFocusUrl" in _ACT
    assert 'from "../schedule/placeLink"' in _ACT
    # the helper lives in the ONE bridge module and targets the internal /map route
    assert "export function activityFocusUrl" in _LINK
    assert '`/map?' in _LINK or '"/map?' in _LINK
    # it goes through buildFocusUrl with the activity source
    assert 'buildFocusUrl(ref, "activity")' in _LINK


def test_view_on_map_has_an_honest_absent_state_not_a_fallback():
    # the control renders ONLY when the helper returns a URL; otherwise nothing
    assert "const mapHref = activityFocusUrl(c, activityId);" in _ACT
    assert "{mapHref && (" in _ACT
    # the helper returns null on missing identity / invalid coords
    assert "): string | null {" in _LINK
    assert "return null;" in _LINK


def test_no_hardcoded_location_on_the_activity_to_map_path():
    # slice out just the activityFocusUrl helper body
    helper = _LINK[_LINK.index("export function activityFocusUrl") :]
    helper = helper[: helper.index("\n}") + 2]
    # coordinates come from the real provider result, never a constant / city
    assert "toPlaceRef(p)" in helper
    for banned in ("Cyberjaya", "cyberjaya", "latitude: 3.", "longitude: 101.", "= 3.1", "= 101.6"):
        assert banned not in helper
    # and the Activity page never invents a place for this control
    assert "Cyberjaya" not in _ACT


def test_map_focus_view_sends_activity_users_back_to_activity():
    assert "parseFocusSource" in _MAP
    assert 'source === "activity" ? "/activity" : "/schedule"' in _MAP
    assert 'source === "activity" ? "Back to activity" : "Back to schedule"' in _MAP
    # parseFocusRef now accepts the activity source too (contract widened, not forked)
    assert "if (parseFocusSource(params) === null) return null;" in _LINK


def test_map_is_still_not_a_primary_navigation_destination():
    # inspect the NAV_ITEMS array body only (the file's prose comment mentions /map)
    arr = _NAV[_NAV.index("export const NAV_ITEMS") : _NAV.index("];", _NAV.index("export const NAV_ITEMS"))]
    assert 'href: "/map"' not in arr
    assert [h for h in ("/", "/schedule", "/mo", "/activity", "/finance") if f'href: "{h}"' in arr] == [
        "/",
        "/schedule",
        "/mo",
        "/activity",
        "/finance",
    ]
