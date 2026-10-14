// Batch 01 — pure, unit-testable helpers for the Personal Details name-edit
// form. No React, no fetch: the page component is a thin consumer so the
// validation/dirty-check logic can be tested with `node --test` the same
// way every other CoreOS form helper is (see app/schedule/addToSchedule.ts).

export const NAME_MAX_LENGTH = 120;

/**
 * Trim + validate a candidate name. Returns null when valid, else a short,
 * truthful error message to show the user. An over-limit name is REJECTED,
 * never silently truncated.
 */
export function validateName(raw: string): string | null {
  const trimmed = raw.trim();
  if (!trimmed) return "Enter a name.";
  if (trimmed.length > NAME_MAX_LENGTH) {
    return `Name must be at most ${NAME_MAX_LENGTH} characters.`;
  }
  return null;
}

/** Has the field's current value actually changed from the last saved name? */
export function isDirty(current: string, saved: string | null | undefined): boolean {
  return current.trim() !== (saved ?? "").trim();
}

/**
 * "MA" for "Mosab Alsadiq", "M" for "Mosab", "?" when there's neither a real
 * name nor email yet — always derived from real account data, never a
 * placeholder image or a fabricated photo (Figma 315:130's "Change photo" is
 * not backed by any avatar-upload capability, so this stays initials-only).
 */
export function initials(name: string | null | undefined, email: string | null | undefined): string {
  const source = (name ?? "").trim();
  if (source) {
    const parts = source.split(/\s+/).filter(Boolean);
    if (parts.length === 1) return parts[0].charAt(0).toUpperCase();
    return (parts[0].charAt(0) + parts[parts.length - 1].charAt(0)).toUpperCase();
  }
  const e = (email ?? "").trim();
  return e ? e.charAt(0).toUpperCase() : "?";
}
