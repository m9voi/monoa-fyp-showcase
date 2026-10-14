"use client";

// Mo — the dedicated, independently-reachable AI command destination (B1).
// Uses the SAME canonical <OmniBar> as the Today entry point (one implementation,
// one execution path, one persistence path) — here opted into its full
// scrollable conversation feed (Section 6/17: a real conversation, not a
// floating result card over empty space). The page fills exactly the space
// AppChrome's <main> already gives it (which itself reserves room for the
// fixed mobile BottomNav), so only the feed scrolls internally and the
// composer stays pinned right above the nav bar.
//
// Phase 07C (Figma 404:*/425:2): every one of Mo's Figma frames repeats the
// same small centered "Mo" nav-style title (17px semibold), never a big
// display headline like Today/Plan use for their own identity — so this page
// intentionally does not use the shared .monoaPageTitle class.

import React from "react";
import OmniBar from "../components/OmniBar";

const BODY_FONT = 'var(--font-hanken), "Hanken Grotesk", sans-serif';

export default function MoPage() {
  return (
    <div className="flex h-full justify-center bg-app">
      <div className="flex h-full w-full max-w-md flex-col px-6">
        <header
          className="shrink-0 pb-3 text-center"
          style={{ paddingTop: "max(1.5rem, calc(env(safe-area-inset-top) + 0.5rem))" }}
        >
          <p className="text-[17px] font-semibold text-primary" style={{ fontFamily: BODY_FONT }}>
            Mo
          </p>
        </header>

        <OmniBar autoFocus showIdleIntro conversation className="pb-4" />
      </div>
    </div>
  );
}
