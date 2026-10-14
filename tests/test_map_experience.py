"""Phase 5D — Real Map Experience: source-level guards.

The interactive behaviour (marker/list selection sync, bounds, directions URL,
distance formatting, the search-origin model) is unit-tested in
app/map/__tests__/*.test.ts via `npm run test:node`. These checks lock in the
security / truthfulness / structure properties of the Map surface, including the
Phase 5D master-closure additions: an explicit search-origin model, real
geocoding for "Change location", race-token guards, and a universal
manual-location escape hatch.
"""
import json
import os
import pathlib
import re

ROOT = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MAP = (ROOT / "app" / "map" / "page.tsx").read_text(encoding="utf-8")
_CANVAS = (ROOT / "app" / "map" / "MapCanvas.tsx").read_text(encoding="utf-8")
_STATE = (ROOT / "app" / "map" / "mapState.ts").read_text(encoding="utf-8")
_ORIGIN = (ROOT / "app" / "map" / "searchOrigin.ts").read_text(encoding="utf-8")
_PANEL = (ROOT / "app" / "map" / "GeocodePanel.tsx").read_text(encoding="utf-8")
_LOC = (ROOT / "app" / "lib" / "location.ts").read_text(encoding="utf-8")
_DEVLOC = (ROOT / "app" / "map" / "deviceLoc.ts").read_text(encoding="utf-8")
# Phase 5E: the device-location acquisition logic that used to be inline in
# app/map/page.tsx now lives in ONE shared hook used by both Map and Activity.
_HOOK = (ROOT / "app" / "lib" / "useDeviceOrigin.ts").read_text(encoding="utf-8")
_CORE = (ROOT / "places_core.py").read_text(encoding="utf-8")
_MAIN = (ROOT / "main.py").read_text(encoding="utf-8")
_PKG = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
# B10 — Map/Explore adaptive parity + "Search this area"
_STA = (ROOT / "app" / "map" / "searchThisArea.ts").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# real map canvas exists and is wired in
# --------------------------------------------------------------------------- #
def test_map_page_renders_the_real_canvas():
    assert 'import MapCanvas from "./MapCanvas"' in _MAP
    assert "<MapCanvas" in _MAP
    # the canvas only mounts once an explicit origin AND an activity exist
    assert "showResultsArea" in _MAP
    assert "!!origin" in _MAP and "!!activity" in _MAP
    assert _MAP.index("showResultsArea =") < _MAP.index("<MapCanvas")


def test_canvas_uses_leaflet_lazily_and_osm_basemap():
    assert 'await import("leaflet")' in _CANVAS            # client-only, lazy
    assert 'import "leaflet/dist/leaflet.css"' in _CANVAS
    assert "tile.openstreetmap.org" in _CANVAS             # real basemap tiles
    assert "OpenStreetMap" in _CANVAS and "contributors" in _CANVAS  # attribution
    # no continuous GPS watching just because a map exists
    assert "watchPosition" not in _CANVAS
    # markers are keyed by the stable provider identity, never an array index
    assert "place_id" in _CANVAS


def test_no_google_maps_js_sdk_or_browser_key():
    for src in (_MAP, _CANVAS, _STATE, _ORIGIN, _PANEL):
        assert "maps.googleapis.com/maps/api/js" not in src
        assert "GOOGLE_PLACES_API_KEY" not in src
        assert "GOOGLE_GEOCODING_API_KEY" not in src
        assert "NEXT_PUBLIC_GOOGLE" not in src
        assert "AIza" not in src


def test_map_state_helpers_present():
    for fn in ("reconcileSelection", "resultBounds", "directionsUrl", "formatDistance"):
        assert f"export function {fn}" in _STATE
    assert 'from "./mapState"' in _MAP


# --------------------------------------------------------------------------- #
# SECTION 2 — explicit search-origin model (device vs manual, never conflated)
# --------------------------------------------------------------------------- #
def test_search_origin_model_is_an_explicit_discriminated_union():
    assert 'source: "device"' in _ORIGIN
    assert 'source: "manual"' in _ORIGIN
    # a manual origin carries a human label; a device origin carries accuracy
    assert "label: string" in _ORIGIN
    assert "accuracyMeters: number | null" in _ORIGIN
    # the shared hook holds ONE origin value
    assert "useState<SearchOrigin | null>" in _HOOK


