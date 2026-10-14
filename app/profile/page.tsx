"use client";
import React, { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { LogOut, ChevronRight } from "lucide-react";
import { useAuth } from "../lib/auth";
import { initials } from "./personalDetails";

// Phase 07F (Figma 261:130 "Profile"): Figma's row set is name/email
// identity + Personal details + Who Am I + Settings + Help & about. This
// build has no Help/About feature (no route, no content) — per the task's
// own "Help/About if currently available" wording and the anti-dead-control
// rule, that row is simply not shown rather than linked to nothing. Sign Out
// isn't in the captured Figma region at all but is required, real, verified
// functionality, so it stays — styled as a deliberately distinct destructive
// row, not a neutral navigation row.
const DISPLAY_FONT = 'var(--font-bricolage), "Bricolage Grotesque", sans-serif';
const BODY_FONT = 'var(--font-hanken), "Hanken Grotesk", sans-serif';

function Row({ href, title, subtitle }: { href: string; title: string; subtitle: string }) {
  return (
    <Link
      href={href}
      className="w-full bg-surface border border-line p-4 rounded-[16px] flex items-center justify-between hover:bg-surface-2 transition-colors min-h-[56px]"
    >
      <div className="min-w-0">
        <p className="text-[14px] font-semibold text-primary" style={{ fontFamily: BODY_FONT }}>{title}</p>
        <p className="text-[11px] text-muted mt-0.5" style={{ fontFamily: BODY_FONT }}>{subtitle}</p>
      </div>
      <ChevronRight size={20} className="text-muted shrink-0" />
    </Link>
  );
}

export default function ProfilePage() {
  const router = useRouter();
  const { user, logout } = useAuth();
  const [signingOut, setSigningOut] = useState(false);

  const handleLogout = async () => {
    if (signingOut) return;
    setSigningOut(true);
    await logout();
    router.replace("/login");
  };

  return (
    <div className="min-h-screen bg-app flex justify-center">
      <div className="w-full max-w-md min-h-screen relative flex flex-col">
        <div
          className="px-6 pb-32 space-y-6"
          style={{ paddingTop: "max(1.5rem, calc(env(safe-area-inset-top) + 0.5rem))" }}
        >
          <header>
            <h1
              className="text-[26px] leading-[1.15] font-extrabold text-primary"
              style={{ fontFamily: DISPLAY_FONT, fontVariationSettings: '"opsz" 14, "wdth" 100' }}
            >
              Profile
            </h1>
          </header>

          <div className="flex items-center gap-3.5">
            <div className="h-[72px] w-[72px] shrink-0 rounded-full bg-accent-lav flex items-center justify-center text-[22px] font-semibold text-primary">
              {initials(user?.name, user?.email)}
            </div>
            <div className="min-w-0">
              <h2 className="text-[19px] font-semibold text-primary truncate" style={{ fontFamily: BODY_FONT }}>
                {user?.name || "MONOA user"}
              </h2>
              <p className="text-[12px] text-muted truncate" style={{ fontFamily: BODY_FONT }}>{user?.email}</p>
            </div>
          </div>

          <div className="space-y-2">
            <h3 className="text-[18px] font-semibold text-primary px-1" style={{ fontFamily: BODY_FONT }}>Personal</h3>
            <Row href="/profile/personal-details" title="Personal details" subtitle="Your name" />
            <Row href="/profile/who-am-i" title="Who Am I?" subtitle="Games, sports, movies & TV" />
          </div>

          <div className="space-y-2">
            <h3 className="text-[18px] font-semibold text-primary px-1" style={{ fontFamily: BODY_FONT }}>App</h3>
            <Row href="/settings" title="Settings" subtitle="Appearance, notifications, privacy & account" />
          </div>

          <button
            onClick={handleLogout}
            disabled={signingOut}
            className="w-full border border-danger/20 bg-danger/10 hover:bg-danger/15 text-danger p-4 rounded-[16px] flex items-center justify-center gap-2 font-bold transition-colors disabled:opacity-60 min-h-[56px]"
            style={{ fontFamily: BODY_FONT }}
          >
            <LogOut size={18} /> {signingOut ? "Signing out…" : "Sign Out"}
          </button>
        </div>
      </div>
    </div>
  );
}
