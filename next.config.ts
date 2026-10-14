import type { NextConfig } from "next";

// Phase 5A — secure development foundation.
//
// The browser (and the iPhone) talk to ONE origin. Next.js serves the app at
// "/" and transparently proxies "/api/*" to the FastAPI backend on the server
// side, so the browser never issues a request to http://<host>:8080 directly.
//
//   browser ──HTTPS──> reverse proxy (dev/Caddyfile) ──> Next.js :3000
//                                                          ├─ /        app
//                                                          └─ /api/*   ─┐
//                                                     rewrite (server) ─┴─> COREOS_BACKEND_ORIGIN
//
// COREOS_BACKEND_ORIGIN is a SERVER-SIDE loopback address (never shipped to the
// browser). It defaults to the local FastAPI port and is overridable for other
// topologies. It must stay an internal address — do not point it at a public URL.
const BACKEND_ORIGIN = (
  process.env.COREOS_BACKEND_ORIGIN || "http://127.0.0.1:8080"
).replace(/\/+$/, "");

// Hostnames allowed to load Next.js dev assets / HMR from a non-localhost origin
// (the reverse-proxy host, plus plain localhost). Overridable via COREOS_DEV_HOST
// — for LAN/physical-device dev testing, set it to your own machine's LAN IP or
// hostname rather than relying on a hardcoded address here.
const DEV_HOST = process.env.COREOS_DEV_HOST || "coreos.local";
const allowedDevOrigins = Array.from(
  new Set([DEV_HOST, "coreos.local", "localhost", "127.0.0.1"]),
);

const isDev = process.env.NODE_ENV !== "production";

// Phase 5H-B4 (M5) — baseline browser security headers on the HTML/document
// layer (the API JSON layer additionally sends X-Content-Type-Options from
// FastAPI). Verified against `next start` on :3000; when the dev Caddy proxy is
// in front it forwards these unchanged.
//
// CSP note: this is a conservative policy chosen to NOT break the current app
// (Next.js inline bootstrap, Tailwind/Leaflet inline styles, Leaflet raster
// tiles from tile.openstreetmap.org, same-origin /api + dev HMR websocket).
// `script-src` still needs 'unsafe-inline' (and 'unsafe-eval' in dev for the
// Turbopack HMR runtime); moving to a strict nonce/hash `script-src` needs
// request-time nonce wiring and is tracked as a separate production-hardening
// item. HSTS is intentionally NOT set here — it is a deployment/proxy concern.
const cspDirectives = [
  "default-src 'self'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
  "object-src 'none'",
  "img-src 'self' data: blob: https://tile.openstreetmap.org https://*.tile.openstreetmap.org",
  "style-src 'self' 'unsafe-inline'",
  `script-src 'self' 'unsafe-inline'${isDev ? " 'unsafe-eval'" : ""}`,
  "font-src 'self'",
  `connect-src 'self'${isDev ? " ws: wss:" : ""}`,
];

const securityHeaders = [
  { key: "Content-Security-Policy", value: cspDirectives.join("; ") },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  {
    key: "Permissions-Policy",
    // geolocation=(self) is REQUIRED — Explore/Activity use the device location.
    value: "geolocation=(self), camera=(), microphone=(), payment=(), usb=()",
  },
];

const nextConfig: NextConfig = {
  allowedDevOrigins,

  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },

  // Same-origin API: the browser calls "/api/..." on this origin; Next forwards
  // it to the backend server-side. Method, query string, body and cookies are
  // preserved. This is what lets NEXT_PUBLIC_API_BASE_URL stay blank.
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${BACKEND_ORIGIN}/api/:path*` },
    ];
  },

  // Pin the workspace root to THIS folder. A stray package-lock.json one level
  // up (C:\Users\musab\Desktop\package-lock.json - an old copy of this same
  // project) makes Next infer the Desktop as the monorepo root and print a
  // "multiple lockfiles / inferred workspace root" warning. This is a
  // CoreOS-local setting only; it does not touch or affect the other file.
  turbopack: {
    root: __dirname,
  },
};

export default nextConfig;
