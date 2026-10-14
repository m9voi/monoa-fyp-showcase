"use client";

// Phase 5H-B5 — create-form glue for the lost-response recovery flow.
//
// A create sheet:
//   * calls arm() right before its create POST (records the opaque token),
//   * calls resolve() on a confirmed success OR a definite 4xx failure,
//   * leaves the record in place on an UNKNOWN outcome (timeout / network /
//     5xx) so a later reload can reconcile it,
//   * reads `recovered` — true when, on mount, the server confirmed an earlier
//     lost create of this category had in fact been saved (so the UI can warn
//     the user they may not need to add it again).

import { useEffect, useState } from "react";
import {
  rememberPendingCreate,
  forgetPendingCreate,
  type PendingKind,
} from "./pendingCreate";
import { reconcilePendingCreates } from "./reconcile";

export function usePendingCreate(kind: PendingKind, token: string) {
  const [recovered, setRecovered] = useState(false);

  useEffect(() => {
    let alive = true;
    void reconcilePendingCreates().then((r) => {
      if (alive && r.committed.includes(kind)) setRecovered(true);
    });
    return () => {
      alive = false;
    };
  }, [kind]);

  return {
    recovered,
    arm: () => rememberPendingCreate(kind, token),
    resolve: () => forgetPendingCreate(token),
  };
}
