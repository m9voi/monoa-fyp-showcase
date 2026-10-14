"use client";

// Phase 5H-B5 — after a reload, quietly ask the server whether any create whose
// response was lost actually committed, and if so tell the user (once) so they
// don't re-submit. Renders nothing unless there is something to report.

import React, { useCallback, useEffect, useRef, useState } from "react";
import { useAuth } from "../lib/auth";
import { reconcilePendingCreates, reconcileMessage } from "../lib/reconcile";
import { listPendingCreates } from "../lib/pendingCreate";

export default function PendingRecovery() {
  const { status } = useAuth();
  const [msg, setMsg] = useState("");
  const running = useRef(false);

  const run = useCallback(async () => {
    if (running.current) return;
    if (listPendingCreates().length === 0) return;
    running.current = true;
    try {
      const result = await reconcilePendingCreates();
      const m = reconcileMessage(result);
      if (m) setMsg(m);
    } finally {
      running.current = false;
    }
  }, []);

  useEffect(() => {
    if (status !== "authenticated") return;
    void run();
    const onVisible = () => {
      if (document.visibilityState === "visible") void run();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [status, run]);

  useEffect(() => {
    if (!msg) return;
    const t = setTimeout(() => setMsg(""), 8000);
    return () => clearTimeout(t);
  }, [msg]);

  if (!msg) return null;
  return (
    <div
      role="status"
      className="fixed bottom-4 left-1/2 -translate-x-1/2 z-[200] max-w-[calc(100vw-2rem)] sm:max-w-md
                 bg-surface border border-white/15 text-primary text-sm rounded-2xl px-4 py-3
                 shadow-2xl flex items-start gap-3"
    >
      <span className="min-w-0">{msg}</span>
      <button
        onClick={() => setMsg("")}
        aria-label="Dismiss"
        className="shrink-0 text-muted hover:text-white text-xs font-bold"
      >
        ✕
      </button>
    </div>
  );
}
