"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Settings, BrainCircuit, LogOut } from "lucide-react";
import { useAuth } from "./lib/auth";
import { NAV_ITEMS, isNavActive } from "./lib/nav";
import { NAV_ICONS } from "./components/NavIcons";

export default function Sidebar() {
  const pathname = usePathname() || "/";
  const router = useRouter();
  const { status, user, logout } = useAuth();

  // Sidebar is app chrome — only show it to an authenticated user.
  if (status !== "authenticated" || !user) return null;

  const handleLogout = async () => {
    await logout();
    router.replace("/login");
  };

  const initial = (user.name || user.email || "?").trim().charAt(0).toUpperCase();

  return (
    <aside className="hidden lg:flex w-64 bg-nav text-secondary flex-col transition-all border-r border-nav-border h-screen">
      <div className="p-6 flex items-center gap-3 text-white">
        <div className="bg-white p-2 rounded-lg">
          <BrainCircuit size={24} className="text-black" />
        </div>
        <span className="text-xl font-bold tracking-tight">MONOA</span>
      </div>

      <nav className="flex-1 px-4 py-6 space-y-2">
        {NAV_ITEMS.map((item) => {
          const active = isNavActive(pathname, item.href);
          const Icon = NAV_ICONS[item.key];
          return (
            <Link
              key={item.key}
              href={item.href}
              aria-current={active ? "page" : undefined}
              className={`flex items-center gap-3 px-4 py-3 rounded-xl transition-all duration-200 ${
                active ? "bg-white/10 text-white shadow-sm" : "text-zinc-400 hover:bg-white/5 hover:text-white"
              }`}
            >
              <Icon
                active={active}
                className={item.center ? "h-5 w-5" : `h-5 w-5 ${active ? "text-white" : "text-zinc-500"}`}
              />
              <span className="font-medium">{item.label}</span>
            </Link>
          );
        })}
      </nav>

      <div className="p-4 mt-auto border-t border-white/10 space-y-2">
        <Link href="/settings" className="flex items-center gap-3 px-4 py-3 hover:bg-white/5 hover:text-white rounded-xl transition-all duration-200">
          <Settings size={20} className="text-zinc-500" />
          <span className="font-medium">Settings</span>
        </Link>

        <Link href="/profile" className="flex items-center gap-3 px-4 py-3 mt-2 bg-surface rounded-xl border border-white/10 hover:bg-white/5 transition-colors">
          <div className="w-8 h-8 rounded-full bg-white/10 text-white flex items-center justify-center font-bold text-sm border border-white/20">
            {initial}
          </div>
          <div className="flex flex-col min-w-0">
            <span className="text-sm font-bold text-white truncate">{user.name || "MONOA user"}</span>
            <span className="text-xs text-zinc-500 truncate">{user.email}</span>
          </div>
        </Link>

        <button
          onClick={handleLogout}
          className="w-full flex items-center gap-3 px-4 py-3 hover:bg-white/5 hover:text-white rounded-xl transition-all duration-200 text-zinc-400"
        >
          <LogOut size={20} className="text-zinc-500" />
          <span className="font-medium">Sign out</span>
        </button>
      </div>
    </aside>
  );
}
