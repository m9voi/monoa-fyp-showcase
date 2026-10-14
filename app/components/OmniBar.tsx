"use client";

// The ONE canonical Mo command surface — shared verbatim by Today and /mo.
//
// B2: Mo is human-in-the-loop. A submission is UNDERSTOOD by the server; a
// supported change comes back as a PENDING PROPOSAL that the user must Confirm
// (or Cancel). Only Confirm executes, through the same canonical service Plan /
// Wallet use, exactly once. A pending / just-executed proposal is restored from
// the server after a refresh (GET /api/process-input/pending) — the original
// request is never resubmitted.
//
// "MO INTERACTION + ACTION UX CLOSURE": Mo is a real conversation feed — every
// submission becomes a turn (the user's own text, then Mo's reply, then an
// optional proposal card) that stays in view, oldest first, newest at the
// bottom. `conversation` (default false) opts a host screen into the full
// scrollable feed + session-scoped transcript restore; the compact Today entry
// point leaves it off and keeps its old single-card-above-the-input look,
// unaffected. Either way there is exactly ONE state machine and ONE set of
// handlers — only the rendering differs.
//
// Phase 07C (Figma 404:*/425:2) restyled every turn-card branch to MONOA's
// semantic token system and Figma's card language (eyebrow label + tag chip +
// field rows), while keeping the conversation-feed architecture: Figma's M2-M7
// frames are full-screen wizard steps with a back chevron, but Mo here is an
// established, tested multi-turn feed (turns accumulate and scroll). Replacing
// the feed with per-state full-screen navigation would drop turn history and
// change interaction architecture, not just presentation — so Figma's visual
// language is applied at the card level instead, inside the existing feed.

import React, { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Send, X, Mic, Square } from "lucide-react";
import { apiFetch, ApiError, describeFailure } from "../lib/api";
import { newClientToken } from "../lib/clientToken";
import { useVoiceInput } from "./useVoiceInput";
import { formatElapsed } from "../lib/voiceRecording";
import { MoMark } from "./NavIcons";
import {
  isBusyView,
  noteOf,
  viewFromUnderstand,
  viewFromPrepare,
  viewFromConfirm,
  viewFromCancel,
  viewFromPending,
  proposalLines,
  proposalTag,
  proposalActionLabel,
  proposalDeclineLabel,
  resultTag,
  GENERIC_STALE_MESSAGE,
  type MoView,
  type MoTurn,
} from "../lib/moProposal";
import type {
  MoUnderstandResponse,
  MoPrepareResponse,
  MoConfirmResponse,
  MoCancelResponse,
  MoPendingResponse,
  MoSuggestion,
  MoMutatingSuggestion,
} from "../lib/types";

const DISPLAY_FONT = 'var(--font-bricolage), "Bricolage Grotesque", sans-serif';
const BODY_FONT = 'var(--font-hanken), "Hanken Grotesk", sans-serif';

const DEFAULT_STARTER_PROMPTS = ["What do I have today?", "Spent RM12 on lunch", "Plan my day"];

// Session-scoped transcript only (Section 8) — never a server-side log, never
// long-term memory. Bounded to the last MAX_STORED_TURNS so it can't grow
// without limit, and cleared automatically when the tab/session ends.
const STORAGE_KEY = "mo_conversation_v1";
const MAX_STORED_TURNS = 20;

function proposalIdOf(v: MoView): string | null {
  return "proposal" in v ? v.proposal.proposal_id : null;
}

