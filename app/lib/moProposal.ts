// Mo human-in-the-loop — the OmniBar state machine (B2).
//
// PURE: no React, no fetch, no DOM. `node --test`-friendly. The component
// (app/components/OmniBar.tsx) owns the effects; this owns the transitions and
// the safe formatting of a proposal card.

import type {
  MoUnderstandResponse,
  MoPrepareResponse,
  MoConfirmResponse,
  MoProposal,
  MoSuggestion,
  MoMutatingSuggestion,
  MoPendingResponse,
} from "./types";

export type MoView =
  | { name: "idle" }
  | { name: "understanding" }
  | {
      name: "answer";
      message: string;
      suggestions: MoSuggestion[];
      mutating: MoMutatingSuggestion[];
    }
  | {
      name: "clarification";
      message: string;
      suggestions: MoSuggestion[];
      mutating: MoMutatingSuggestion[];
    }
  | {
      name: "unsupported";
      message: string;
      suggestions: MoSuggestion[];
      mutating: MoMutatingSuggestion[];
    }
  | { name: "failed_understand"; message: string; retryable: boolean }
  // B8: a mutating suggestion was selected; building the proposal server-side.
  | { name: "preparing_suggestion" }
  | {
      name: "proposal";
      proposal: MoProposal;
      message: string;
      suggestions: MoSuggestion[];
    }
  | { name: "confirming"; proposal: MoProposal }
  | {
      name: "executed";
      proposal: MoProposal;
      message: string;
      suggestions: MoSuggestion[];
    }
  | { name: "cancelled"; proposal: MoProposal }
  // B8: the domain changed under a suggestion before Confirm — nothing mutated.
  | { name: "stale"; message: string }
  | { name: "failed_execute"; proposal: MoProposal; message: string };

export const IDLE: MoView = { name: "idle" };
export const UNDERSTANDING: MoView = { name: "understanding" };

/**
 * One turn of the Mo conversation feed: the user's own message (empty for a
 * turn restored purely from server-owned pending state, which carries no
 * user text) plus the assistant-side view that resolved from it. Only the
 * LAST turn may ever be in a busy state ("understanding" / "confirming" /
 * "preparing_suggestion") — Mo processes one thing at a time.
 */
export interface MoTurn {
  id: string;
  userText: string;
  view: MoView;
}

export function isBusyView(v: MoView): boolean {
  return v.name === "understanding" || v.name === "confirming" || v.name === "preparing_suggestion";
}

/** The small secondary note (Section 5) attached to a proposal's display,
 * e.g. "I couldn't find a dentist appointment, so I'll leave that alone." —
 * kept OUT of the primary summary sentence so the UI can render it as a
 * lower-emphasis line rather than mixing it into Mo's main reply. */
export function noteOf(v: MoView): string | null {
  const p =
    v.name === "proposal" || v.name === "confirming" || v.name === "executed" || v.name === "failed_execute"
      ? v.proposal
      : null;
  if (!p) return null;
  const n = (p.display as Record<string, unknown>)?.note;
  return typeof n === "string" && n.trim() ? n : null;
}

/** Map a POST /api/process-input response to the next view. Most kinds are
 * a normal "understand" outcome; "executed"/"failed"/"stale"/"cancelled" are
 * only ever returned when the submitted text was a typed yes/no fallback for
 * the user's one active pending proposal (Section 9/10) — those reuse the
 * SAME view-building logic as a real Confirm/Cancel, never a parallel copy. */
export function viewFromUnderstand(r: MoUnderstandResponse): MoView {
  switch (r.kind) {
    case "answer":
      return {
        name: "answer",
        message: r.message,
        suggestions: r.suggested_actions,
        mutating: cappedMutating(r.mutating_suggestions),
      };
    case "clarification_required":
      return {
        name: "clarification",
        message: r.message,
        suggestions: r.suggested_actions,
        mutating: cappedMutating(r.mutating_suggestions),
      };
    case "unsupported":
      return {
        name: "unsupported",
        message: r.message,
        suggestions: r.suggested_actions,
        mutating: cappedMutating(r.mutating_suggestions),
      };
    case "error":
      return { name: "failed_understand", message: r.message, retryable: r.retryable !== false };
    case "proposal_required":
      return proposalView(r.proposal, r.message, r.suggested_actions);
    case "executed":
    case "failed":
    case "stale":
      return viewFromConfirm(r);
    case "cancelled":
      return viewFromCancel(r);
  }
}

