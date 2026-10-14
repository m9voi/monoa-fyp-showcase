"use client";

import React, { useEffect } from "react";
import { usePathname, useRouter } from "next/navigation";
import { useAuth } from "../lib/auth";
import { unauthenticatedDestination } from "../lib/launchRoute";

/**
 * Wrap every protected page. While the session is being checked it renders a
 * neutral placeholder (never another user's data). If unauthenticated it
 * redirects straight to /login — see launchRoute.ts for the exact,
 * unit-tested rule. Children are keyed by user id so switching accounts
 * cannot leave stale private state mounted.
 */
export default function AuthGuard({ children }: { children: React.ReactNode }) {
  const { status, user, refresh } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (status === "unauthenticated") {
      router.replace(unauthenticatedDestination(pathname));
    }
  }, [status, pathname, router]);

  // Phase 5H-B5: we could not reach the server to check the session. Do NOT
  // redirect to /login (that would look like a forced logout on a flaky
  // connection) — offer a retry.
  if (status === "offline") {
    return (
      <div className="min-h-screen bg-app flex flex-col items-center justify-center gap-4 px-6 text-center">
        <p className="text-sm text-secondary">
          Couldn&apos;t reach MONOA to check your session. You may be offline.
        </p>
        <button
          onClick={() => void refresh()}
          className="bg-white text-black text-sm font-semibold px-5 py-2.5 rounded-full"
        >
          Try again
        </button>
      </div>
    );
  }

  if (status !== "authenticated" || !user) {
    return (
      <div className="min-h-screen bg-app flex items-center justify-center">
        <div className="w-6 h-6 border-2 border-white/20 border-t-white/80 rounded-full animate-spin" />
      </div>
    );
  }

  return <div key={user.id}>{children}</div>;
}