export default function OmniBar({
  onSuccess,
  autoFocus = false,
  placeholder = "Tell Mo what you spent or need to do…",
  className = "",
  showIdleIntro = false,
  starterPrompts = DEFAULT_STARTER_PROMPTS,
  conversation = false,
}: {
  /** Called after a confirmed execution so a host screen can refresh its data. */
  onSuccess?: () => void;
  autoFocus?: boolean;
  placeholder?: string;
  className?: string;
  /** Opt-in only (default false) so the compact Today entry point is
   * unaffected — the full greeting + starter chips are for the dedicated
   * /mo page only. */
  showIdleIntro?: boolean;
  starterPrompts?: string[];
  /** Opt-in only (default false). true = the full scrollable conversation
   * feed with a session-scoped transcript (the /mo page). false = the
   * original compact single-card behavior (the Today entry point). */
  conversation?: boolean;
}) {
  const [input, setInput] = useState("");
  const [turns, setTurns] = useState<MoTurn[]>([]);
  const busyRef = useRef(false); // synchronous re-entrancy guard (all actions)
  const tokenRef = useRef<{ text: string; token: string } | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const feedEndRef = useRef<HTMLDivElement | null>(null);
  const hydratedRef = useRef(false);

  const lastTurn = turns.length ? turns[turns.length - 1] : null;
  const busy = !!lastTurn && isBusyView(lastTurn.view);

  // Voice input (Batch 03) — ONLY an input method. A successful transcription
  // lands in the exact same `input` state typed text uses; Send still goes
  // through the unchanged handleSubmit/process-input path below.
  const onTranscribed = useCallback((text: string) => {
    setInput(text);
    inputRef.current?.focus();
  }, []);
  const voice = useVoiceInput(onTranscribed);
  const voiceBusy =
    voice.state === "requesting_permission" || voice.state === "recording" || voice.state === "transcribing";

  const updateTurn = useCallback((id: string, view: MoView) => {
    setTurns((prev) => prev.map((t) => (t.id === id ? { ...t, view } : t)));
  }, []);

  // Restore the session transcript (conversation mode only — the compact
  // Today entry point never persists or shows history, matching its old
  // behavior exactly).
  useEffect(() => {
    if (!conversation) {
      hydratedRef.current = true;
      return;
    }
    try {
      const raw = sessionStorage.getItem(STORAGE_KEY);
      if (raw) {
        const parsed = JSON.parse(raw) as MoTurn[];
        if (Array.isArray(parsed)) setTurns(parsed);
      }
    } catch {
      /* private mode / blocked storage — fine, just no history this load */
    }
    hydratedRef.current = true;
  }, [conversation]);

  useEffect(() => {
    if (!conversation || !hydratedRef.current) return;
    try {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(turns.slice(-MAX_STORED_TURNS)));
    } catch {
      /* best-effort only */
    }
  }, [turns, conversation]);

  useEffect(() => {
    if (!conversation) return;
    feedEndRef.current?.scrollIntoView({ block: "end" });
  }, [turns, conversation]);

  // Restore a server-owned pending / executing / just-executed proposal on
  // load — reconciled into the existing transcript by proposal id when
  // possible, appended as a standalone turn otherwise. NEVER resubmits the
  // original request; it only reflects server-owned state.
  //
  // Only the full conversation surface (/mo) restores an already-RESOLVED
  // result ("executed" / "stale") on load — that's what lets refreshing Mo
  // still show the truthful outcome. The compact Today entry point opts out
  // (allowResolved: false): Today already shows the real outcome via its own
  // data (the moved event, the new balance, etc.), so re-showing a finished
  // Mo card there — on every navigation back until the server's bounded
  // window closes — would just be a stale duplicate of something the user
  // already dismissed. A genuinely actionable proposal (pending / executing /
  // failed) still restores on Today exactly as before.
  useEffect(() => {
    let alive = true;
    void apiFetch<MoPendingResponse>("/api/process-input/pending", { kind: "crud" })
      .then((r) => {
        if (!alive) return;
        const v = viewFromPending(r, { allowResolved: conversation });
        if (!v) return;
        const pid = r.proposal?.proposal_id ?? null;
        setTurns((prev) => {
          const idx = pid ? prev.findIndex((t) => proposalIdOf(t.view) === pid) : -1;
          if (idx >= 0) {
            const next = prev.slice();
            next[idx] = { ...next[idx], view: v };
            return next;
          }
          return [...prev, { id: pid || newClientToken(), userText: "", view: v }];
        });
      })
      .catch((err) => {
        // offline / 401 — stay as-is. On a real 401 (signed out), the
        // transcript belongs to nobody any more; drop it rather than risk
        // showing it to whoever signs in next in the same tab.
        if (err instanceof ApiError && err.kind === "http" && err.status === 401) {
          try {
            sessionStorage.removeItem(STORAGE_KEY);
          } catch {
            /* ignore */
          }
          setTurns([]);
        }
      });
    return () => {
      alive = false;
    };
  }, [conversation]);

  const handleSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      const text = input.trim();
      if (!text || busy || busyRef.current) return;
      busyRef.current = true;
      // reuse the token only for an identical retry after an unknown outcome.
      const token =
        tokenRef.current && tokenRef.current.text === text
          ? tokenRef.current.token
          : newClientToken();
      const id = newClientToken();
      // Section 7 — the composer clears the moment Send is pressed, not when
      // the response arrives; the sent text renders immediately as the user's
      // own turn instead.
      setInput("");
      setTurns((prev) => [...prev, { id, userText: text, view: { name: "understanding" } }]);
      try {
        const r = await apiFetch<MoUnderstandResponse>("/api/process-input", {
          method: "POST",
          kind: "ai",
          body: JSON.stringify({ user_input: text, client_token: token }),
        });
        tokenRef.current = r.kind === "proposal_required" ? null : { text, token };
        updateTurn(id, viewFromUnderstand(r));
        if (r.kind === "executed") onSuccess?.();
      } catch (err) {
        if (err instanceof ApiError && err.kind === "http" && err.status === 409) {
          updateTurn(id, {
            name: "failed_understand",
            message: "That looks like a repeat of something already in progress. Refresh and try again.",
            retryable: false,
          });
        } else {
          tokenRef.current = { text, token }; // outcome unknown -> allow identical retry
          updateTurn(id, {
            name: "failed_understand",
            message: describeFailure(err, { verb: "reach Mo" }),
            retryable: true,
          });
        }
      } finally {
        busyRef.current = false;
      }
    },
    [input, busy, updateTurn, onSuccess],
  );

  // B8: a mutating suggestion was picked. Send ONLY the opaque token back; the
  // server re-derives the effect and returns a normal proposal to review. This
  // never mutates domain data — only Confirm does.
  const prepareSuggestion = useCallback(
    async (turnId: string, token: string) => {
      if (busyRef.current) return;
      busyRef.current = true;
      updateTurn(turnId, { name: "preparing_suggestion" });
      try {
        const r = await apiFetch<MoPrepareResponse>(
          `/api/process-input/suggested/${encodeURIComponent(token)}`,
          { method: "POST", kind: "crud" },
        );
        updateTurn(turnId, viewFromPrepare(r));
        tokenRef.current = null;
      } catch (err) {
        if (err instanceof ApiError && err.kind === "http" && (err.status === 409 || err.status === 404)) {
          updateTurn(turnId, { name: "stale", message: err.message });
        } else {
          updateTurn(turnId, {
            name: "failed_understand",
            message: describeFailure(err, { verb: "reach Mo" }),
            retryable: true,
          });
        }
      } finally {
        busyRef.current = false;
      }
    },
    [updateTurn],
  );

  const confirm = useCallback(
    async (turnId: string) => {
      const turn = turns.find((t) => t.id === turnId);
      if (!turn || (turn.view.name !== "proposal" && turn.view.name !== "failed_execute")) return;
      if (busyRef.current) return;
      busyRef.current = true;
      const p = turn.view.proposal;
      updateTurn(turnId, { name: "confirming", proposal: p });
      try {
        const r = await apiFetch<MoConfirmResponse>(
          `/api/process-input/${encodeURIComponent(p.proposal_id)}/confirm`,
          { method: "POST", kind: "crud" },
        );
        const next = viewFromConfirm(r);
        updateTurn(turnId, next);
        if (next.name === "executed") {
          tokenRef.current = null;
          onSuccess?.();
        }
      } catch (err) {
        if (err instanceof ApiError && err.kind === "http" && (err.status === 409 || err.status === 404)) {
          // expired / cancelled / gone — reflect the server, don't claim success
          updateTurn(turnId, { name: "failed_execute", proposal: p, message: err.message });
        } else {
          updateTurn(turnId, {
            name: "failed_execute",
            proposal: p,
            message: "I couldn’t confirm that just now. Try again in a moment.",
          });
        }
      } finally {
        busyRef.current = false;
      }
    },
    [turns, updateTurn, onSuccess],
  );

  const cancel = useCallback(
    async (turnId: string) => {
      const turn = turns.find((t) => t.id === turnId);
      if (!turn || (turn.view.name !== "proposal" && turn.view.name !== "failed_execute")) return;
      if (busyRef.current) return;
      busyRef.current = true;
      const p = turn.view.proposal;
      try {
        const r = await apiFetch<MoCancelResponse>(
          `/api/process-input/${encodeURIComponent(p.proposal_id)}/cancel`,
          { method: "POST", kind: "crud" },
        );
        updateTurn(turnId, viewFromCancel(r));
        tokenRef.current = null;
      } catch {
        // cancel is best-effort; drop the active state regardless so nothing lingers.
        updateTurn(turnId, { name: "cancelled", proposal: p });
        tokenRef.current = null;
      } finally {
        busyRef.current = false;
      }
    },
    [turns, updateTurn],
  );

  // Compact (Today) mode: return to a blank input, exactly like the old
  // single-card behavior — it only ever shows one turn, so clearing all of
  // them is equivalent to clearing "the" card. Conversation mode (/mo) keeps
  // its history but removes exactly the dismissed turn, leaving every other
  // turn (pending proposals included) exactly as it was — Dismiss is only
  // ever rendered on a terminal (stale/cancelled/executed) card, never on a
  // proposal awaiting confirmation, so this can't hide or cancel real work.
  const dismiss = useCallback(
    (turnId: string) => {
      if (conversation) {
        setTurns((prev) => prev.filter((t) => t.id !== turnId));
        return;
      }
      setTurns([]);
      tokenRef.current = null;
    },
    [conversation],
  );

  // Shared by starter-prompt chips AND a failed turn's "Try again" — both are
  // pure prefill + focus, never an auto-submit; the user still presses Send.
  const useStarter = useCallback((text: string) => {
    setInput(text);
    inputRef.current?.focus();
  }, []);

  const showIntro = showIdleIntro && turns.length === 0 && !input;

  const composer = (
    <div>
      {voice.errorMessage && (
        <p
          role="alert"
          className="mb-2 flex items-center justify-between gap-2 rounded-[14px] border border-warning/25 bg-warning/10 px-3 py-2 text-xs text-warning"
        >
          <span>{voice.errorMessage}</span>
          <button
            type="button"
            onClick={voice.dismissError}
            className="shrink-0 font-semibold text-warning transition-colors hover:text-primary"
          >
            Dismiss
          </button>
        </p>
      )}

      {voice.state === "recording" ? (
        <div className="flex items-center gap-3 rounded-[18px] border border-line bg-surface px-5 py-4 shadow-[0px_5px_16px_0px_rgba(0,0,0,0.06)]">
          <span className="relative flex h-3 w-3 shrink-0" aria-hidden="true">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-danger-solid opacity-75" />
            <span className="relative inline-flex h-3 w-3 rounded-full bg-danger-solid" />
          </span>
          <p role="status" className="flex-1 text-sm text-primary" style={{ fontFamily: BODY_FONT }}>
            Recording… {formatElapsed(voice.elapsedSeconds)} / {formatElapsed(voice.maxSeconds)}
          </p>
          <button
            type="button"
            onClick={voice.cancelRecording}
            aria-label="Cancel recording"
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[14px] border border-line bg-inset text-secondary transition-colors hover:bg-surface-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
          >
            <X size={16} />
          </button>
          <button
            type="button"
            onClick={voice.stopRecording}
            aria-label="Stop recording"
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[14px] bg-[#1b2130] text-[#fafaf5] transition-opacity hover:opacity-90 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
          >
            <Square size={16} />
          </button>
        </div>
      ) : voice.state === "transcribing" ? (
        <div className="flex items-center gap-3 rounded-[18px] border border-line bg-surface px-5 py-4 shadow-[0px_5px_16px_0px_rgba(0,0,0,0.06)]">
          <Spinner />
          <p role="status" className="text-sm text-secondary" style={{ fontFamily: BODY_FONT }}>
            Transcribing…
          </p>
        </div>
      ) : (
        <form onSubmit={handleSubmit} className="relative">
          <input
            ref={inputRef}
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={placeholder}
            disabled={busy || voiceBusy}
            autoFocus={autoFocus}
            aria-label="Message Mo"
            className="w-full rounded-[18px] border border-line bg-surface py-4 pl-12 pr-14 text-[16px] text-primary shadow-[0px_5px_16px_0px_rgba(0,0,0,0.06)] outline-none transition-colors focus:border-line-strong disabled:opacity-70"
            style={{ fontFamily: BODY_FONT }}
          />
          <button
            type="button"
            onClick={voice.startRecording}
            disabled={busy || voiceBusy}
            aria-label="Record a voice message"
            className="absolute left-2 top-2 bottom-2 flex items-center justify-center rounded-[14px] px-3 text-muted transition-colors hover:bg-inset hover:text-primary disabled:opacity-60"
          >
            {voiceBusy ? <Spinner /> : <Mic size={18} />}
          </button>
          <button
            type="submit"
            disabled={busy || !input.trim()}
            aria-label="Send to Mo"
            className="absolute right-2 top-2 bottom-2 flex items-center justify-center rounded-full bg-[#1b2130] px-3 text-[#fafaf5] transition-opacity hover:opacity-90 disabled:opacity-40"
          >
            {busy ? <Spinner tone="onDark" /> : <Send size={18} />}
          </button>
        </form>
      )}
    </div>
  );

  const intro = showIntro && (
    <div className="mb-4">
      <p className="text-sm text-muted" style={{ fontFamily: BODY_FONT }}>
        What are we sorting out?
      </p>
      <div className="mt-3 flex flex-wrap gap-2">
        {starterPrompts.slice(0, 3).map((p) => (
          <button
            key={p}
            type="button"
            onClick={() => useStarter(p)}
            className="inline-flex min-h-[36px] items-center rounded-full border border-line bg-surface px-3 text-xs font-medium text-secondary transition-colors hover:bg-surface-2 hover:text-primary focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
            style={{ fontFamily: BODY_FONT }}
          >
            {p}
          </button>
        ))}
      </div>
    </div>
  );

  if (!conversation) {
    // Compact mode — unchanged shape from before: the intro, at most the
    // latest turn's card, then the composer.
    return (
      <div className={className}>
        {intro}
        {lastTurn && (
          <MoTurnCard
            turn={lastTurn}
            onConfirm={confirm}
            onCancel={cancel}
            onDismiss={dismiss}
            onPrepare={prepareSuggestion}
            onRetry={useStarter}
            busy={busy}
          />
        )}
        {composer}
      </div>
    );
  }

  // Conversation mode — a real, scrollable feed: oldest turn first, newest at
  // the bottom, composer pinned below it (Section 6/17).
  return (
    <div className={`${className} flex min-h-0 flex-1 flex-col`}>
      <div className="min-h-0 flex-1 overflow-y-auto pb-2">
        {intro}
        {turns.map((t) => (
          <div key={t.id} className="moTurnIn mb-4">
            {t.userText && (
              <div className="mb-2 flex justify-end">
                <div
                  className="max-w-[85%] rounded-[18px] rounded-br-md bg-inset px-4 py-2.5 text-sm text-primary"
                  style={{ fontFamily: BODY_FONT }}
                >
                  {t.userText}
                </div>
              </div>
            )}
            <MoTurnCard
              turn={t}
              onConfirm={confirm}
              onCancel={cancel}
              onDismiss={dismiss}
              onPrepare={prepareSuggestion}
              onRetry={useStarter}
              busy={busy && t.id === lastTurn?.id}
            />
          </div>
        ))}
        <div ref={feedEndRef} />
      </div>
      <div className="pt-2">{composer}</div>
    </div>
  );
}

