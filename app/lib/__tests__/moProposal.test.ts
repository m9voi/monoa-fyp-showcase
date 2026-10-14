import test from "node:test";
import assert from "node:assert/strict";
import {
  viewFromUnderstand,
  viewFromPrepare,
  viewFromConfirm,
  viewFromCancel,
  viewFromPending,
  proposalLines,
  proposalId,
  isBusyView,
  noteOf,
  proposalTag,
  proposalActionLabel,
  proposalDeclineLabel,
  resultTag,
} from "../moProposal.ts";

const TX_PROPOSAL = {
  proposal_id: "prop_1",
  status: "pending" as const,
  action_type: "create_transaction" as const,
  summary: "Got it — here’s the expense to log.",
  effect: "Add RM 20.00 expense in Personal Care, dated 5 Sep.",
  display: { amount: "RM 20.00", type: "expense", category: "Personal Care", description: "shaving", date: "5 Sep" },
  expires_at: "2026-09-05T10:00:00+08:00",
};

test("answer -> answer view with its suggestions", () => {
  const v = viewFromUnderstand({ kind: "answer", message: "You spent RM 15.00.", suggested_actions: [{ key: "open_wallet", label: "Open Wallet", route: "/finance" }] });
  assert.equal(v.name, "answer");
  assert.equal(v.name === "answer" && v.message, "You spent RM 15.00.");
  assert.equal(v.name === "answer" && v.suggestions[0].route, "/finance");
});

test("clarification / unsupported / error map to their own views", () => {
  assert.equal(viewFromUnderstand({ kind: "clarification_required", message: "What for?", suggested_actions: [] }).name, "clarification");
  assert.equal(viewFromUnderstand({ kind: "unsupported", message: "Can’t do that.", suggested_actions: [] }).name, "unsupported");
  const e = viewFromUnderstand({ kind: "error", message: "Try again.", suggested_actions: [] });
  assert.equal(e.name, "failed_understand");
  assert.equal(e.name === "failed_understand" && e.retryable, true);
});

test("proposal_required -> proposal view carrying the proposal", () => {
  const v = viewFromUnderstand({ kind: "proposal_required", proposal: TX_PROPOSAL, message: TX_PROPOSAL.summary, suggested_actions: [] });
  assert.equal(v.name, "proposal");
  assert.equal(proposalId(v), "prop_1");
});

test("confirm executed / failed map to executed / failed_execute", () => {
  assert.equal(viewFromConfirm({ kind: "executed", proposal: { ...TX_PROPOSAL, status: "executed" }, message: "Logged RM 20.00.", suggested_actions: [] }).name, "executed");
  assert.equal(viewFromConfirm({ kind: "failed", proposal: TX_PROPOSAL, message: "Couldn’t save.", retryable: true, suggested_actions: [] }).name, "failed_execute");
});

test("cancel -> cancelled view", () => {
  assert.equal(viewFromCancel({ proposal: { ...TX_PROPOSAL, status: "cancelled" } }).name, "cancelled");
});

test("viewFromPending restores each server status, and null for nothing/cancelled/expired", () => {
  assert.equal(viewFromPending({ proposal: null }), null);
  assert.equal(viewFromPending({ proposal: { ...TX_PROPOSAL, status: "pending" } })?.name, "proposal");
  assert.equal(viewFromPending({ proposal: { ...TX_PROPOSAL, status: "executing" } })?.name, "confirming");
  assert.equal(viewFromPending({ proposal: { ...TX_PROPOSAL, status: "failed" } })?.name, "failed_execute");
  const ex = viewFromPending({ proposal: { ...TX_PROPOSAL, status: "executed", exec_message: "Logged RM 20.00 for shaving." } });
  assert.equal(ex?.name, "executed");
  assert.equal(ex?.name === "executed" && ex.message, "Logged RM 20.00 for shaving.");
  assert.equal(viewFromPending({ proposal: { ...TX_PROPOSAL, status: "cancelled" } }), null);
  assert.equal(viewFromPending({ proposal: { ...TX_PROPOSAL, status: "expired" } }), null);
});