def test_all_map_logic_reads_the_single_origin():
    # place search sends the origin's coordinates (not a stray navigator call)
    assert "runSearch(act, origin)" in _MAP or "runSearch(activity, origin)" in _MAP
    assert "o.latitude" in _MAP and "o.longitude" in _MAP
    # navigator.geolocation is reached ONLY through the Phase 5B helper, and only
    # from the one shared hook — never inline in a page.
    assert "navigator.geolocation" not in _MAP and "navigator.geolocation" not in _HOOK
    assert "getCurrentLocation(" not in _MAP
    assert _HOOK.count("await getCurrentLocation(") == 1
    # the canvas is told which kind of origin it is drawing
    assert "kind: origin.source" in _MAP


def test_map_and_activity_share_one_location_hook():
    for src in (_MAP, (ROOT / "app" / "activity" / "page.tsx").read_text(encoding="utf-8")):
        assert 'from "../lib/useDeviceOrigin"' in src
        assert "useDeviceOrigin()" in src
    assert "export function useDeviceOrigin(" in _HOOK


def test_changing_origin_clears_stale_results_and_selection():
    # a manual pick and a device (re)acquire both reset the selection
    assert _MAP.count("setSelectedId(null)") >= 2
    # a fresh place search always clears the selection then re-runs
    assert re.search(r"setSelectedId\(null\);\s*\n\s*setSearch\(\{ phase: \"searching\" \}\)", _MAP)


# --------------------------------------------------------------------------- #
# SECTION 4 + 6 — real geocoding for "Change location"
# --------------------------------------------------------------------------- #
def test_change_location_uses_the_real_backend_geocode_endpoint():
    assert 'import GeocodePanel from "./GeocodePanel"' in _MAP
    assert "<GeocodePanel" in _MAP
    assert '"/api/geocode/search"' in _PANEL
    assert 'method: "POST"' in _PANEL
    # the query goes in the POST body — never a URL / query string
    assert "query=${" not in _PANEL and "?q=" not in _PANEL
    assert "JSON.stringify({ query" in _PANEL


def test_geocode_panel_has_no_fake_autocomplete_or_hidden_autoselect():
    # explicit Search button, min length, not keystroke autocomplete
    assert 'type="submit"' in _PANEL
    assert "trim().length < 2" in _PANEL
    # a result is chosen ONLY by the user tapping it
    assert "onClick={() => onPick(r)}" in _PANEL
    assert _PANEL.count("onPick(") == 1
    # no AI-invented / hardcoded coordinates in the panel
    assert "AIza" not in _PANEL and "2.9213" not in _PANEL and "101.6559" not in _PANEL


def test_geocode_panel_states_and_accessibility():
    for token in ('role="dialog"', 'aria-modal="true"', 'aria-live="polite"'):
        assert token in _PANEL
    # loading / empty / error states all exist
    assert 'phase: "searching"' in _PANEL
    assert 'phase: "empty"' in _PANEL
    assert 'phase: "error"' in _PANEL
    # provider errors are shown as friendly copy, never a raw kind
    assert "GEO_ERROR_COPY" in _PANEL
    # attribution for whichever provider answered
    assert "OpenStreetMap" in _PANEL and "Google" in _PANEL


# --------------------------------------------------------------------------- #
# SECTION 14 — race conditions: stale responses never overwrite newer state
# --------------------------------------------------------------------------- #
def test_race_token_guards_on_every_async_flow():
    # the shared hook: a monotonic request token + a mounted flag, checked
    # before every setState in the async acquisition path
    assert "reqSeqRef = useRef(0)" in _HOOK
    assert "mountedRef = useRef(true)" in _HOOK
    assert "++reqSeqRef.current" in _HOOK
    assert "my !== reqSeqRef.current || !mountedRef.current" in _HOOK
    assert _HOOK.count("my !== reqSeqRef.current") >= 2   # post-geo + post-validate + catch
    # the Map place-search flow shares the SAME token/flag from the hook
    assert "my !== reqSeqRef.current || !mountedRef.current" in _MAP
    # panel: its own token so a slow response can't replace a newer search
    assert "seq = useRef(0)" in _PANEL
    assert "my !== seq.current" in _PANEL


