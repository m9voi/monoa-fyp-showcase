# CoreOS — Secure Development Foundation (Phase 5A)

This document describes the **single trusted HTTPS origin** used for CoreOS
development, and how to reproduce it after a PC restart.

Phase 5A is infrastructure only. It does **not** implement geolocation, places,
weather, or Activity — those come in Phase 5B+.

---

## 1. Architecture

```
  iPhone / browser
        │   HTTPS  (https://coreos.local)
        ▼
  ┌───────────────┐   Caddy — dev/Caddyfile
  │ Reverse proxy │   • terminates TLS with Caddy's built-in local CA (tls internal)
  │   :443        │   • /api/*  ─► 127.0.0.1:8080   (FastAPI, loopback HTTP)
  └───────────────┘   • /*      ─► 127.0.0.1:3000   (Next.js, loopback HTTP)
        │
        ├───────────────► FastAPI  :8080   (never browser-facing)
        └───────────────► Next.js  :3000   (app + HMR; also rewrites /api ─► FastAPI)
```

* The browser only ever sees **one origin**: `https://coreos.local`.
* Internal hops (proxy → Next, proxy → FastAPI, Next → FastAPI) stay on
  `127.0.0.1` HTTP because they are not browser-facing. There is no public
  internet exposure and no cloud dependency.
* `next.config.ts` **also** rewrites `/api/*` to the backend server-side, so the
  same-origin API works even in plain `http://localhost:3000` development
  (without Caddy). Caddy simply adds TLS and the trusted hostname on top.
* Why this exists: `http://<LAN-IP>:3000` is **not a secure browser context**,
  so iOS Safari will not grant `navigator.geolocation` (often without even
  showing the permission prompt). A trusted HTTPS origin is a hard prerequisite
  for the real-location work in Phase 5B.

---

## 2. Prerequisites

| Tool | Why | Install |
|---|---|---|
| Python + backend deps | FastAPI backend | `pip install -r requirements.txt` |
| Node + `npm install` | Next.js frontend | already set up |
| **Caddy v2** | HTTPS reverse proxy + local CA | Download the single `caddy.exe` from <https://caddyserver.com/download> (pick *Windows / amd64*), put it on `PATH` (e.g. `C:\Tools\caddy\caddy.exe`). No installer, no admin needed to run it. |

`mkcert` is **not** required — Caddy's `tls internal` issues the certificate.

---

## 3. One-time setup on this Windows machine

1. **Hosts entry** — map the dev hostname to loopback. Open an **elevated**
   editor and add to `C:\Windows\System32\drivers\etc\hosts`:

   ```
   127.0.0.1   coreos.local
   ```

   (If `coreos.local` is inconvenient, pick another name, set
   `COREOS_DEV_HOST=<name>` in `.env`, and use that name everywhere below.)

2. **Trust Caddy's local CA** — run once, in an **elevated** terminal:

   ```powershell
   caddy trust
   ```

   This installs Caddy's development root CA into the Windows certificate store
   so Chrome/Edge/Firefox trust `https://coreos.local`. The CA lives under
   `%AppData%\Caddy` — **outside this repository**. Nothing secret is written
   into the working tree.

3. **`.env`** — copy `.env.example` to `.env` if you have not already, then set
   the Phase 5A values:

   ```ini
   # same-origin: browser calls "/api/..."; Next proxies it to the backend
   NEXT_PUBLIC_API_BASE_URL=

   # the browser-facing origin is HTTPS behind the proxy
   COREOS_COOKIE_SECURE=1

   # leave blank to use the safe dev allowlist (includes https://coreos.local)
   COREOS_ALLOWED_ORIGINS=

   COREOS_DEV_HOST=coreos.local
   COREOS_BACKEND_ORIGIN=http://127.0.0.1:8080
   COREOS_ENV=development
   ```

   > **`NEXT_PUBLIC_*` is captured when the dev server starts (and inlined at
   > `next build` time).** After editing any `NEXT_PUBLIC_API_BASE_URL` value you
   > MUST restart `npm run dev` (and re-run `npm run build` for `next start`).
   > A stale `NEXT_PUBLIC_API_BASE_URL=http://<LAN-IP>:8080` left in `.env` is
   > what makes Login fail on `https://coreos.local` with "Couldn't reach the
   > server" — the mixed-content guard in `app/lib/api.ts` refuses the http://
   > base before any request is sent (look for the `CoreOS config error:` line
   > in the browser console).

