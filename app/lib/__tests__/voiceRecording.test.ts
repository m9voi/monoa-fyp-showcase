import { test } from "node:test";
import assert from "node:assert/strict";

import {
  nextVoiceState,
  pickSupportedMimeType,
  formatElapsed,
  isRecordingActive,
  isVoiceBusy,
  VOICE_CANDIDATE_MIME_TYPES,
  type VoiceState,
} from "../voiceRecording.ts";

// --------------------------------------------------------------------------- #
// nextVoiceState — the full recording lifecycle
// --------------------------------------------------------------------------- #
test("idle -> requesting_permission on mic_tap", () => {
  assert.equal(nextVoiceState("idle", { type: "mic_tap" }), "requesting_permission");
});

test("idle ignores every other event", () => {
  for (const type of ["stop", "cancel", "transcribe_success"] as const) {
    assert.equal(nextVoiceState("idle", { type }), "idle");
  }
});

test("requesting_permission -> recording on permission_granted", () => {
  assert.equal(nextVoiceState("requesting_permission", { type: "permission_granted" }), "recording");
});

test("requesting_permission -> error on permission_denied / mic_unavailable / unsupported", () => {
  assert.equal(nextVoiceState("requesting_permission", { type: "permission_denied" }), "error");
  assert.equal(nextVoiceState("requesting_permission", { type: "mic_unavailable" }), "error");
  assert.equal(nextVoiceState("requesting_permission", { type: "unsupported" }), "error");
});

test("requesting_permission -> idle on cancel", () => {
  assert.equal(nextVoiceState("requesting_permission", { type: "cancel" }), "idle");
});

test("recording -> transcribing on stop", () => {
  assert.equal(nextVoiceState("recording", { type: "stop" }), "transcribing");
});

test("recording -> transcribing on max_duration_reached (auto-stop)", () => {
  assert.equal(nextVoiceState("recording", { type: "max_duration_reached" }), "transcribing");
});

test("recording -> idle on cancel (no transcription happens)", () => {
  assert.equal(nextVoiceState("recording", { type: "cancel" }), "idle");
});

test("transcribing -> transcribed on transcribe_success", () => {
  assert.equal(nextVoiceState("transcribing", { type: "transcribe_success" }), "transcribed");
});

test("transcribing -> error on no_speech_detected", () => {
  assert.equal(nextVoiceState("transcribing", { type: "no_speech_detected" }), "error");
});

test("transcribing -> error on transcribe_error", () => {
  assert.equal(
    nextVoiceState("transcribing", { type: "transcribe_error", reason: "network" }),
    "error",
  );
});

test("transcribing ignores cancel — an in-flight request runs to a real outcome", () => {
  assert.equal(nextVoiceState("transcribing", { type: "cancel" }), "transcribing");
});

test("error -> idle on dismiss_error or reset", () => {
  assert.equal(nextVoiceState("error", { type: "dismiss_error" }), "idle");
  assert.equal(nextVoiceState("error", { type: "reset" }), "idle");
});

test("error -> requesting_permission on mic_tap (retry)", () => {
  assert.equal(nextVoiceState("error", { type: "mic_tap" }), "requesting_permission");
});

test("transcribed -> requesting_permission on mic_tap (record again)", () => {
  assert.equal(nextVoiceState("transcribed", { type: "mic_tap" }), "requesting_permission");
});

test("transcribed -> idle on reset", () => {
  assert.equal(nextVoiceState("transcribed", { type: "reset" }), "idle");
});

test("every state is reachable from idle through a realistic sequence", () => {
  let s: VoiceState = "idle";
  s = nextVoiceState(s, { type: "mic_tap" });
  assert.equal(s, "requesting_permission");
  s = nextVoiceState(s, { type: "permission_granted" });
  assert.equal(s, "recording");
  s = nextVoiceState(s, { type: "stop" });
  assert.equal(s, "transcribing");
  s = nextVoiceState(s, { type: "transcribe_success" });
  assert.equal(s, "transcribed");
});

// --------------------------------------------------------------------------- #
// isRecordingActive / isVoiceBusy
// --------------------------------------------------------------------------- #
test("isRecordingActive is true only while recording", () => {
  assert.equal(isRecordingActive("recording"), true);
  for (const s of ["idle", "requesting_permission", "transcribing", "transcribed", "error"] as VoiceState[]) {
    assert.equal(isRecordingActive(s), false);
  }
});

test("isVoiceBusy covers permission/recording/transcribing only", () => {
  assert.equal(isVoiceBusy("requesting_permission"), true);
  assert.equal(isVoiceBusy("recording"), true);
  assert.equal(isVoiceBusy("transcribing"), true);
  assert.equal(isVoiceBusy("idle"), false);
  assert.equal(isVoiceBusy("transcribed"), false);
  assert.equal(isVoiceBusy("error"), false);
});

// --------------------------------------------------------------------------- #
// pickSupportedMimeType — MIME negotiation
// --------------------------------------------------------------------------- #
test("picks the first supported candidate in preference order", () => {
  const supported = new Set(["audio/mp4", "audio/aac"]);
  const picked = pickSupportedMimeType((m) => supported.has(m));
  assert.equal(picked, "audio/mp4"); // earlier in VOICE_CANDIDATE_MIME_TYPES than aac
});

test("prefers webm/opus when the browser supports it (Chromium-like)", () => {
  const supported = new Set(["audio/webm;codecs=opus", "audio/webm", "audio/wav"]);
  assert.equal(pickSupportedMimeType((m) => supported.has(m)), "audio/webm;codecs=opus");
});

test("falls back to mp4/aac when webm is unsupported (Safari-like)", () => {
  const supported = new Set(["audio/mp4;codecs=mp4a.40.2", "audio/mp4", "audio/aac"]);
  assert.equal(pickSupportedMimeType((m) => supported.has(m)), "audio/mp4;codecs=mp4a.40.2");
});

test("returns null when nothing is supported (caller falls back to browser default)", () => {
  assert.equal(pickSupportedMimeType(() => false), null);
});

test("never hard-codes a single format — the candidate list has more than one entry", () => {
  assert.ok(VOICE_CANDIDATE_MIME_TYPES.length > 1);
  assert.ok(VOICE_CANDIDATE_MIME_TYPES.some((m) => m.includes("webm")));
  assert.ok(VOICE_CANDIDATE_MIME_TYPES.some((m) => m.includes("mp4")));
});

// --------------------------------------------------------------------------- #
// formatElapsed
// --------------------------------------------------------------------------- #
test("formatElapsed formats seconds as m:ss", () => {
  assert.equal(formatElapsed(0), "0:00");
  assert.equal(formatElapsed(7), "0:07");
  assert.equal(formatElapsed(59), "0:59");
  assert.equal(formatElapsed(60), "1:00");
  assert.equal(formatElapsed(65), "1:05");
});

test("formatElapsed clamps negative input to 0:00", () => {
  assert.equal(formatElapsed(-5), "0:00");
});

test("formatElapsed floors fractional seconds", () => {
  assert.equal(formatElapsed(7.9), "0:07");
});