def test_manual_origin_selection_invalidates_a_pending_gps_flow():
    # setManualOrigin bumps the token BEFORE any state is set, so a still-in-flight
    # acquireDevice() can never call setOrigin({source:"device"}) over it.
    fn = _HOOK[_HOOK.index("const setManualOrigin ="):_HOOK.index("const clearOrigin =")]
    assert "reqSeqRef.current++" in fn
    assert fn.index("reqSeqRef.current++") < fn.index("setOrigin(o)")
    # acquireDevice re-checks the token at each async boundary before it writes.
    acq = _HOOK[_HOOK.index("const acquireDevice ="):_HOOK.index("const setManualOrigin =")]
    assert acq.count("my !== reqSeqRef.current") >= 2
    assert acq.index("reqSeqRef.current") < acq.index("setOrigin(o)")
    assert acq.index("reqSeqRef.current") < acq.index('setDeviceLoc({ phase: "failed", status: result.status')
    # Map "change activity" also invalidates any pending acquire/search
    assert "reqSeqRef.current++" in _MAP[_MAP.index("const changeActivity ="):_MAP.index("const retrySearch =")]


def test_location_acquisition_has_a_bounded_backend_call():
    # a wedged /api/location/validate must not strand the UI in "requesting":
    # the fetch is abort-bounded and its timer is always cleared (in the hook).
    acq = _HOOK[_HOOK.index("const acquireDevice ="):_HOOK.index("const setManualOrigin =")]
    assert "new AbortController()" in acq
    assert "signal: ctrl.signal" in acq
    assert "ctrl.abort()" in acq
    assert "} finally {" in acq and "clearTimeout(validateTimer)" in acq
    # a validate failure/abort is a recoverable BACKEND state, never a hang
    assert 'status: "BACKEND_UNREACHABLE"' in acq


def test_location_helper_has_a_coreos_owned_watchdog():
    assert "LOCATION_WATCHDOG_MS" in _LOC
    assert "clearTimeout(watchdog)" in _LOC
    # settle-once flag makes late callbacks no-ops
    assert "if (settled) return;" in _LOC
    # the watchdog resolves to a truthful error, never a fabricated position
    wd = _LOC[_LOC.index("watchdog = setTimeout("):_LOC.index("try {", _LOC.index("watchdog = setTimeout("))]
    assert 'makeLocationError("TIMEOUT")' in wd
    assert "2.9213" not in _LOC and "Cyberjaya" not in _LOC


def test_location_helper_uses_browser_only_timer_semantics():
    # a Node-only handle method in client code (`.unref()`) was removed — plain
    # setTimeout/clearTimeout only.
    assert ".unref" not in _LOC


# --------------------------------------------------------------------------- #
# "Getting your location…" HANG — root cause was a Strict-Mode effect replay
# leaving `mounted.current` stuck false so every async guard bailed forever.
# --------------------------------------------------------------------------- #
def test_mounted_flag_is_reestablished_on_every_effect_setup():
    # the buggy cleanup-only form must appear nowhere
    for src in (_MAP, _HOOK):
        assert "useEffect(() => () => { mounted" not in src
    # the fixed form: an effect body that SETS the flag, via the shared helper
    assert "useEffect(() => mountedEffect(mountedRef), [])" in _HOOK
    assert "export function mountedEffect(" in _DEVLOC
    body = _DEVLOC[_DEVLOC.index("export function mountedEffect("):]
    assert "ref.current = true;" in body
    assert body.index("ref.current = true;") < body.index("return () =>")