/** Map a POST /api/process-input/suggested/{token} response to the next view. */
export function viewFromPrepare(r: MoPrepareResponse): MoView {
  return proposalView(r.proposal, r.message, r.suggested_actions);
}

// A mutating suggestion is only ever allow-listed server-side; the client still
// hard-caps at 2 so a bad response can never build a pill wall.
const MAX_MUTATING = 2;
function cappedMutating(items: MoMutatingSuggestion[] | undefined): MoMutatingSuggestion[] {
  return Array.isArray(items) ? items.slice(0, MAX_MUTATING) : [];
}

/** Map a POST /api/process-input/{id}/confirm response to the next view. */
export function viewFromConfirm(r: MoConfirmResponse): MoView {
  if (r.kind === "executed") {
    return { name: "executed", proposal: r.proposal, message: r.message, suggestions: r.suggested_actions };
  }
  if (r.kind === "stale") {
    return { name: "stale", message: r.message };
  }
  return { name: "failed_execute", proposal: r.proposal, message: r.message };
}

/** Map a POST /api/process-input/{id}/cancel response to the next view. */
export function viewFromCancel(r: { proposal: MoProposal }): MoView {
  return { name: "cancelled", proposal: r.proposal };
}

/**
 * Restore a view from GET /api/process-input/pending on Today / Mo load.
 * Returns null when there is nothing to show (stay idle). NEVER resubmits the
 * original request — it only reflects server-owned state.
 *
 * The server keeps an already-resolved proposal ("executed" / "stale")
 * answerable for a bounded window after confirm purely so a REFRESH OF THE
 * SAME Mo conversation still shows its truthful outcome. That window is not
 * a "this is still pending review" signal — Today's compact entry point
 * mounts this same restore path on every visit, so without a filter it would
 * re-show a result the user already saw and dismissed, on every navigation
 * back to Today until the window closes. `opts.allowResolved` (default true)
 * lets a host screen opt OUT of restoring already-resolved statuses; genuinely
 * actionable ones (pending / executing / failed) always restore regardless,
 * since those are real work still waiting on the user.
 */
export function viewFromPending(
  r: MoPendingResponse,
  opts?: { allowResolved?: boolean },
): MoView | null {
  const p = r.proposal;
  if (!p) return null;
  if (p.status === "pending") return proposalView(p, p.summary, []);
  if (p.status === "executing") return { name: "confirming", proposal: p };
  if (p.status === "failed") return { name: "failed_execute", proposal: p, message: p.failure_reason || "That didn’t go through. Try Confirm again." };
  if (opts?.allowResolved === false) return null;
  if (p.status === "executed") {
    return { name: "executed", proposal: p, message: p.exec_message || `Done — ${lowerFirst(p.effect)}`, suggestions: [] };
  }
  // B8.1: a recently-stale suggestion restores the TRUTHFUL acknowledgement
  // (never "Cancelled"), and the server stops surfacing it once its bounded
  // window closes.
  if (p.status === "stale") {
    return { name: "stale", message: p.failure_reason || GENERIC_STALE_MESSAGE };
  }
  // cancelled / expired -> nothing to restore
  return null;
}

export const GENERIC_STALE_MESSAGE =
  "This suggestion is no longer current. Nothing was changed.";

function proposalView(proposal: MoProposal, message: string, suggestions: MoSuggestion[]): MoView {
  return { name: "proposal", proposal, message, suggestions };
}

export function isAwaitingConfirmation(v: MoView): v is Extract<MoView, { name: "proposal" }> {
  return v.name === "proposal";
}

export function proposalId(v: MoView): string | null {
  if (v.name === "proposal" || v.name === "confirming" || v.name === "failed_execute") {
    return v.proposal.proposal_id;
  }
  return null;
}

function lowerFirst(s: string): string {
  return s ? s[0].toLowerCase() + s.slice(1) : s;
}

// --- safe display helpers for the proposal card (never invent values) ------

export interface ProposalLine {
  label: string;
  value: string;
}

