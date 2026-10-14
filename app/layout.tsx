import type { Metadata, Viewport } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import Providers from "./components/Providers";
import AppChrome from "./components/AppChrome";
import { bricolageGrotesque, hankenGrotesk } from "./lib/fonts";

const inter = Inter({ subsets: ["latin"] });

export const metadata: Metadata = {
  title: "MONOA - AI Life Assistant",
  description: "Unified productivity and finance dashboard",
  manifest: "/manifest.json",
  icons: {
    icon: [
      { url: "/favicon-32.png", sizes: "32x32", type: "image/png" },
      { url: "/icon-192.png", sizes: "192x192", type: "image/png" },
    ],
    apple: [{ url: "/apple-touch-icon.png", sizes: "180x180", type: "image/png" }],
  },
  appleWebApp: {
    capable: true,
    statusBarStyle: "black-translucent",
    title: "MONOA",
  },
};

// viewportFit "cover" lets the app draw under the notch/Dynamic Island and
// home indicator, which is what makes every existing env(safe-area-inset-*)
// rule in the app (AvatarLink, BottomNav, RemindersBell, login) resolve to a
// real value instead of 0 on edge-to-edge iPhones. maximumScale/userScalable
// disable page-level pinch/double-tap zoom, which has no in-app affordance to
// undo inside the Capacitor WebView shell; iOS's own system-level Zoom
// accessibility feature is a separate OS gesture and is unaffected by this.
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 1,
  userScalable: false,
  viewportFit: "cover",
  themeColor: "#1b2130",
};

// Pre-hydration appearance bootstrap (B4). Runs synchronously before first
// paint so the correct theme is on <html> before any UI is visible — no
// wrong-theme flash, no dependency on React having hydrated. Only an explicit
// Light/Dark choice stamps data-theme; "system" (and a missing/corrupt value)
// leaves it unset so the CSS `prefers-color-scheme` block resolves it and keeps
// following the OS live with zero JavaScript. Tolerates blocked/absent
// localStorage. Inline-script is permitted by the app CSP (script-src
// 'unsafe-inline'); no external dependency.
const THEME_BOOTSTRAP = `(function(){try{var r=localStorage.getItem("monoa.appearance");var m=null;if(r){var s=r.trim().replace(/^["']|["']$/g,"").toLowerCase();if(s==="light"||s==="dark"||s==="system")m=s;}var d=document.documentElement;if(m==="light"||m==="dark")d.setAttribute("data-theme",m);else d.removeAttribute("data-theme");}catch(e){}})();`;

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOTSTRAP }} />
      </head>
      <body
        className={`${inter.className} ${bricolageGrotesque.variable} ${hankenGrotesk.variable} bg-app flex h-screen overflow-hidden relative`}
      >
        <Providers>
          <AppChrome>{children}</AppChrome>
        </Providers>
      </body>
    </html>
  );
}