def test_page_level_acquisition_failsafe_exists():
    # an independent deadline INSIDE the hook (not an async closure, no
    # dependency on mountedRef.current) that force-transitions out of "requesting"
    assert "LOCATION_ACQUIRE_DEADLINE_MS" in _HOOK and "LOCATION_ACQUIRE_DEADLINE_MS" in _DEVLOC
    assert 'acquireFailsafe(prev, locationErrorMessage("TIMEOUT"))' in _HOOK
    fx = _HOOK[_HOOK.index('if (deviceLoc.phase !== "requesting") return;'):]
    fx = fx[:fx.index("}, [deviceLoc.phase]);") + 25]
    assert "setTimeout(" in fx and "clearTimeout(deadline)" in fx
    assert "setDeviceLoc((prev) =>" in fx        # functional update, no stale closure
    # the failsafe transition is pure + truthful
    af = _DEVLOC[_DEVLOC.index("export function acquireFailsafe("):]
    assert 'phase: "failed", status: "TIMEOUT"' in af
    assert "latitude" not in af and "Cyberjaya" not in _DEVLOC


def test_only_one_write_of_the_requesting_phase():
    # once we leave "requesting" nothing but a fresh user gesture can re-enter it
    assert _HOOK.count('setDeviceLoc({ phase: "requesting" })') == 1
    assert _MAP.count('setDeviceLoc({ phase: "requesting" })') == 0
    acq = _HOOK[_HOOK.index("const acquireDevice ="):_HOOK.index("const setManualOrigin =")]
    assert 'setDeviceLoc({ phase: "requesting" })' in acq   # the sole writer
    assert "useEffect" not in acq                            # never re-entered by an effect


def test_manual_search_is_offered_while_requesting():
    for src in (_MAP, (ROOT / "app" / "activity" / "page.tsx").read_text(encoding="utf-8")):
        req = src[src.index('{deviceLoc.phase === "requesting" && ('):]
        req = req[:req.index('{deviceLoc.phase === "failed"')]
        assert "Search a location instead" in req
        assert "setGeoOpen(true)" in req


def test_no_forensic_debug_instrumentation_left_in_the_product():
    # the temporary hang-investigation tracing was removed before ship
    for src in (_MAP, _HOOK):
        assert "locTrace" not in src and "LOCDBG" not in src
    assert "onEvent" not in _LOC
    # and no coordinate/console tracing crept in
    assert "console.debug" not in _MAP and "console.debug" not in _LOC and "console.debug" not in _HOOK


# --------------------------------------------------------------------------- #
# single selection source of truth (no drift between markers and list)
# --------------------------------------------------------------------------- #
def test_single_selection_state():
    assert "selectedId" in _MAP and "setSelectedId" in _MAP
    assert "reconcileSelection(" in _MAP
    assert "onSelectPlace={setSelectedId}" in _MAP
    assert "selectedPlace" in _MAP
    assert "places.find((p) => p.place_id === selectedId)" in _MAP


# --------------------------------------------------------------------------- #
# search / privacy unchanged from Phase 5C
# --------------------------------------------------------------------------- #
def test_search_still_posts_to_the_real_endpoint_body_only():
    assert '"/api/places/search"' in _MAP
    assert 'method: "POST"' in _MAP
    assert "&lat=" not in _MAP and "lat=${" not in _MAP and "lng=${" not in _MAP
    assert "latitude" in _MAP and "longitude" in _MAP  # go in the JSON body


def test_directions_uses_real_selected_destination():
    assert "directionsUrl(selectedPlace)" in _MAP
    low = _MAP.lower()
    for bad in ("eta:", "drive time", "driving distance", "min drive", "minutes by car",
                "km by road", "fastest route", "route line", "estimated arrival"):
        assert bad not in low
    assert "straight line" in _MAP


def test_no_fabricated_trust_or_ai_language():
    for bad in ("Verified Location", "AI selected", "Best place", "Perfect conditions",
                "highly rated", "recommended for you"):
        assert bad.lower() not in _MAP.lower()
    assert "Identity from" in _MAP