export function proposalLines(p: MoProposal): ProposalLine[] {
  const d = p.display as Record<string, string | boolean | null | undefined>;
  const rows: ProposalLine[] = [];
  const push = (label: string, value: unknown) => {
    if (value === null || value === undefined || value === "" || value === false) return;
    rows.push({ label, value: String(value) });
  };
  if (p.action_type === "create_transaction") {
    push("Amount", d.amount);
    push("Type", d.type);
    push("Category", d.category);
    push("Note", d.description);
    push("Date", d.date);
  } else if (p.action_type === "create_task") {
    push(d.kind === "reminder" ? "Reminder" : "Task", d.title);
    push("Remind at", d.remind_at);
    push("Due", d.due);
  } else if (p.action_type === "create_event") {
    push("Event", d.title);
    push("Date", d.date);
    push(d.all_day ? "" : "Time", d.time);
    push("Where", d.location);
  } else if (p.action_type === "schedule_task_block") {
    push("Task", d.title);
    push("Date", d.date);
    push("Time", d.time);
    push("Length", d.duration);
    if (d.deadline) rows.push({ label: "Deadline", value: `${String(d.deadline)} (unchanged)` });
  } else if (p.action_type === "complete_task") {
    push("Task", d.title);
  } else if (p.action_type === "update_task") {
    push("Task", d.title);
    push("New title", d.new_title);
    push("New due", d.new_due);
    push("New remind at", d.new_remind_at);
    push("New planned time", d.new_planned_at);
  } else if (p.action_type === "delete_task") {
    push("Task", d.title);
  } else if (p.action_type === "update_event") {
    push("Event", d.title);
    push("New title", d.new_title);
    push("New date", d.new_date);
    push("New time", d.new_time);
    push("New location", d.new_location);
  } else if (p.action_type === "delete_event") {
    push("Event", d.title);
  } else if (p.action_type === "batch_operations") {
    const items =
      (p.display.items as Array<{ op: string; title: string; when: string; old_when?: string | null }>) || [];
    for (const it of items) {
      const verb = it.op === "add" ? "ADD" : it.op === "change" ? "CHANGE" : "DELETE";
      const value = it.old_when ? `${it.old_when} → ${it.when}` : it.when;
      rows.push({ label: `${verb} · ${it.title}`, value: value || "" });
    }
  }
  return rows;
}

// --- Mo visual integration (Figma 404:*/425:2) — safe, derived-only display
// labels. Each is a pure function of the proposal's real `action_type` enum
// (never free text, never fabricated) so the UI can visually distinguish a
// NEW addition from a CHANGE to something already saved from a REMOVAL,
// matching Figma's separate "What I understood" (M3) vs "Confirm this
// change" (M4) treatments, without inventing any data the backend didn't
// send. A generic fallback keeps this forward-compatible with any future
// action_type the frontend doesn't recognise yet.

export type ProposalTagKind = "new" | "change" | "remove" | "complete" | "multiple";

export function proposalTag(p: MoProposal): { label: string; kind: ProposalTagKind } {
  switch (p.action_type) {
    case "create_event":
    case "create_task":
    case "schedule_task_block":
    case "create_transaction":
      return { label: "NEW", kind: "new" };
    case "update_event":
    case "update_task":
      return { label: "CHANGE", kind: "change" };
    case "delete_event":
    case "delete_task":
      return { label: "REMOVE", kind: "remove" };
    case "complete_task":
      return { label: "DONE", kind: "complete" };
    case "batch_operations":
      return { label: "MULTIPLE", kind: "multiple" };
    default:
      return { label: "PROPOSED", kind: "new" };
  }
}

/** The primary (Confirm-equivalent) button label — never the raw new value
 * (e.g. never "Move to 5:30"), only a verb tied to the real action_type, so
 * it stays correct regardless of title/time length or formatting. */
export function proposalActionLabel(p: MoProposal): string {
  switch (p.action_type) {
    case "create_event":
    case "create_task":
    case "schedule_task_block":
      return "Add to Plan";
    case "create_transaction":
      return "Log it";
    case "update_event":
    case "update_task":
      return "Save change";
    case "complete_task":
      return "Mark done";
    case "delete_event":
    case "delete_task":
      return "Delete";
    case "batch_operations":
      return "Confirm all";
    default:
      return "Confirm";
  }
}

/** The secondary (Cancel-equivalent) button label. */
export function proposalDeclineLabel(p: MoProposal): string {
  return p.action_type === "delete_event" || p.action_type === "delete_task" ? "Keep it" : "Cancel";
}

/** The completion card's eyebrow ("ADDED" / "UPDATED" / …), Figma M5's
 * "ADDED" generalised across every action_type via the same real enum. */
export function resultTag(p: MoProposal): string {
  switch (p.action_type) {
    case "create_event":
    case "create_task":
    case "schedule_task_block":
    case "create_transaction":
      return "ADDED";
    case "update_event":
    case "update_task":
      return "UPDATED";
    case "delete_event":
    case "delete_task":
      return "REMOVED";
    case "complete_task":
      return "COMPLETED";
    case "batch_operations":
      return "DONE";
    default:
      return "DONE";
  }
}