---

## 4. Daily startup (three terminals, from the repo root)

| # | Command | Serves |
|---|---|---|
| 1 | `python main.py` | FastAPI on `http://127.0.0.1:8080` |
| 2 | `npm run dev` | Next.js on `http://127.0.0.1:3000` |
| 3 | `npm run dev:proxy` | Caddy on `https://coreos.local` (and `https://localhost`) |

Then open **`https://coreos.local`**.

To stop: Ctrl-C each terminal. Order does not matter.

---

## 5. Environment variables (Phase 5A)

| Variable | Default | Meaning |
|---|---|---|
| `NEXT_PUBLIC_API_BASE_URL` | *(blank)* | Blank ⇒ same-origin relative `/api/...`. An explicit `http://…` base is rejected at runtime when the page is HTTPS (mixed-content guard). |
| `COREOS_COOKIE_SECURE` | `false` in dev, `true` in prod | Adds `Secure` to the session cookie. Set `1` for the HTTPS proxy. Cannot be disabled in production. |
| `COREOS_DEV_HOST` | `coreos.local` | The single HTTPS hostname. Consumed by `dev/Caddyfile` and `next.config.ts` (`allowedDevOrigins`) and the dev CORS allowlist. |
| `COREOS_BACKEND_ORIGIN` | `http://127.0.0.1:8080` | **Server-side only.** Where Next.js forwards `/api/*`. Never sent to the browser; keep it a loopback/internal address. |
| `COREOS_ALLOWED_ORIGINS` | *(blank ⇒ dev allowlist)* | Explicit CORS allowlist. Blank in dev uses a safe default list that already contains `https://coreos.local`. **Required** in production. |

Production behaviour is unchanged and still fails fast: weak/missing
`COREOS_SESSION_SECRET`, missing `COREOS_ALLOWED_ORIGINS`, a non-HTTPS public
origin, or `COREOS_COOKIE_SECURE=0` all abort startup.

---

## 6. Verifying the secure context

**Automated (already covered by `pytest`):**

* `tests/test_secure_dev_foundation.py` — cookie/CORS config, production
  guard rails, no browser-facing `http://` backend URL in `app/**`, same-origin
  API default + mixed-content guard, `/api` rewrite present.

**Manual, in the browser dev console at `https://coreos.local`:**

```js
window.isSecureContext            // must be true
window.location.protocol          // "https:"
'geolocation' in navigator        // true (capability only — NOT proof GPS works)
performance.getEntriesByType('resource')
  .map(r => r.name)
  .filter(n => n.startsWith('http://'))   // must be []  (no mixed content)
```

`window.isSecureContext` is **browser runtime state** — no backend endpoint can
or should claim to know it. There is deliberately no server "am I secure?" API.

**Detecting mixed content:** the browser console logs
`Mixed Content: … was loaded over HTTPS, but requested an insecure resource …`
and the request is blocked. With `NEXT_PUBLIC_API_BASE_URL` blank this cannot
happen; if someone sets an `http://` base, `apiUrl()` throws a clear config
error instead of issuing the request.

---

## 7. Later: trusting the CA on a physical iPhone (Phase 5B, not done yet)

> These are **instructions only**. Phase 5A has not performed or tested this.

1. Export Caddy's root CA on the PC:
   ```powershell
   caddy trust        # if not already done
   # CA file location:
   "%AppData%\Caddy\pki\authorities\local\root.crt"
   ```
2. Send `root.crt` to the iPhone (AirDrop, or email to yourself).
3. iPhone: open the file → **Settings ▸ Profile Downloaded ▸ Install**.
4. iPhone: **Settings ▸ General ▸ About ▸ Certificate Trust Settings** →
   enable full trust for the "Caddy Local Authority" root.
5. Make `coreos.local` resolve on the iPhone to the PC's LAN IP — either add an
   `A` record on the router's DNS, or use the PC's own mDNS name
   (`DESKTOP-R54QCKA.local` on this machine) as `COREOS_DEV_HOST` instead.
6. On the same Wi-Fi, open `https://coreos.local` (or the chosen name) in Safari.
7. Only then run the physical secure-context / geolocation acceptance test.