def test_truthful_missing_evidence():
    assert '"not available"' in _MAP
    assert "may offer" in _MAP
    assert "Hours &amp; ratings unavailable" in _MAP or "Hours & ratings unavailable" in _MAP


# --------------------------------------------------------------------------- #
# accessibility basics
# --------------------------------------------------------------------------- #
def test_accessibility_basics():
    assert 'aria-live="polite"' in _MAP
    assert "aria-pressed=" in _MAP
    assert "aria-label=" in _MAP
    assert "<ul" in _MAP and "<button" in _MAP
    assert 'role="application"' in _CANVAS and "aria-label" in _CANVAS


def test_schedule_handoff_preserved_and_truthful():
    assert 'params.get("from") === "schedule"' in _MAP
    assert "From your schedule" in _MAP
    assert "not yet a matched place" in _MAP


# --------------------------------------------------------------------------- #
# Phase 5D acceptance: nearest-first ordering + coarse-location UX
# --------------------------------------------------------------------------- #
def test_frontend_does_not_resort_places():
    assert ".sort(" not in _MAP
    assert "search.places.map(" in _MAP
    assert "places={places}" in _MAP
    assert 'search.phase === "results" ? search.places : []' in _MAP


def test_backend_ordering_is_nearest_first_and_provider_neutral():
    assert "def finalize_results" in _CORE
    assert "_distance_sort_key" in _CORE
    assert "sorted(places, key=lambda" in _CORE
    assert "math.inf" in _CORE
    # deterministic tie-break by the stable identity
    assert '_distance_sort_key(x), str(x.get("place_id")' in _CORE
    assert "finalize_results(list(raw_places))" in _MAIN


def test_coarse_location_unreliable_tier_exists_and_is_used():
    assert "LOCATION_UNRELIABLE_ACCURACY_M" in _LOC
    assert "export function isAccuracyUnreliable" in _LOC
    # the map surface consumes it through the search-origin helpers
    assert "isAccuracyUnreliable" in _ORIGIN
    assert "originIsVeryApproximate" in _MAP and "originDistanceReliable" in _MAP
    # a truthful warning + a real retry + a manual escape, no fabricated location
    assert "very approximate" in _MAP.lower()
    assert "Try location again" in _MAP
    assert "highAccuracy: true" in _MAP
    # confident metre distances are withheld under an unreliable fix
    assert "distanceLabel(" in _MAP
    assert "distance not reliable" in _STATE


def test_manual_location_is_a_universal_escape_hatch():
    # every device-location failure still offers "Search a location"
    assert 'deviceLoc.phase === "failed"' in _MAP
    assert "Search a location" in _MAP
    # the failure block does not gate the manual option behind `retryable`.
    # (Bounded by the next section marker after the failure block; the shared
    #  BottomNav lives in app chrome now, so this file no longer contains its
    #  own "bottom-0" bar to delimit against.)
    fail_block = _MAP[_MAP.index('deviceLoc.phase === "failed"'):]
    fail_block = fail_block[:fail_block.index("origin set + activity chosen")]
    assert "setGeoOpen(true)" in fail_block


def test_device_and_manual_origins_get_distinct_markers():
    # the manual origin must NOT be drawn as the blue "you are here" dot
    assert "areaIconHtml" in _CANVAS and "deviceIconHtml" in _CANVAS
    assert 'origin.kind === "device"' in _CANVAS
    # the accuracy circle is device-only (a manual point is not a GPS fix)
    assert re.search(r"isDevice && .*acc", _CANVAS)
    assert 'kind: "device" | "manual"' in _CANVAS


def test_no_cyberjaya_or_hardcoded_fallback_on_the_map_surface():
    # This surface must NEVER "correct" a bad fix with an invented location.
    # (GeocodePanel's placeholder text lists example city names for the user to
    #  type — that is UI copy, not a fallback origin, and is guarded separately
    #  in test_geocode_panel_has_no_fake_autocomplete_or_hidden_autoselect.)
    for src in (_MAP, _CANVAS, _STATE, _ORIGIN, _LOC):
        assert "2.9213" not in src and "101.6559" not in src
        assert "Cyberjaya" not in src
        assert "fallbackLat" not in src and "fallbackLng" not in src
        assert "ipgeolocation" not in src.lower() and "ip-api" not in src.lower()