function Spinner({ tone = "default" }: { tone?: "default" | "onDark" }) {
  return (
    <span
      aria-hidden="true"
      className={
        "block h-5 w-5 animate-spin rounded-full border-2 motion-reduce:animate-[spin_1.6s_linear_infinite] " +
        (tone === "onDark" ? "border-white/25 border-t-white" : "border-line-strong border-t-primary")
      }
    />
  );
}

/** Eyebrow label — Figma's small uppercase section tag ("MO IS READING",
 * "MO UNDERSTOOD", "SUGGESTED ACTIONS", …). The copy passed in is always a
 * static, already-uppercase UI-chrome string, never user/model data. */
function Eyebrow({ children }: { children: React.ReactNode }) {
  return (
    <p className="mb-2 text-[10px] font-semibold tracking-wide text-faint" style={{ fontFamily: BODY_FONT }}>
      {children}
    </p>
  );
}

function Tag({ label, tone }: { label: string; tone: "new" | "change" | "remove" | "complete" | "multiple" }) {
  const cls =
    tone === "change"
      ? "bg-accent-lav text-primary"
      : tone === "remove"
        ? "bg-danger/10 text-danger"
        : tone === "complete"
          ? "bg-success/10 text-success"
          : tone === "multiple"
            ? "bg-inset text-secondary"
            : "bg-success/10 text-success"; // "new"
  return (
    <span
      className={`inline-flex h-[26px] items-center rounded-[13px] px-3 text-[11px] font-semibold ${cls}`}
      style={{ fontFamily: BODY_FONT }}
    >
      {label}
    </span>
  );
}