test("viewFromPending({ allowResolved: false }) restores actionable statuses but never a stale/executed leftover — the Today-vs-Mo split behind the completed-card-leaking-into-Today fix", () => {
  const opts = { allowResolved: false };
  assert.equal(viewFromPending({ proposal: { ...TX_PROPOSAL, status: "pending" } }, opts)?.name, "proposal");
  assert.equal(viewFromPending({ proposal: { ...TX_PROPOSAL, status: "executing" } }, opts)?.name, "confirming");
  assert.equal(viewFromPending({ proposal: { ...TX_PROPOSAL, status: "failed" } }, opts)?.name, "failed_execute");
  assert.equal(viewFromPending({ proposal: { ...TX_PROPOSAL, status: "executed" } }, opts), null);
  assert.equal(viewFromPending({ proposal: { ...TX_PROPOSAL, status: "stale" } }, opts), null);
});

test("viewFromPending never resubmits — it only reads status (no request field consumed)", () => {
  // the function signature only accepts { proposal }; there is no user_input to echo
  const v = viewFromPending({ proposal: { ...TX_PROPOSAL, status: "pending" } });
  assert.ok(v && v.name === "proposal");
  assert.ok(!("user_input" in (v as object)));
});

test("proposalLines shows only present, non-empty fields — never a fabricated value", () => {
  const lines = proposalLines(TX_PROPOSAL);
  assert.deepEqual(lines.map((l) => l.label), ["Amount", "Type", "Category", "Note", "Date"]);
  assert.equal(lines[0].value, "RM 20.00");

  const sparse = proposalLines({
    ...TX_PROPOSAL,
    action_type: "create_task",
    display: { kind: "task", title: "Email Dr Tan", due: null, remind_at: "" },
  });
  assert.deepEqual(sparse.map((l) => l.label), ["Task"]);
  assert.equal(sparse[0].value, "Email Dr Tan");
});

test("proposalLines: all-day event omits the Time row", () => {
  const lines = proposalLines({
    ...TX_PROPOSAL,
    action_type: "create_event",
    display: { kind: "event", title: "Trip", date: "6 Sep", time: null, all_day: true, location: null },
  });
  assert.deepEqual(lines.map((l) => l.label), ["Event", "Date"]);
});

// --- B8: Mo mutating suggested actions ------------------------------------- #

test("B8: answer view carries allow-listed mutating suggestions, capped at 2", () => {
  const mk = (n: number) =>
    Array.from({ length: n }, (_, i) => ({
      key: `mut:complete_task:t${i}`,
      label: `Mark Task ${i} complete`,
      kind: "complete_task" as const,
      token: `tok${i}`,
    }));
  const v = viewFromUnderstand({
    kind: "answer",
    message: "Today: 1 event; 2 open tasks.",
    suggested_actions: [{ key: "open_plan", label: "Open Plan", route: "/schedule" }],
    mutating_suggestions: mk(4),
  });
  assert.equal(v.name, "answer");
  assert.equal(v.name === "answer" && v.mutating.length, 2);
  assert.equal(v.name === "answer" && v.mutating[0].token, "tok0");
  assert.equal(v.name === "answer" && v.suggestions[0].route, "/schedule");
});

test("B8: an answer with no mutating suggestions has an empty list (never undefined)", () => {
  const v = viewFromUnderstand({ kind: "answer", message: "Your day is clear.", suggested_actions: [] });
  assert.equal(v.name === "answer" && Array.isArray(v.mutating) && v.mutating.length, 0);
});

test("B8: viewFromPrepare turns the suggested-action response into a proposal to review", () => {
  const proposal = {
    ...TX_PROPOSAL,
    proposal_id: "prop_sg",
    action_type: "schedule_task_block" as const,
    summary: "Here’s a time to block for this task.",
    effect: "Set aside 45 min to work on “Finish Chapter 5”…",
    display: {
      kind: "schedule_task_block",
      title: "Finish Chapter 5",
      date: "12 Sep",
      time: "3:00 pm – 3:45 pm",
      duration: "45 min",
      deadline: "15 Sep",
      deadline_unchanged: true,
    },
  };
  const v = viewFromPrepare({ kind: "proposal_required", proposal, message: proposal.summary, suggested_actions: [] });
  assert.equal(v.name, "proposal");
  assert.equal(proposalId(v), "prop_sg");
});

test("B8: proposalLines for schedule_task_block marks the deadline explicitly unchanged", () => {
  const lines = proposalLines({
    ...TX_PROPOSAL,
    action_type: "schedule_task_block",
    display: {
      kind: "schedule_task_block",
      title: "Finish Chapter 5",
      date: "12 Sep",
      time: "3:00 pm – 3:45 pm",
      duration: "45 min",
      deadline: "15 Sep",
      deadline_unchanged: true,
    },
  });
  assert.deepEqual(lines.map((l) => l.label), ["Task", "Date", "Time", "Length", "Deadline"]);
  assert.equal(lines.find((l) => l.label === "Deadline")?.value, "15 Sep (unchanged)");
});

