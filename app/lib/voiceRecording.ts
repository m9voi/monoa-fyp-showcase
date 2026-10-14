// Mo voice input (Product Completion Batch 03) — pure, browser-API-free
// pieces: the recording state machine and MIME negotiation. The actual
// MediaRecorder/getUserMedia glue lives in useVoiceInput.ts (a thin hook
// that is not itself unit-testable in Node); everything that CAN be pure
// is pulled out here so it's directly testable, matching this codebase's
// established pattern (moProposal.ts, personalDetails.ts, interestSelection.ts).

export type VoiceState =
  | "idle"
  | "requesting_permission"
  | "recording"
  | "transcribing"
  | "transcribed"
  | "error";

export type VoiceErrorReason =
  | "permission_denied"
  | "mic_unavailable"
  | "unsupported"
  | "no_speech"
  | "transcription_failed"
  | "network";

export type VoiceEvent =
  | { type: "mic_tap" }
  | { type: "permission_granted" }
  | { type: "permission_denied" }
  | { type: "mic_unavailable" }
  | { type: "unsupported" }
  | { type: "stop" }
  | { type: "max_duration_reached" }
  | { type: "cancel" }
  | { type: "transcribe_success" }
  | { type: "no_speech_detected" }
  | { type: "transcribe_error"; reason?: VoiceErrorReason }
  | { type: "dismiss_error" }
  | { type: "reset" };

/**
 * One explicit state machine, testable with zero DOM/browser APIs. The
 * React hook (useVoiceInput) is a thin adapter around this — it never
 * invents its own ad-hoc transitions.
 */
export function nextVoiceState(current: VoiceState, event: VoiceEvent): VoiceState {
  switch (current) {
    case "idle":
      return event.type === "mic_tap" ? "requesting_permission" : current;

    case "requesting_permission":
      if (event.type === "permission_granted") return "recording";
      if (event.type === "permission_denied") return "error";
      if (event.type === "mic_unavailable") return "error";
      if (event.type === "unsupported") return "error";
      if (event.type === "cancel") return "idle";
      return current;

    case "recording":
      if (event.type === "stop" || event.type === "max_duration_reached") return "transcribing";
      if (event.type === "cancel") return "idle";
      return current;

    case "transcribing":
      if (event.type === "transcribe_success") return "transcribed";
      if (event.type === "no_speech_detected") return "error";
      if (event.type === "transcribe_error") return "error";
      // A recording can still be cancelled conceptually before the user
      // acts on the result, but the in-flight request itself cannot be
      // aborted mid-way in a way that un-transcribes it — once STOP was
      // pressed we run to a real outcome (success or error), never silently
      // discard a request the user already committed to.
      return current;

    case "transcribed":
      // Functionally equivalent to idle (mic available again, text sits in
      // the normal editable input) — kept as its own named state per the
      // product spec, distinct for testability even though the composer
      // renders it the same way idle renders.
      if (event.type === "mic_tap") return "requesting_permission";
      if (event.type === "reset") return "idle";
      return current;

    case "error":
      if (event.type === "dismiss_error" || event.type === "reset") return "idle";
      if (event.type === "mic_tap") return "requesting_permission";
      return current;

    default:
      return current;
  }
}

export function isRecordingActive(state: VoiceState): boolean {
  return state === "recording";
}

export function isVoiceBusy(state: VoiceState): boolean {
  return state === "requesting_permission" || state === "recording" || state === "transcribing";
}

export const VOICE_ERROR_MESSAGES: Record<VoiceErrorReason, string> = {
  permission_denied: "Microphone access was denied. You can still type your message.",
  mic_unavailable: "No microphone is available on this device.",
  unsupported: "Voice input isn't supported in this browser. You can still type your message.",
  no_speech: "I didn't catch any speech. Please try again.",
  transcription_failed: "Couldn't transcribe that. Please try again.",
  network: "Couldn't reach MONOA to transcribe that. Check your connection and try again.",
};

// --- Recording limits (Batch 03 — documented, demo-safe bounds) ---
// A short voice COMMAND, not a long recording. Mirrors the backend's
// COREOS_MO_TRANSCRIBE_MAX_SECONDS default (security_config.py).
export const VOICE_MAX_SECONDS = 30;

// --- MIME negotiation ---
// Ordered by preference: Opus-in-WebM/Ogg (Chromium/Firefox) first since
// it's the most efficient, then AAC-in-MP4 (Safari/iOS, which does not
// support WebM), then a couple of plain fallbacks. The caller supplies its
// own `isSupported` probe (MediaRecorder.isTypeSupported in the real hook)
// so this stays pure and testable with a fake probe.
export const VOICE_CANDIDATE_MIME_TYPES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/ogg;codecs=opus",
  "audio/ogg",
  "audio/mp4;codecs=mp4a.40.2",
  "audio/mp4",
  "audio/aac",
  "audio/mpeg",
  "audio/wav",
] as const;

/**
 * Pick the first candidate the browser actually supports, or null if none
 * are (the caller then falls back to MediaRecorder's unspecified default,
 * per the task's "browser default fallback" requirement, and reads the
 * REAL resulting Blob.type afterward rather than assuming one).
 */
export function pickSupportedMimeType(
  isSupported: (mime: string) => boolean,
  candidates: readonly string[] = VOICE_CANDIDATE_MIME_TYPES,
): string | null {
  for (const mime of candidates) {
    if (isSupported(mime)) return mime;
  }
  return null;
}

/** "0:07", "1:03" — for the recording indicator. Clamped to >= 0. */
export function formatElapsed(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return `${m}:${rem.toString().padStart(2, "0")}`;
}