def test_no_exact_coordinate_logging_added():
    for name in ("main.py", "places_core.py", "places_provider.py", "geocode_provider.py"):
        src = (ROOT / name).read_text(encoding="utf-8")
        for m in re.finditer(r"print\(([^)]*)\)", src):
            frag = m.group(1)
            assert "latitude" not in frag and "longitude" not in frag, (name, frag)
            assert "lat," not in frag and "lng," not in frag, (name, frag)


# --------------------------------------------------------------------------- #
# SECTION 16 — legacy /api/map-locations is deprecated and unused
# --------------------------------------------------------------------------- #
def test_legacy_map_locations_route_is_deprecated_and_coordinate_free():
    start = _MAIN.index('@app.get("/api/map-locations")')
    nxt = _MAIN.find("\n@app.", start + 1)
    block = _MAIN[start:nxt if nxt != -1 else start + 1500]
    assert "DEPRECATED" in block
    # no lat/lng query params on the Map surface
    assert "lat:" not in block and "lng:" not in block
    assert "lat: Optional" not in block and "lng: Optional" not in block


def test_frontend_never_calls_the_legacy_map_locations_route():
    for f in list((ROOT / "app").rglob("*.ts")) + list((ROOT / "app").rglob("*.tsx")):
        assert "map-locations" not in f.read_text(encoding="utf-8"), f


# --------------------------------------------------------------------------- #
# dependency hygiene
# --------------------------------------------------------------------------- #
def test_map_dependency_is_leaflet_only():
    deps = _PKG.get("dependencies", {})
    assert "leaflet" in deps
    assert "@types/leaflet" in _PKG.get("devDependencies", {})
    for banned in ("mapbox-gl", "@react-google-maps/api", "google-map-react",
                   "@vis.gl/react-google-maps", "@react-leaflet/core"):
        assert banned not in deps


def test_node_test_script_covers_the_search_origin_suite():
    assert "app/map/__tests__/searchOrigin.test.ts" in _PKG["scripts"]["test:node"]
    assert "app/map/__tests__/mapState.test.ts" in _PKG["scripts"]["test:node"]
    assert "app/map/__tests__/searchThisArea.test.ts" in _PKG["scripts"]["test:node"]


# --------------------------------------------------------------------------- #
# B10 — Map/Explore adaptive parity (8 -> 12 -> 20 km) + "Search this area"
# --------------------------------------------------------------------------- #
def test_map_search_opts_into_adaptive_mode():
    # the Map/Explore flow asks the backend for the staged ladder, and sends NO
    # fixed radius alongside it.
    assert "adaptive_radius: true" in _MAP
    assert "radius_m:" not in _MAP            # the Map UI never pins a radius
    # the backend contract is additive: the flag is optional and mutually
    # exclusive with a fixed radius.
    assert 'body.get("adaptive_radius") is True' in _MAIN
    assert "radius_m cannot be combined with adaptive_radius" in _MAIN
    # staged: a genuine provider request per rung, gated by within_radius, never
    # a 4th rung, never 20 km first.
    seg = _MAIN[_MAIN.index("B10 staged adaptive search"):]
    seg = seg[: seg.index("return PlacesSearchResult", seg.index("for radius_m in ladder"))]
    assert "for radius_m in ladder" in seg
    assert "await provider.search_nearby(" in seg
    assert "places.within_radius(" in seg
    assert "places.finalize_results(" in seg


def test_expanded_radius_note_is_factual_not_a_control():
    assert 'from "./searchThisArea"' in _MAP
    assert "expandedRadiusNote(search.radiusM)" in _MAP
    assert "Expanded search to" in _STA
    # no radius selector / filter panel anywhere on the Map surface
    low = _MAP.lower()
    for banned in ("radius selector", "<select", 'type="range"', "filter panel", "km slider"):
        assert banned not in low
    # empty state is radius-honest
    assert 'search.radiusM >= 20_000 ? " within 20 km" : " nearby"' in _MAP