test("B8: proposalLines for complete_task shows only the task title", () => {
  const lines = proposalLines({
    ...TX_PROPOSAL,
    action_type: "complete_task",
    display: { kind: "complete_task", title: "Submit draft" },
  });
  assert.deepEqual(lines.map((l) => l.label), ["Task"]);
  assert.equal(lines[0].value, "Submit draft");
});

test("B8: confirm 'stale' maps to a stale view with no proposal card", () => {
  const v = viewFromConfirm({
    kind: "stale",
    proposal: { ...TX_PROPOSAL, status: "stale" },
    message: "This suggestion is no longer current because the task changed. Nothing was changed.",
    suggested_actions: [],
  });
  assert.equal(v.name, "stale");
  assert.equal(
    v.name === "stale" && v.message,
    "This suggestion is no longer current because the task changed. Nothing was changed.",
  );
});

test("B8.1: viewFromPending restores a recently-stale proposal as the truthful ack, never Cancelled", () => {
  const staleProp = {
    ...TX_PROPOSAL,
    status: "stale" as const,
    failure_reason: "This suggestion is no longer current — that task no longer exists. Nothing was changed.",
  };
  const v = viewFromPending({ proposal: staleProp });
  assert.equal(v?.name, "stale");
  assert.equal(
    v?.name === "stale" && v.message,
    "This suggestion is no longer current — that task no longer exists. Nothing was changed.",
  );
  // it is NOT restored as an actionable proposal / confirming / cancelled
  assert.notEqual(v?.name, "proposal");
  assert.notEqual(v?.name, "cancelled");
});

test("B8.1: a stale proposal with no stored reason falls back to the generic truthful copy", () => {
  const v = viewFromPending({ proposal: { ...TX_PROPOSAL, status: "stale", failure_reason: null } });
  assert.equal(v?.name, "stale");
  assert.equal(v?.name === "stale" && /no longer current/i.test(v.message) && /nothing was changed/i.test(v.message), true);
});

// --- Mo interaction/action UX closure ---------------------------------------- #

test("typed yes/no fallback: /api/process-input 'executed'/'failed'/'stale'/'cancelled' route through the SAME view logic as a real Confirm/Cancel", () => {
  const executed = viewFromUnderstand({ kind: "executed", proposal: { ...TX_PROPOSAL, status: "executed" }, message: "Logged RM 20.00.", suggested_actions: [] });
  assert.equal(executed.name, "executed");
  assert.equal(executed.name === "executed" && executed.message, "Logged RM 20.00.");

  const failed = viewFromUnderstand({ kind: "failed", proposal: TX_PROPOSAL, message: "Couldn’t save.", retryable: true, suggested_actions: [] });
  assert.equal(failed.name, "failed_execute");

  const stale = viewFromUnderstand({ kind: "stale", proposal: { ...TX_PROPOSAL, status: "stale" }, message: "No longer current. Nothing was changed.", suggested_actions: [] });
  assert.equal(stale.name, "stale");

  const cancelled = viewFromUnderstand({ kind: "cancelled", proposal: { ...TX_PROPOSAL, status: "cancelled" } });
  assert.equal(cancelled.name, "cancelled");
});

test("isBusyView is true only for understanding/confirming/preparing_suggestion", () => {
  assert.equal(isBusyView({ name: "understanding" }), true);
  assert.equal(isBusyView({ name: "confirming", proposal: TX_PROPOSAL }), true);
  assert.equal(isBusyView({ name: "preparing_suggestion" }), true);
  assert.equal(isBusyView({ name: "idle" }), false);
  assert.equal(isBusyView({ name: "answer", message: "x", suggestions: [], mutating: [] }), false);
  assert.equal(isBusyView({ name: "proposal", proposal: TX_PROPOSAL, message: "x", suggestions: [] }), false);
});

