// Phase 5G — pure helpers for the shared "Add to Schedule" flow (no DOM, no
// React). Unit-tested with `npm run test:node`.
//
// The backend is the authority: it re-validates every field, owns conflict
// detection and enforces idempotency via `client_token`. These helpers only
// build a truthful draft and a well-formed request body.

import type { EventPlaceRef } from "../lib/types";

// CoreOS schedule times are Asia/Kuala_Lumpur wall-clock (Phase 4 — no DST).
// Mirrors `MYT_OFFSET` in app/lib/datetime.ts; kept local so this module stays
// dependency-free and node-testable.
const MYT_OFFSET = "+08:00";

/**
 * Intelligent, TRUTHFUL title prefill:
 *   * activity intent + place  ->  "Futsal at Elite Futsal Arena"
 *   * place only               ->  "Elite Futsal Arena"
 * The user must still be able to see and edit this before confirming.
 */
export function prefillTitle(opts: { placeName: string; activityLabel?: string | null }): string {
  const place = opts.placeName.trim();
  const act = (opts.activityLabel || "").trim();
  if (act && place) return `${act} at ${place}`;
  return place;
}

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

/** The MYT wall-clock parts of an instant (CoreOS schedule tz — Phase 4). */
function mytParts(ms: number): { y: number; mo: number; d: number; h: number; mi: number } {
  const shifted = new Date(ms + 8 * 3600 * 1000);
  return {
    y: shifted.getUTCFullYear(),
    mo: shifted.getUTCMonth() + 1,
    d: shifted.getUTCDate(),
    h: shifted.getUTCHours(),
    mi: shifted.getUTCMinutes(),
  };
}

function toInput(p: { y: number; mo: number; d: number; h: number; mi: number }): string {
  return `${p.y}-${pad(p.mo)}-${pad(p.d)}T${pad(p.h)}:${pad(p.mi)}`;
}

/**
 * Default one-hour window for a new event: the next full hour (MYT), to the hour
 * after. Returns `<input type="datetime-local">` strings (MYT wall time), exactly
 * like the existing Schedule EventSheet.
 */
export function roundedWindow(nowMs: number): { startInput: string; endInput: string } {
  const start = mytParts(nowMs + 3600 * 1000);
  start.mi = 0;
  const endMs = Date.UTC(start.y, start.mo - 1, start.d, start.h, 0) + 3600 * 1000 - 8 * 3600 * 1000;
  const end = mytParts(endMs);
  return { startInput: toInput(start), endInput: toInput(end) };
}

/** `<input type="datetime-local">` value (MYT wall time) -> ISO with +08:00. */
export function localInputToMyt(v: string): string {
  return v ? `${v}:00${MYT_OFFSET}` : "";
}

/**
 * Client-side pre-check ONLY (fast feedback). The backend still re-validates and
 * owns conflict rules. Returns an error string, or null when the draft looks
 * sendable.
 */
export function draftError(input: { title: string; startInput: string; endInput: string }): string | null {
  if (!input.title.trim()) return "Give the event a title.";
  if (!input.startInput) return "Pick a start time.";
  if (!input.endInput) return "Pick an end time.";
  if (input.endInput <= input.startInput) return "The end time must be after the start time.";
  return null;
}

/**
 * Assemble the POST /api/events body. `client_token` is generated ONCE per open
 * of the form (not per click) so a double-click / retry / Strict-Mode replay all
 * carry the SAME token and the backend returns the first event instead of a
 * duplicate.
 */
export function buildCreateBody(input: {
  title: string;
  startInput: string;
  endInput: string;
  ref: EventPlaceRef;
  clientToken: string;
  environment?: "indoor" | "outdoor" | "uncertain";
}): Record<string, unknown> {
  const body: Record<string, unknown> = {
    title: input.title.trim(),
    start: localInputToMyt(input.startInput),
    end: localInputToMyt(input.endInput),
    all_day: false,
    place: {
      provider: input.ref.provider,
      provider_place_id: input.ref.provider_place_id,
      name: input.ref.name,
      address: input.ref.address ?? undefined,
      latitude: input.ref.latitude,
      longitude: input.ref.longitude,
    },
    client_token: input.clientToken,
  };
  if (input.environment) body.environment = input.environment;
  return body;
}
