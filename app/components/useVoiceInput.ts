"use client";
// Mo voice input (Product Completion Batch 03) — the browser-API glue layer.
// Every decision this hook makes about WHAT state comes next is delegated to
// the pure, unit-tested nextVoiceState() reducer in ../lib/voiceRecording;
// this file only ever calls the real MediaRecorder/getUserMedia APIs and the
// real /api/mo/transcribe endpoint. The result is handed back via
// `onTranscribed(text)` — the caller (OmniBar) puts it straight into the
// SAME `input` state its existing composer already uses. No second Mo
// pipeline exists here; this hook never calls /api/process-input itself.

import { useCallback, useRef, useState } from "react";
import { apiFetch, ApiError } from "../lib/api";
import {
  nextVoiceState,
  pickSupportedMimeType,
  VOICE_MAX_SECONDS,
  VOICE_ERROR_MESSAGES,
  type VoiceState,
  type VoiceErrorReason,
  type VoiceEvent,
} from "../lib/voiceRecording";

function blobToBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onloadend = () => {
      const result = reader.result as string;
      const comma = result.indexOf(",");
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
}

export function useVoiceInput(onTranscribed: (text: string) => void) {
  const [state, setState] = useState<VoiceState>("idle");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);

  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const dispatch = useCallback((event: VoiceEvent) => {
    setState((s) => nextVoiceState(s, event));
  }, []);

  const clearTimer = useCallback(() => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const stopStream = useCallback(() => {
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
  }, []);

  const transcribe = useCallback(
    async (blob: Blob) => {
      // Console-only diagnostics (no secrets, no audio/transcript content) —
      // lets a real device test show exactly what MediaRecorder actually
      // produced and exactly how the request failed, instead of only the one
      // generic message every failure reason collapses to below.
      console.log("[MoVoice] recorded blob:", { type: blob.type, size: blob.size });
      try {
        const audio_base64 = await blobToBase64(blob);
        const mime_type = blob.type || "audio/webm";
        const res = await apiFetch<{ transcript: string }>("/api/mo/transcribe", {
          method: "POST",
          kind: "ai",
          body: JSON.stringify({ audio_base64, mime_type }),
        });
        const text = (res.transcript || "").trim();
        if (!text) {
          setErrorMessage(VOICE_ERROR_MESSAGES.no_speech);
          dispatch({ type: "no_speech_detected" });
          return;
        }
        onTranscribed(text);
        dispatch({ type: "transcribe_success" });
      } catch (err) {
        const reason: VoiceErrorReason =
          err instanceof ApiError && (err.kind === "network" || err.kind === "offline")
            ? "network"
            : "transcription_failed";
        console.log("[MoVoice] transcribe failed:", {
          kind: err instanceof ApiError ? err.kind : "unknown",
          status: err instanceof ApiError ? err.status : 0,
          message: err instanceof Error ? err.message : String(err),
        });
        setErrorMessage(VOICE_ERROR_MESSAGES[reason]);
        dispatch({ type: "transcribe_error", reason });
      } finally {
        setElapsedSeconds(0);
        mediaRecorderRef.current = null;
      }
    },
    [dispatch, onTranscribed],
  );

  const startRecording = useCallback(async () => {
    if (state !== "idle" && state !== "transcribed" && state !== "error") return; // no duplicate taps
    setErrorMessage(null);
    dispatch({ type: "mic_tap" }); // -> requesting_permission

    if (
      typeof window === "undefined" ||
      typeof window.MediaRecorder === "undefined" ||
      !navigator.mediaDevices?.getUserMedia
    ) {
      setErrorMessage(VOICE_ERROR_MESSAGES.unsupported);
      dispatch({ type: "unsupported" });
      return;
    }

    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      const name = err instanceof DOMException ? err.name : "";
      const permissionDenied = name === "NotAllowedError" || name === "SecurityError";
      setErrorMessage(VOICE_ERROR_MESSAGES[permissionDenied ? "permission_denied" : "mic_unavailable"]);
      dispatch({ type: permissionDenied ? "permission_denied" : "mic_unavailable" });
      return;
    }
    streamRef.current = stream;

    const mimeType = pickSupportedMimeType(
      (m) => (typeof MediaRecorder.isTypeSupported === "function" ? MediaRecorder.isTypeSupported(m) : false),
    );
    console.log("[MoVoice] selected mimeType:", mimeType);

    let recorder: MediaRecorder;
    try {
      recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
    } catch {
      stopStream();
      setErrorMessage(VOICE_ERROR_MESSAGES.unsupported);
      dispatch({ type: "unsupported" });
      return;
    }

    chunksRef.current = [];
    recorder.ondataavailable = (e: BlobEvent) => {
      if (e.data && e.data.size > 0) chunksRef.current.push(e.data);
    };
    recorder.onstop = () => {
      clearTimer();
      stopStream();
      console.log("[MoVoice] recorder.mimeType at stop:", recorder.mimeType);
      const blob = new Blob(chunksRef.current, { type: recorder.mimeType || mimeType || "audio/webm" });
      chunksRef.current = [];
      void transcribe(blob);
    };

    mediaRecorderRef.current = recorder;
    recorder.start();
    dispatch({ type: "permission_granted" }); // -> recording
    setElapsedSeconds(0);
    timerRef.current = setInterval(() => {
      setElapsedSeconds((s) => {
        const next = s + 1;
        if (next >= VOICE_MAX_SECONDS && mediaRecorderRef.current?.state === "recording") {
          dispatch({ type: "max_duration_reached" }); // -> transcribing
          mediaRecorderRef.current.stop(); // triggers onstop -> transcribe()
        }
        return next;
      });
    }, 1000);
  }, [state, dispatch, transcribe, clearTimer, stopStream]);

  const stopRecording = useCallback(() => {
    if (state !== "recording" || !mediaRecorderRef.current) return;
    dispatch({ type: "stop" }); // -> transcribing
    mediaRecorderRef.current.stop(); // triggers onstop -> transcribe()
  }, [state, dispatch]);

  const cancelRecording = useCallback(() => {
    // Detach onstop FIRST so cancelling can never trigger a transcription
    // call — the whole point of Cancel is that no audio is sent anywhere.
    if (mediaRecorderRef.current) {
      mediaRecorderRef.current.onstop = null;
      if (mediaRecorderRef.current.state !== "inactive") {
        try {
          mediaRecorderRef.current.stop();
        } catch {
          /* already stopped */
        }
      }
    }
    clearTimer();
    stopStream();
    chunksRef.current = [];
    mediaRecorderRef.current = null;
    setElapsedSeconds(0);
    setErrorMessage(null);
    dispatch({ type: "cancel" });
  }, [dispatch, clearTimer, stopStream]);

  const dismissError = useCallback(() => {
    setErrorMessage(null);
    dispatch({ type: "dismiss_error" });
  }, [dispatch]);

  return {
    state,
    errorMessage,
    elapsedSeconds,
    maxSeconds: VOICE_MAX_SECONDS,
    startRecording,
    stopRecording,
    cancelRecording,
    dismissError,
  };
}