test("noteOf reads the proposal's secondary note (Section 5) and stays null when there is none", () => {
  const withNote = {
    ...TX_PROPOSAL,
    display: { ...TX_PROPOSAL.display, note: "I couldn’t find a dentist appointment, so I’ll leave that alone." },
  };
  const v = viewFromUnderstand({ kind: "proposal_required", proposal: withNote, message: withNote.summary, suggested_actions: [] });
  assert.equal(noteOf(v), "I couldn’t find a dentist appointment, so I’ll leave that alone.");

  const withoutNote = viewFromUnderstand({ kind: "proposal_required", proposal: TX_PROPOSAL, message: TX_PROPOSAL.summary, suggested_actions: [] });
  assert.equal(noteOf(withoutNote), null);

  // never surfaced for a non-proposal view
  assert.equal(noteOf({ name: "answer", message: "hi", suggestions: [], mutating: [] }), null);
});

// --- Mo visual integration (Phase 07C) — derived tag/label helpers ---------- #

test("proposalTag distinguishes NEW / CHANGE / REMOVE / DONE / MULTIPLE by action_type only", () => {
  assert.deepEqual(proposalTag({ ...TX_PROPOSAL, action_type: "create_event" }), { label: "NEW", kind: "new" });
  assert.deepEqual(proposalTag({ ...TX_PROPOSAL, action_type: "create_task" }), { label: "NEW", kind: "new" });
  assert.deepEqual(proposalTag({ ...TX_PROPOSAL, action_type: "schedule_task_block" }), { label: "NEW", kind: "new" });
  assert.deepEqual(proposalTag({ ...TX_PROPOSAL, action_type: "create_transaction" }), { label: "NEW", kind: "new" });
  assert.deepEqual(proposalTag({ ...TX_PROPOSAL, action_type: "update_event" }), { label: "CHANGE", kind: "change" });
  assert.deepEqual(proposalTag({ ...TX_PROPOSAL, action_type: "update_task" }), { label: "CHANGE", kind: "change" });
  assert.deepEqual(proposalTag({ ...TX_PROPOSAL, action_type: "delete_event" }), { label: "REMOVE", kind: "remove" });
  assert.deepEqual(proposalTag({ ...TX_PROPOSAL, action_type: "delete_task" }), { label: "REMOVE", kind: "remove" });
  assert.deepEqual(proposalTag({ ...TX_PROPOSAL, action_type: "complete_task" }), { label: "DONE", kind: "complete" });
  assert.deepEqual(proposalTag({ ...TX_PROPOSAL, action_type: "batch_operations" }), { label: "MULTIPLE", kind: "multiple" });
});

test("proposalActionLabel is a verb tied to action_type, never the raw new value", () => {
  assert.equal(proposalActionLabel({ ...TX_PROPOSAL, action_type: "create_event" }), "Add to Plan");
  assert.equal(proposalActionLabel({ ...TX_PROPOSAL, action_type: "create_transaction" }), "Log it");
  assert.equal(proposalActionLabel({ ...TX_PROPOSAL, action_type: "update_event" }), "Save change");
  assert.equal(proposalActionLabel({ ...TX_PROPOSAL, action_type: "complete_task" }), "Mark done");
  assert.equal(proposalActionLabel({ ...TX_PROPOSAL, action_type: "delete_task" }), "Delete");
  assert.equal(proposalActionLabel({ ...TX_PROPOSAL, action_type: "batch_operations" }), "Confirm all");
});

test("proposalDeclineLabel is 'Keep it' only for a delete, 'Cancel' otherwise", () => {
  assert.equal(proposalDeclineLabel({ ...TX_PROPOSAL, action_type: "delete_event" }), "Keep it");
  assert.equal(proposalDeclineLabel({ ...TX_PROPOSAL, action_type: "delete_task" }), "Keep it");
  assert.equal(proposalDeclineLabel({ ...TX_PROPOSAL, action_type: "create_event" }), "Cancel");
  assert.equal(proposalDeclineLabel({ ...TX_PROPOSAL, action_type: "update_task" }), "Cancel");
});

test("resultTag generalises Figma's 'ADDED' completion eyebrow across every action_type", () => {
  assert.equal(resultTag({ ...TX_PROPOSAL, action_type: "create_task" }), "ADDED");
  assert.equal(resultTag({ ...TX_PROPOSAL, action_type: "update_event" }), "UPDATED");
  assert.equal(resultTag({ ...TX_PROPOSAL, action_type: "delete_event" }), "REMOVED");
  assert.equal(resultTag({ ...TX_PROPOSAL, action_type: "complete_task" }), "COMPLETED");
  assert.equal(resultTag({ ...TX_PROPOSAL, action_type: "batch_operations" }), "DONE");
});