---

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `https://coreos.local` → *your connection is not private* | Run `caddy trust` (elevated); restart the browser. |
| Login/Signup → "Couldn't reach the server" but nothing hits Caddy/Next/FastAPI | Stale `NEXT_PUBLIC_API_BASE_URL` in `.env` (e.g. an old `http://<LAN-IP>:8080`). Blank it, then **restart `npm run dev`**. Console shows `CoreOS config error: …`. |
| `coreos.local` does not resolve | Add `127.0.0.1 coreos.local` to the Windows hosts file (elevated). |
| API calls 404 / HTML instead of JSON | FastAPI (terminal 1) is not running, or `COREOS_BACKEND_ORIGIN` is wrong. |
| Login works but you are logged out on the next request | `COREOS_COOKIE_SECURE=1` but you opened the app over `http://` — use `https://coreos.local`. |
| CORS error in the console | You are hitting the API cross-origin (not via the proxy). Use the proxy origin, or add your origin to `COREOS_ALLOWED_ORIGINS`. |
| Caddy: *permission denied* binding :443 | Run the `npm run dev:proxy` terminal as Administrator, or set an alternate `https_port` in `dev/Caddyfile`. |
| HMR / live reload not updating | Confirm `COREOS_DEV_HOST` is in `allowedDevOrigins` (it is, via `next.config.ts`); hard-refresh. |

---

## 9. Reverting to plain localhost development

Phase 5A is additive. To go back to the pre-5A flow:

* Don't start Caddy (`npm run dev:proxy`).
* In `.env` set `COREOS_COOKIE_SECURE=` (blank) and either keep
  `NEXT_PUBLIC_API_BASE_URL=` blank (same-origin via the Next `/api` rewrite —
  recommended) or `NEXT_PUBLIC_API_BASE_URL=http://localhost:8080` (direct).
* Open `http://localhost:3000`.

No code changes, no migration, nothing to undo.

---

## 10. Location acquisition (Phase 5B)

CoreOS acquires the user's **real** device location from the browser
(`navigator.geolocation.getCurrentPosition`). There is **no fake fallback** — if
a fix cannot be obtained, the UI states the specific reason.

**Requirements**

* **HTTPS.** Geolocation is a secure-context API. Use `https://coreos.local`
  (Phase 5A). `localhost` also counts as secure for desktop testing.
* **User permission.** The browser prompts on the first request. Denial is
  remembered by the browser until the user changes the site setting.

**The service** — `app/lib/location.ts`

* `getCurrentLocation()` → resolves (never rejects) to a typed result:
  `AVAILABLE { coordinates }` or an error state
  (`PERMISSION_DENIED`, `POSITION_UNAVAILABLE`, `TIMEOUT`, `INSECURE_CONTEXT`,
  `UNSUPPORTED`, `INVALID_COORDINATES`, `UNKNOWN_ERROR`;
  callers add `BACKEND_UNREACHABLE` for the validate round-trip).
* Options: `timeout 15 s`, `maximumAge 120 s`, `enableHighAccuracy false`
  (neighbourhood accuracy is enough for place search; faster, lower-battery,
  better indoors). One call = at most one permission prompt; no auto-retry.
* Point-in-time only — **no `watchPosition` / continuous tracking**.
* Pure helpers (`validateCoordinates`, `normalizeGeolocationError`,
  `isLocationFresh`, …) are unit-tested via `npm run test:node`
  (Node's built-in runner; zero deps).

**Backend** — `POST /api/location/validate`

* Authoritative, **stateless** coordinate validation (frontend checks are not
  trusted alone). Rejects NaN / Infinity / out-of-range / missing values with a
  **coordinate-free** 422 message.
* Nothing is stored (no table, no migration) and nothing is logged.

**Privacy boundary**

* Coordinates live only in React component state (in memory); cleared on
  navigation / reload / "Go back".
* Sent to the backend **only in a POST body**, never a URL/query string.
* **Never** persisted, logged, put in an error message, or sent to the AI model.
* No location-history storage anywhere.

**Troubleshooting**

| Symptom | Fix |
|---|---|
| "Location access is off." | The browser has geolocation blocked for this site. Enable it in the site settings (padlock ▸ Site settings ▸ Location), then "Try again". |
| "Location needs a secure connection." | You opened the app over `http://`. Use `https://coreos.local`. |
| "We couldn't get your location in time." | Transient (indoors / cold GPS). Press "Try again". |

**Physical-device limit:** a working `https://coreos.local` + granted permission
on this PC does **not** prove iPhone behaviour. Device name-resolution, CA trust,
and the iOS permission prompt are verified separately later in Phase 5.