function Suggestions({ items }: { items: MoSuggestion[] }) {
  if (!items?.length) return null;
  return (
    <div className="mt-3 flex flex-wrap gap-2">
      {items.map((s) => (
        <Link
          key={s.key}
          href={s.route}
          className="inline-flex min-h-[44px] items-center rounded-[16px] border border-line bg-surface px-4 text-[13px] font-semibold text-primary transition-colors hover:bg-surface-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
          style={{ fontFamily: BODY_FONT }}
        >
          {s.label}
        </Link>
      ))}
    </div>
  );
}

// B8 — mutating suggestions. Plain buttons (never links); a click only PREPARES
// a proposal to review. Server already caps at 2; the client trusts that but
// renders no more than it is given. Styled as the PRIMARY (solid Ink) pill —
// Figma's asymmetry between a primary action and a secondary suggestion maps
// directly onto our real mutating-vs-navigation-only distinction.
function MutatingSuggestions({
  items,
  onPrepare,
  disabled,
}: {
  items: MoMutatingSuggestion[];
  onPrepare: (token: string) => void;
  disabled: boolean;
}) {
  if (!items?.length) return null;
  return (
    <div className="mt-3 flex flex-wrap gap-2">
      {items.map((s) => (
        <button
          key={s.key}
          type="button"
          disabled={disabled}
          onClick={() => onPrepare(s.token)}
          className="inline-flex min-h-[44px] items-center rounded-[16px] bg-[#1b2130] px-4 text-[13px] font-semibold text-[#fafaf5] transition-opacity hover:opacity-90 disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
          style={{ fontFamily: BODY_FONT }}
        >
          {s.label}
        </button>
      ))}
    </div>
  );
}

