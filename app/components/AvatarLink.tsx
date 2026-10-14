"use client";

// The ONE profile entry point on authenticated screens. Profile is deliberately
// kept OUT of the bottom navigation; this fixed avatar is its reachable control
// on every top-level authed screen. Renders nothing until a real user is known,
// and nothing on /profile itself (you are already there).

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useAuth } from "../lib/auth";

export default function AvatarLink() {
  const { status, user } = useAuth();
  const pathname = usePathname() || "/";

  if (status !== "authenticated" || !user) return null;
  if (pathname === "/profile") return null;

  const initial = (user.name || user.email || "?").trim().charAt(0).toUpperCase();

  return (
    <Link
      href="/profile"
      aria-label="Your profile"
      className="fixed right-3 z-50 flex h-11 w-11 items-center justify-center rounded-full transition-transform active:scale-95 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring lg:hidden"
      style={{ top: "max(0.75rem, env(safe-area-inset-top))" }}
    >
      <span className="flex h-10 w-10 items-center justify-center rounded-full border border-line-strong bg-surface text-sm font-bold text-primary shadow-sm backdrop-blur-md">
        {initial}
      </span>
    </Link>
  );
}
