"use client";

import React from "react";
import { usePathname } from "next/navigation";
import Sidebar from "../Sidebar";
import AuthGuard from "./AuthGuard";
import BottomNav from "./BottomNav";
import AvatarLink from "./AvatarLink";
import RemindersBell from "./RemindersBell";

// Routes that must render WITHOUT auth and without the app navigation chrome.
const PUBLIC_ROUTES = new Set<string>(["/login", "/welcome"]);

export default function AppChrome({ children }: { children: React.ReactNode }) {
  const pathname = usePathname() || "/";

  if (PUBLIC_ROUTES.has(pathname)) {
    return <main className="flex-1 overflow-y-auto w-full">{children}</main>;
  }

  return (
    <>
      <div className="hidden lg:block">
        <Sidebar />
      </div>
      {/* Reserve space for the fixed mobile BottomNav so no page's own content
          can hide behind it; the desktop Sidebar needs no such inset. */}
      <main className="flex-1 overflow-y-auto w-full pb-[calc(64px+env(safe-area-inset-bottom))] lg:pb-0">
        <AuthGuard>
          {/* Shared authenticated chrome: one avatar entry to /profile, one
              mobile bottom navigation, one in-app reminders bell (B11.1) —
              all only render once a real user is resolved (AuthGuard gates
              this subtree) and the bell only when there is something to show. */}
          <AvatarLink />
          <RemindersBell />
          {children}
          <BottomNav />
        </AuthGuard>
      </main>
    </>
  );
}
