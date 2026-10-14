# CoreOS — Verified Places (Phase 5C)

Real nearby-place search. CoreOS never invents a place, coordinate, rating,
opening hour, phone, website or "open now" value. If a provider does not supply
a fact, CoreOS shows it as unavailable.

```
real device location (Phase 5B)
   → POST /api/places/search   (auth; coords in the BODY, never a URL)
      → server validation      (coordinates 5B · activity taxonomy · radius ≤ 20 km)
         → PlaceProvider abstraction   (places_provider.py)
            → external provider        (OpenStreetMap/Overpass  or  Google Places API New)
               → normalise + evidence  (places_core.build_place)
                  → Haversine distance (straight-line) + sort + cap
                     → CoreOS place model → Map UI
```

## Provider architecture

| | Primary (rich) | Default / fallback (free) |
|---|---|---|
| Provider | **Google Places API (New)** — Text Search | **OpenStreetMap / Overpass** |
| Key / billing | server-side `GOOGLE_PLACES_API_KEY`, billing-enabled GCP project | none |
| Supplies | identity, coords, address, **rating**, **review count**, **opening hours**, **open-now**, **business status**, website, phone, maps URL | identity, coords, address (if tagged), opening hours (if tagged), website/phone (if tagged) |
| Does **not** supply | — | ratings, review counts, reliable open-now, operational status |
| Attribution | "Powered by Google" | "© OpenStreetMap contributors" (ODbL) |

**Selection** — `COREOS_PLACES_PROVIDER`: `auto` (default — Google if a key is
present, else OSM) · `osm` · `google`. The Map UI is provider-neutral: it renders
the normalised `CorePlace` model, never a raw Google/OSM payload.

**Why OSM is the shipped default:** it needs zero setup, so CoreOS place search
works immediately. Google is a drop-in upgrade — add the key, done. No UI change.

## Credential boundary

- `GOOGLE_PLACES_API_KEY` is read **only** server-side (`places_provider.py`).
  It is sent to Google in the `X-Goog-Api-Key` **request header** and never
  appears in a response body, a log line, or the browser bundle.
- Never create a `NEXT_PUBLIC_GOOGLE_*` variable.

## Normalised CoreOS place model (`CorePlace`)

`place_id` (`osm:node:123` / `google:ChIJ…`), `provider`, `provider_place_id`,
`name`, `latitude`, `longitude`, `address?`, `distance_m`,
`distance_kind = "straight_line"`, `activity`, `activity_match` (`exact` |
`broad`), `provider_types[]`, and evidence-backed optionals — all nullable:
`rating?`, `rating_count?`, `opening_hours?`, `open_now?`, `business_status?`,
`website?`, `phone?`, `maps_url`.

No manufactured defaults (`rating = 5`, `open_now = true`, `"Open 24 hours"`).

## Evidence model

Trust is **not** one boolean. Each place carries per-category provenance:

| Category | Records | OSM | Google |
|---|---|---|---|
| `identity` | `{source, verified: true}` — the place exists at these coordinates | ✅ always | ✅ always |
| `operational` | `{source, status}` — OPERATIONAL / CLOSED_TEMPORARILY / CLOSED_PERMANENTLY | ✗ `source: null` | ✅ when returned |
| `hours` | `{source, has_hours, open_now}` | tag-based, `open_now: null` | ✅ incl. `open_now` |
| `quality` | `{source, rating, rating_count}` | ✗ `source: null` | ✅ when returned |
| `contact` | `{source, website, phone}` | tag-based | ✅ when returned |

`identity.verified` is the **only** verification flag. A place can have verified
identity and *unknown* hours / *no* rating simultaneously — the UI reflects that
("Identity from OpenStreetMap", "Hours & ratings unavailable").

## Activity → search taxonomy (`places_core.ACTIVITY_TAXONOMY`)

Centralised and precision-first. A bike shop is not a cycling place; a park is
not a football field. Ambiguous venue types are omitted rather than padding
results.