def test_search_this_area_is_an_accessible_semantic_button():
    # the control's opening tag through its className, as one contiguous slice
    start = _MAP.index('onClick={onAreaSearch}')
    btn = _MAP[_MAP.rindex("<button", 0, start): _MAP.index(">", start + 200)]
    assert 'type="button"' in btn
    assert 'aria-label="Search this area"' in btn
    assert "min-h-[44px]" in btn and "min-w-[44px]" in btn
    assert "focus-visible:outline" in btn
    assert "disabled={areaBusy}" in btn and "aria-busy={areaBusy}" in btn
    # loading state is not colour-only: it carries a spinner AND the word
    assert "Searching…" in _MAP
    # reduced motion is respected on the new spinner
    assert "motion-reduce:animate-none" in _MAP


def test_search_this_area_visibility_is_movement_gated_not_automatic():
    assert "shouldOfferAreaSearch(lastSearchCenter, mapCenter)" in _MAP
    # it only shows once a completed result set exists AND the map moved
    assert 'search.phase === "results" && shouldOfferAreaSearch(' in _MAP
    # the canvas reports a settled centre; the page only stores it (NO search)
    assert "onCenterSettled={(c) => setMapCenter(c)}" in _MAP
    assert "onCenterSettled" in _CANVAS
    canvas_cb = _CANVAS[_CANVAS.index("const reportSettled"):_CANVAS.index("map.on(\"moveend\"")]
    assert "search_nearby" not in canvas_cb and "fetch(" not in canvas_cb and "apiFetch" not in canvas_cb
    # a programmatic recentre (fitBounds / panTo) must not surface the control
    assert "markProgrammaticMove()" in _CANVAS
    assert "programmaticMoveRef.current" in _CANVAS


def test_search_this_area_centre_is_not_called_current_location():
    # the map centre is searched as an explicit "This area" origin — never the
    # device location, and it never triggers geolocation.
    assert 'setManualOrigin({ latitude: mapCenter.latitude, longitude: mapCenter.longitude, label: "This area" })' in _MAP
    area = _MAP[_MAP.index("const onAreaSearch ="):_MAP.index("const showAreaSearchButton")]
    assert "acquireDevice(" not in area
    assert "Current location" not in area and "Your location" not in area
    # nothing persists / logs the centre
    assert "localStorage" not in _MAP and "sessionStorage" not in _MAP


def test_search_this_area_failure_and_empty_are_honest():
    # a failed area search keeps the map mounted (position preserved) and offers
    # a retry through the existing error path
    assert 'search.phase === "error"' in _MAP and "retrySearch" in _MAP
    # the area search clears busy state on completion (success OR failure)
    assert "setAreaBusy(false)" in _MAP
    assert ".finally(() =>" in _MAP


def test_focus_view_has_no_search_this_area_and_is_not_overwritten_by_entering_map():
    # the "Search this area" control lives only in the main results area, never
    # in the Schedule/Activity -> Map focus view (a separate early return).
    assert "if (focusRef) return <FocusPlaceView" in _MAP
    focus_component = _MAP[_MAP.index("function FocusPlaceView"):]
    assert "Search this area" not in focus_component
    assert "onAreaSearch" not in focus_component


def test_search_this_area_helpers_are_pure_and_dom_free():
    for banned in ("document", "window", "leaflet", "useState", "useEffect", "apiFetch"):
        assert banned not in _STA
    assert "AREA_SEARCH_MIN_MOVE_M" in _STA and "haversineMeters" in _STA


# --------------------------------------------------------------------------- #
# B11.1 — Location Services preference genuinely gates navigator.geolocation
# --------------------------------------------------------------------------- #
_ACT = (ROOT / "app" / "activity" / "page.tsx").read_text(encoding="utf-8")
_SETTINGS = (ROOT / "app" / "settings" / "page.tsx").read_text(encoding="utf-8")
_THEME = (ROOT / "app" / "components" / "ThemeProvider.tsx").read_text(encoding="utf-8")


