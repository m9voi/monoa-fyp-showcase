"use client";

import { useCallback, useEffect, useState } from "react";
import { apiFetch, ApiError, describeFailure, type RequestKind } from "./api";

interface ApiState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  /** Re-run the request. */
  reload: () => void;
}

/**
 * Read-only data loader for a GET endpoint.
 *
 * Phase 5H-B5:
 *   * bounded — the request inherits apiFetch's deadline (never hangs);
 *   * cancelable — a path change / unmount ABORTS the in-flight request
 *     (not just ignores it), and the resulting "canceled" failure is silent;
 *   * truthful — a timeout / offline / network failure produces distinct copy.
 */
export function useApiData<T>(
  path: string | null,
  enabled = true,
  kind: RequestKind = "crud",
): ApiState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(enabled && path !== null);
  const [nonce, setNonce] = useState(0);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    if (!enabled || path === null) return;
    let ignore = false;
    const ctrl = new AbortController();

    (async () => {
      try {
        const result = await apiFetch<T>(path, { kind, signal: ctrl.signal });
        if (!ignore) {
          setData(result);
          setError(null);
        }
      } catch (err) {
        // A caller-initiated cancel (navigation / unmount / reload) is not an
        // error the user should ever see.
        if (err instanceof ApiError && err.kind === "canceled") return;
        if (!ignore) {
          setData(null);
          setError(describeFailure(err, { verb: "load that" }));
        }
      } finally {
        if (!ignore) setLoading(false);
      }
    })();

    return () => {
      ignore = true;
      ctrl.abort();
    };
  }, [path, enabled, kind, nonce]);

  return { data, error, loading, reload };
}