| Activity | OSM (examples) | Google text query |
|---|---|---|
| futsal | `sport=futsal` (exact), `leisure=pitch`+`sport~soccer\|football` (broad) | "futsal court" |
| football | `leisure=pitch`+`sport~soccer\|football` (exact) | "football field" |
| basketball | `sport=basketball` (exact) | "basketball court" |
| badminton | `sport=badminton` (exact) | "badminton court" |
| swimming | `leisure=swimming_pool`, `sport=swimming` (exact) | "public swimming pool" |
| running | `leisure=track`+`sport~running\|athletics` (exact) | "running track" |
| cycling | `sport=cycling`, velodrome (exact) — **not** `shop=bicycle` | "cycling track velodrome" |

`activity_match: "exact"` = the venue names the sport; `"broad"` = a multi-sport
venue that plausibly hosts it (UI: "may offer <activity>").

## Search / radius / cost policy (`places_core`)

`SEARCH_RADIUS_DEFAULT_M = 8000`, `SEARCH_RADIUS_MAX_M = 20000` (historical
"~20 km nearby" ceiling), `SEARCH_RADIUS_MIN_M = 200`,
`SEARCH_RESULT_LIMIT = 20`, `PROVIDER_ELEMENT_CAP = 60`,
`PROVIDER_TIMEOUT_S = 10`. Radius is validated server-side (≤ 0 → 422; above max
→ clamped). **One user search = one upstream request.** No retry loop. Google
uses a restrictive `X-Goog-FieldMask` so only needed fields are billed.

## Distance

`places_core.haversine_m` — great-circle **straight-line** metres from the
user's validated coordinates to the place. Labelled "away" / "straight line",
**never** "drive". Travel time/distance needs a routing provider (later).

## Provider failure model

Normalised, never a raw upstream string to the user:

| Kind | Meaning | Map copy |
|---|---|---|
| `PROVIDER_NOT_CONFIGURED` | `google` selected but no key / key rejected (401/403) | "Place search isn't switched on for this server yet." |
| `PROVIDER_UNAVAILABLE` | 5xx / network / bad status | "Nearby search is temporarily unavailable." + Try again |
| `PROVIDER_TIMEOUT` | request exceeded `PROVIDER_TIMEOUT_S` | "Nearby search took too long." + Try again |
| `PROVIDER_RATE_LIMITED` | 429 / `RESOURCE_EXHAUSTED` | "Nearby search is busy right now." + Try again |
| `INVALID_PROVIDER_RESPONSE` | unparseable upstream body | "Nearby search is temporarily unavailable." |

`NO_RESULTS` is **not** an error — an empty list is a success (`status: "NO_RESULTS"`,
"No matching places found nearby."). A provider failure is never shown as "no
places", and a failed provider is never silently replaced by fake DB rows.

## Privacy boundary

- Coordinates: POST body only; validated by the Phase 5B validator (error
  messages are coordinate-free); **never** logged, **never** in a raised
  message, **never** sent to the AI model.
- Persistence: **none.** No `map_locations` writes, no search history, no new
  table, no migration. (`map_locations` is now unused and superseded; it is left
  in place — no destructive change in 5C.)
- Provider key: server-side only (see credential boundary).
- AI: `/api/places/search` never touches the LLM.

## Live vs mocked verification

- **Mocked** (`tests/test_places_foundation.py`, `httpx.MockTransport`): proves
  adapter parsing, normalisation, error mapping, the API contract, evidence,
  distance, bounds, privacy. Does **not** prove a provider account / key /
  billing / quota, or that live results exist for a given area.
- **Live OSM smoke:** a single real Overpass call is exercised out-of-band (not
  in CI) to confirm the adapter works end-to-end against the real endpoint.
- **Live Google:** requires the human-only setup below; not performed.

## Human-only setup (Google upgrade — optional)

CoreOS runs on OpenStreetMap with no setup. To enable the richer Google
provider:

1. Create / choose a Google Cloud project and **enable billing** (a card is
   required; there is a monthly free tier).
2. Enable **"Places API (New)"** for that project.
3. Create an **API key**, restrict it to the Places API (and ideally to the
   server IP).
4. Put it in the local `.env` **privately**: `GOOGLE_PLACES_API_KEY=…`
   (and optionally `COREOS_PLACES_PROVIDER=google`). Restart the backend.

Nothing else changes — the Map UI and the CoreOS place model are identical.

## Attribution requirement

When OSM data is shown, display "© OpenStreetMap contributors" (the Map results
view does). When Google data is shown, display "Powered by Google".