def test_useDeviceOrigin_reads_the_location_preference_and_gates_before_geolocation():
    # the hook consumes the single settings store, not a second preference store
    assert 'from "../components/ThemeProvider"' in _HOOK and "useSettings()" in _HOOK
    # the GATE sits BEFORE the one getCurrentLocation() call and returns early
    gate = _HOOK[_HOOK.index("const acquireDevice") : _HOOK.index("await getCurrentLocation(")]
    assert "locationServicesRef.current === false" in gate
    assert 'status: "SERVICES_OFF"' in gate
    assert "return;" in gate
    # still exactly one geolocation entry point, still never navigator.geolocation here
    assert _HOOK.count("await getCurrentLocation(") == 1
    assert "navigator.geolocation" not in _HOOK


def test_turning_the_preference_off_clears_transient_device_origin_only():
    seg = _HOOK[_HOOK.index("turning Location Services OFF") : _HOOK.index("sign-out or an account switch")]
    assert "settingsReady" in seg and "locationServices" in seg
    assert 'prev.source === "device" ? null : prev' in seg   # a manual origin is kept


def test_logout_or_account_switch_clears_transient_location_state():
    assert 'from "./auth"' in _HOOK and "useAuth()" in _HOOK
    seg = _HOOK[_HOOK.index("sign-out or an account switch") :]
    assert "[authStatus, user?.id]" in seg
    assert "setOrigin(null)" in seg


def test_settings_toggle_writes_through_the_one_store_and_never_prompts():
    assert "useSettings" in _SETTINGS and "setPref(" in _SETTINGS
    # the settings screen must not itself acquire a location or import the hook
    assert "acquireDevice" not in _SETTINGS and "getCurrentLocation" not in _SETTINGS
    assert "navigator.geolocation" not in _SETTINGS
    # ThemeProvider exposes the reactive prefs + a setter; still one provider
    assert "export function useSettings" in _THEME
    assert "locationServices" in _THEME and "notifications" in _THEME
    # setPref persists via the existing settings endpoint, no location call
    sp = _THEME[_THEME.index("const setPref") : _THEME.index("const resolvedTheme")]
    assert '"/api/settings/update"' in sp
    assert "getCurrentLocation" not in sp and "navigator.geolocation" not in sp


def test_settings_copy_distinguishes_in_app_from_background_push():
    low = _SETTINGS.lower()
    assert "in-app reminder" in low
    assert "background push" in low and ("not supported" in low or "not currently supported" in low)
    # location copy is honest about NOT changing the device/OS permission
    assert "doesn't change your device" in _SETTINGS or "does not change your device" in _SETTINGS
    # the old misleading lines are gone
    assert "Notification delivery is not built yet" not in _SETTINGS
    assert "The browser still prompts for location when a feature needs it" not in _SETTINGS


def test_activity_and_map_show_a_truthful_services_off_recovery():
    for src in (_ACT, _MAP):
        assert 'locationRecoveryAction(deviceLoc.status) === "settings"' in src
        # a real route to Settings ...
        assert 'href="/settings"' in src and "Open Settings" in src
        # ... and the manual fallback is still offered in the same panel
        panel = src[src.index('locationRecoveryAction(deviceLoc.status) === "settings"') :]
        panel = panel[: panel.index("</div>") + 6]
        assert "Search a location" in panel
    # the message itself (from location.ts) points at Settings + manual search
    assert "SERVICES_OFF" in _LOC
    off = _LOC[_LOC.index("SERVICES_OFF:") :]
    assert "Settings" in off[:400] and "search for a place" in off[:400].lower()


def test_search_this_area_is_not_blocked_by_the_location_gate():
    # "Search this area" uses setManualOrigin (no geolocation), so the gate in
    # acquireDevice cannot reach it — the control stays usable with the pref off.
    area = _MAP[_MAP.index("const onAreaSearch =") : _MAP.index("const showAreaSearchButton")]
    assert "acquireDevice" not in area
    assert "setManualOrigin({ latitude: mapCenter.latitude" in area
