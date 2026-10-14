"use client";

import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { apiFetch, ApiError } from "./api";
import {
  clearAllPendingCreates,
  pendingActionForAuthTransition,
  type AuthTransition,
} from "./pendingCreate";
import type { AuthUser } from "./types";

// Phase 5H-B5: "offline" is distinct from "unauthenticated". A network / timeout
// failure while checking the session is NOT proof the user is logged out — it
// must never bounce an authenticated user to /login. Only a real HTTP 401 does.
type AuthStatus = "loading" | "authenticated" | "unauthenticated" | "offline";

interface AuthContextValue {
  status: AuthStatus;
  user: AuthUser | null;
  /** Re-check the session against the backend. */
  refresh: () => Promise<void>;
  /** POST credentials; on success the session cookie is set by the server. */
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string, name?: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [user, setUser] = useState<AuthUser | null>(null);
  const didInit = useRef(false);
  const userRef = useRef<AuthUser | null>(null);
  useEffect(() => {
    userRef.current = user;
  }, [user]);

  // Phase 5H-B5: EVERY auth transition routes its pending-record decision
  // through the one policy function (pendingActionForAuthTransition). Explicit
  // logout -> clear; a bare session expiry -> keep (reconciliation is merely
  // suspended, and a same-user re-auth still needs the opaque token); a new
  // authentication -> clear only on a VERIFIED switch to a different user id.
  const applyAuthTransition = useCallback((event: AuthTransition) => {
    if (pendingActionForAuthTransition(userRef.current?.id, event) === "clear") {
      clearAllPendingCreates();
    }
  }, []);

  const refresh = useCallback(async () => {
    try {
      const data = await apiFetch<{ user: AuthUser }>("/api/auth/me", { kind: "auth" });
      applyAuthTransition({ kind: "authenticated", userId: data.user.id });
      setUser(data.user);
      setStatus("authenticated");
    } catch (err) {
      if (err instanceof ApiError && err.kind === "http" && err.status === 401) {
        // The session is not valid. This is NOT an intentional sign-out: the
        // policy keeps the opaque pending records (a same-user re-auth still
        // needs them; they expire at the TTL regardless) and reconciliation is
        // simply suspended while unauthenticated.
        applyAuthTransition({ kind: "expired" });
        setUser(null);
        setStatus("unauthenticated");
        return;
      }
      // Network / timeout / offline / 5xx: outcome unknown.
      if (userRef.current) {
        // We were already signed in — a transient blip must not sign us out.
        setStatus("authenticated");
      } else {
        setStatus("offline");
      }
    }
  }, [applyAuthTransition]);

  useEffect(() => {
    if (didInit.current) return;
    didInit.current = true;
    void refresh();
  }, [refresh]);

  const login = useCallback(
    async (email: string, password: string) => {
      const data = await apiFetch<{ user: AuthUser }>("/api/auth/login", {
        method: "POST",
        kind: "auth",
        body: JSON.stringify({ email, password }),
      });
      applyAuthTransition({ kind: "authenticated", userId: data.user.id });
      setUser(data.user);
      setStatus("authenticated");
    },
    [applyAuthTransition],
  );

  const register = useCallback(
    async (email: string, password: string, name?: string) => {
      const data = await apiFetch<{ user: AuthUser }>("/api/auth/register", {
        method: "POST",
        kind: "auth",
        body: JSON.stringify({ email, password, name }),
      });
      applyAuthTransition({ kind: "authenticated", userId: data.user.id });
      setUser(data.user);
      setStatus("authenticated");
    },
    [applyAuthTransition],
  );

  const logout = useCallback(async () => {
    try {
      await apiFetch("/api/auth/logout", { method: "POST", kind: "auth" });
    } catch {
      /* best-effort — clear locally regardless */
    } finally {
      // EXPLICIT sign-out on this browser -> a clean account boundary.
      applyAuthTransition({ kind: "logout" });
      setUser(null);
      setStatus("unauthenticated");
    }
  }, [applyAuthTransition]);

  return (
    <AuthContext.Provider value={{ status, user, refresh, login, register, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