function Card({ children }: { children: React.ReactNode }) {
  return (
    <div className="mb-3 rounded-[22px] border border-line bg-surface p-[22px] text-sm shadow-[0px_5px_16px_0px_rgba(0,0,0,0.05)]">
      {children}
    </div>
  );
}

// One turn's assistant-side rendering — the ONLY place a MoView becomes UI.
// Visual hierarchy (Section 15): 1) Mo's concise reply, 2) an optional
// secondary note, 3) an optional structured proposal, 4) its controls. Never
// all mixed into one dense block.
function MoTurnCard({
  turn,
  onConfirm,
  onCancel,
  onDismiss,
  onPrepare,
  onRetry,
  busy,
}: {
  turn: MoTurn;
  onConfirm: (turnId: string) => void;
  onCancel: (turnId: string) => void;
  onDismiss: (turnId: string) => void;
  onPrepare: (turnId: string, token: string) => void;
  onRetry: (text: string) => void;
  busy: boolean;
}) {
  const view = turn.view;

  if (view.name === "idle") return null; // never actually constructed for a turn

  if (view.name === "understanding") {
    return (
      <Card>
        <Eyebrow>MO IS READING</Eyebrow>
        <div className="flex items-center gap-2.5">
          <MoMark className="h-7 w-7 shrink-0" />
          <p role="status" className="text-[13px] text-secondary" style={{ fontFamily: BODY_FONT }}>
            Thinking…
          </p>
        </div>
        <div className="mt-4 space-y-2.5" aria-hidden="true">
          <div className="h-3 w-[70%] rounded-full bg-inset" />
          <div className="h-3 w-[85%] rounded-full bg-inset" />
          <div className="h-3 w-[45%] rounded-full bg-inset" />
        </div>
      </Card>
    );
  }

  if (view.name === "answer" || view.name === "clarification" || view.name === "unsupported") {
    const eyebrow =
      view.name === "answer" ? "MO’S ANSWER" : view.name === "clarification" ? "NEEDS MORE INFO" : "CAN’T DO THAT";
    return (
      <Card>
        <Eyebrow>{eyebrow}</Eyebrow>
        <p
          role="status"
          className="whitespace-pre-wrap text-[15px] leading-relaxed text-primary"
          style={{ fontFamily: BODY_FONT }}
        >
          {view.message}
        </p>
        <MutatingSuggestions items={view.mutating} onPrepare={(tok) => onPrepare(turn.id, tok)} disabled={busy} />
        <Suggestions items={view.suggestions} />
      </Card>
    );
  }

  if (view.name === "preparing_suggestion") {
    return (
      <Card>
        <Eyebrow>MO IS READING</Eyebrow>
        <p role="status" className="flex items-center gap-2 text-secondary" style={{ fontFamily: BODY_FONT }}>
          <Spinner /> Getting the details…
        </p>
      </Card>
    );
  }

  if (view.name === "failed_understand") {
    return (
      <Card>
        <Eyebrow>WHAT HAPPENED</Eyebrow>
        <Tag label="COULDN’T COMPLETE" tone="remove" />
        <p role="alert" className="mt-2.5 text-[14px] text-primary" style={{ fontFamily: BODY_FONT }}>
          {view.message}
        </p>
        {view.retryable && (
          <button
            type="button"
            onClick={() => onRetry(turn.userText)}
            className="mt-3 inline-flex min-h-[44px] items-center rounded-[16px] bg-[#1b2130] px-4 text-[13px] font-semibold text-[#fafaf5] transition-opacity hover:opacity-90 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
            style={{ fontFamily: BODY_FONT }}
          >
            Try again
          </button>
        )}
      </Card>
    );
  }

  if (view.name === "stale") {
    // B8.1: a suggestion that no longer matches live data. Truthful copy, never
    // "You cancelled this" / "Completed" / "Updated", and no "Try again" (a
    // blind retry cannot succeed).
    return (
      <Card>
        <Eyebrow>WHAT HAPPENED</Eyebrow>
        <Tag label="NO LONGER CURRENT" tone="multiple" />
        <p role="status" className="mt-2.5 text-[14px] text-primary" style={{ fontFamily: BODY_FONT }}>
          {view.message || GENERIC_STALE_MESSAGE}
        </p>
        <button
          onClick={() => onDismiss(turn.id)}
          className="mt-3 inline-flex min-h-[44px] items-center text-[13px] font-semibold text-muted hover:text-primary focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
          style={{ fontFamily: BODY_FONT }}
        >
          Dismiss
        </button>
      </Card>
    );
  }

  if (view.name === "cancelled") {
    return (
      <Card>
        <Eyebrow>OKAY</Eyebrow>
        <p role="status" className="text-[14px] text-secondary" style={{ fontFamily: BODY_FONT }}>
          Nothing was changed.
        </p>
        <button
          onClick={() => onDismiss(turn.id)}
          className="mt-2 min-h-[36px] text-[12px] font-semibold text-muted hover:text-primary"
          style={{ fontFamily: BODY_FONT }}
        >
          Dismiss
        </button>
      </Card>
    );
  }

  if (view.name === "executed") {
    // Section 20 — success state (Figma M5 "Done"): a dark, unmistakably
    // final card replaces the proposal entirely — there is no active button
    // left to press. The nested field list reuses the SAME proposalLines()
    // rows the original proposal card showed, so "what happened" always
    // matches "what was proposed".
    const lines = proposalLines(view.proposal);
    return (
      <>
        <div className="mb-3 rounded-[22px] bg-[#1b2130] p-[22px]">
          <p className="text-[11px] font-semibold tracking-wide text-[#9c9ab4]" style={{ fontFamily: BODY_FONT }}>
            {resultTag(view.proposal)}
          </p>
          <p
            role="status"
            className="mt-2 text-[19px] font-extrabold leading-snug text-[#fafaf5]"
            style={{ fontFamily: DISPLAY_FONT, fontVariationSettings: '"opsz" 14, "wdth" 100' }}
          >
            {view.message}
          </p>
          {lines.length > 0 && (
            <div className="mt-3 space-y-1 rounded-[14px] bg-[#242540] px-4 py-3">
              {lines.map((l) => (
                <div key={l.label} className="flex items-baseline justify-between gap-3 text-[12px]">
                  <span className="text-[#9c9ab4]">{l.label}</span>
                  <span className="text-right font-semibold text-[#fafaf5]">{l.value}</span>
                </div>
              ))}
            </div>
          )}
        </div>
        <Suggestions items={view.suggestions} />
        <button
          onClick={() => onDismiss(turn.id)}
          className="mb-3 min-h-[36px] text-[12px] font-semibold text-muted hover:text-primary"
          style={{ fontFamily: BODY_FONT }}
        >
          Dismiss
        </button>
      </>
    );
  }

  // proposal / confirming / failed_execute -> the confirmation card. Figma
  // separates a NEW addition ("What I understood", M3) from a CHANGE to
  // something already saved ("Confirm this change", M4); proposalTag()
  // derives which one this is purely from the real action_type, and the
  // Confirm/Cancel labels follow the same safe derivation — never the raw
  // new value (e.g. never "Move to 5:30"), only a verb tied to the action.
  const p = view.proposal;
  const confirming = view.name === "confirming";
  const lines = proposalLines(p);
  const note = noteOf(view);
  const tag = proposalTag(p);
  const eyebrow = confirming ? "CONFIRMING" : view.name === "failed_execute" ? "COULDN’T SAVE" : "MO UNDERSTOOD";
  return (
    <Card>
      <Eyebrow>{eyebrow}</Eyebrow>
      <Tag label={tag.label} tone={tag.kind} />
      <p role="status" className="mt-2.5 text-[15px] font-semibold text-primary" style={{ fontFamily: BODY_FONT }}>
        {p.summary}
      </p>
      {note && (
        <p className="mt-1 text-[12px] text-muted" style={{ fontFamily: BODY_FONT }}>
          {note}
        </p>
      )}
      {lines.length > 0 && (
        <dl className="mt-3 divide-y divide-line">
          {lines.map((l) => (
            <div key={l.label} className="flex items-center justify-between gap-3 py-2 first:pt-0 last:pb-0">
              <dt className="text-[14px] font-medium text-primary" style={{ fontFamily: BODY_FONT }}>
                {l.label}
              </dt>
              <dd className="text-right text-[14px] font-semibold text-secondary" style={{ fontFamily: BODY_FONT }}>
                {l.value}
              </dd>
            </div>
          ))}
        </dl>
      )}
      {view.name === "failed_execute" && (
        <p role="alert" className="mt-2 text-[13px] text-danger" style={{ fontFamily: BODY_FONT }}>
          {view.message}
        </p>
      )}
      <div className="mt-4 flex gap-2">
        <button
          onClick={() => onConfirm(turn.id)}
          disabled={confirming}
          className="flex min-h-[48px] flex-1 items-center justify-center gap-2 rounded-[16px] bg-[#1b2130] px-4 text-[14px] font-semibold text-[#fafaf5] transition-opacity hover:opacity-90 disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
          style={{ fontFamily: BODY_FONT }}
        >
          {confirming ? <Spinner tone="onDark" /> : view.name === "failed_execute" ? "Try again" : proposalActionLabel(p)}
        </button>
        <button
          onClick={() => onCancel(turn.id)}
          disabled={confirming}
          className="flex min-h-[48px] items-center justify-center gap-2 rounded-[16px] border border-line bg-surface px-4 text-[14px] font-semibold text-primary transition-colors hover:bg-surface-2 disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
          style={{ fontFamily: BODY_FONT }}
        >
          {proposalDeclineLabel(p)}
        </button>
      </div>
      <p className="mt-3 text-center text-[11px] text-faint" style={{ fontFamily: BODY_FONT }}>
        Nothing changes until you confirm.
      </p>
    </Card>
  );
}
