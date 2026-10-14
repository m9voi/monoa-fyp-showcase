# MONOA

**Final Year Project demo — under active development.**

MONOA is a personal life-assistant web app built as a Final Year Project (FYP). It combines a daily schedule, task tracking, manual expense tracking, and a conversational assistant ("Mo") behind a single login, with an experimental iOS packaging attempt on top of the same web app.

This is a working prototype, not a finished product, a published mobile app, or a commercially launched service. The FYP presentation and submission are complete; the codebase has continued to receive fixes and small features afterward and is not a static snapshot. Several areas listed under Limitations below are real, known gaps — this README does not hide them.

## What's actually implemented

The app has five main areas, all backed by a real FastAPI + SQLite backend (no mock data):

- **Today** — a daily dashboard: the next event, open tasks, today's spending against budget, and any pending Activity recommendations.
- **Plan** — full schedule management: one-off and recurring events, tasks with due dates/reminders, and conflict detection between overlapping events and between a task and an event.
- **Mo** — a conversational layer over the app's own real actions (see below), not a separate or decorative chatbot.
- **Activity** — real nearby-place search (Google Places or OpenStreetMap, depending on configuration) for a chosen activity, with results added to Plan as real events.
- **Wallet** — manual income/expense tracking against daily/weekly/monthly budgets, with a transaction history and category breakdown.

Supporting features: email/password authentication (Argon2id hashing, server-side sessions), a "Who Am I" interest profile used to lightly personalize Mo, account settings (appearance, notifications, location toggle), and account security (change password, change email, delete account).

### Mo and the confirmation flow

Mo understands natural-language requests for the actions above — creating or editing an event or task, logging an expense, or asking a read-only question about your schedule or spending. Every action that would change your data follows the same pattern:

1. Mo interprets the request and proposes a specific, concrete change (never a vague "ok, doing that").
2. You see exactly what will happen and must explicitly confirm it.
3. Only on confirmation does the backend execute it, through the *same* validated code path a manual edit in Plan or Wallet would use — there is no separate, less-validated "AI path."
4. If a request is ambiguous (e.g. it could match more than one event) or missing required information, Mo asks a clarifying question instead of guessing.

**Mo's request understanding is a known weak point, not a solved one.** It handles clear, direct phrasing well, but natural variation — unusual word order, multi-step requests, or a vague follow-up like "move it" — can still produce a clarifying question when a human would have understood immediately, or in rarer cases fail to match an existing record. This is an active area of work, not something this README is overstating.

### Manual finance, not bank integration

Wallet is manual-entry only. There is no bank or card connection, no transaction import, and no real money is held or moved anywhere by this app — "income" and "expense" are figures the user types in. Budgets are simple thresholds the user sets.

### Activity, location, and weather depend on external services

Place search (Activity, Map) and weather context call real third-party APIs (Google Places and/or OpenStreetMap/Overpass for places, Open-Meteo for weather) and therefore need outbound internet access and, for Google, a valid API key. Without a configured key the app falls back to OpenStreetMap where supported, or reports an honest "unavailable" state — it does not fabricate results.

## Technology stack

- **Frontend:** Next.js 16 (App Router), React 19, TypeScript, Tailwind CSS 4.
- **Backend:** FastAPI (Python 3.14 in development), Pydantic 2, Uvicorn.
- **Database:** SQLite, schema-managed with Alembic migrations.
- **Auth:** Argon2id password hashing (`argon2-cffi`), server-side sessions.
- **AI:** OpenAI's API for Mo's natural-language understanding.
- **Maps/places/weather:** Google Places API and/or OpenStreetMap (Overpass/Nominatim), Open-Meteo.
- **Mobile:** Capacitor (iOS) — see Limitations; this is an early proof, not a shipped app.
- **Testing:** `pytest` (backend), Node's built-in test runner (frontend pure-logic modules).

## Setup

These commands were run and verified against the current code while preparing this README.

### Backend

```bash
python -m pip install -r requirements.txt
```

Create a `.env` file in the project root (see `.env.example` for every variable and what it does — copy it and fill in real values; never commit the real `.env`). At minimum, set `OPENAI_API_KEY` for Mo to work. Then run migrations and start the API:

```bash
python -m alembic upgrade head
python main.py
```

The backend listens on `127.0.0.1:8080` by default (loopback-only by design — see `.env.example` for how the frontend reaches it).

### Frontend

```bash
npm install
npm run dev
```

Open `http://localhost:3000`. The frontend proxies `/api/*` to the backend server-side, so no separate API URL configuration is needed for local use.

### Tests

```bash
# Backend — IMPORTANT: point COREOS_DB_PATH at a disposable file first;
# main.py runs migrations against whatever database it resolves to.
COREOS_DB_PATH=/path/to/a/throwaway.db python -m pytest

# Frontend (pure-logic modules only — no browser/DOM required)
npm run test:node
```

## Test results

**Current — run 14 October 2026**, on a disposable database (never the real one), as part of preparing this README:

- Backend: **1,324 / 1,324** pytest tests passing.
- Frontend: **314 / 314** Node tests passing (pure logic only — these don't exercise the UI in a browser).
- `npx tsc --noEmit`: clean.
- `npm run build`: succeeds, all 21 routes compile.

(Two test files initially failed this run — `test_recurring_conflicts.py` and `test_task_conflicts.py` — because their fixtures hardcoded calendar dates relative to when they were written; the backend's conflict-recomputation only expands occurrences within a bounded window around "now," so those dates had aged out of it. The fixtures now compute their dates relative to the actual run time instead, so this can't recur. No application code changed as a result of this — it was a test-fixture bug, not a functional one.)

**Historical — FYP report snapshot (mid-September 2026, around the submission date):** 1,309 backend + 313 frontend = 1,622 tests passing. This figure is cited only as the report's own snapshot from that date, not re-verified today — the codebase has grown since. It is not combined with, or used to replace, the current numbers above.

## Limitations (current, honest)

- **Mo's language understanding is imperfect** — see "Mo and the confirmation flow" above.
- **iOS packaging is an early experiment, not a working app.** A Capacitor project exists in `ios/`, but it has only been exercised against a temporary development tunnel and is not configured for a real deployment target. Treat it as a proof that the web app *can* be wrapped, not as something installable today.
- **No real hosting.** The backend binds to `127.0.0.1` only and expects a trusted HTTPS reverse proxy (a local Caddy config is included for development/demo use over a LAN or a temporary tunnel) — there is no production deployment.
- **Single-process, development-grade backend.** Rate limiting and the background reminder scheduler are in-process and not designed for multiple server instances.
- **No automated accessibility or cross-browser audit** has been performed; manual testing has focused on desktop Chrome and Safari on iOS.

## Planned / not yet built

These are genuinely future work, not implemented in any partial form:

- A production deployment target for the backend (and a corresponding non-development frontend configuration).
- A real, store-distributable iOS (and/or Android) build.
- Improving Mo's natural-language robustness (the known weak point above).
- Any bank/financial-institution integration for Wallet (there are currently no plans to hold or move real funds — manual entry is a deliberate scope choice, not a stopgap).

## CI

`.github/workflows/ios-unsigned-build.yml` is a manual (workflow-dispatch only), unsigned build check for the experimental iOS shell — it builds the app without code signing to confirm the Capacitor/Xcode project still compiles. It does not publish, sign, or distribute anything.

## License

No license has been chosen for this repository yet. Without one, default copyright applies — visitors may view and read the code, but have no explicit legal permission to reuse, modify, or redistribute it. This is a deliberate placeholder, not an oversight: pick a license (e.g. MIT) when you're ready to explicitly invite reuse.
