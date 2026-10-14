// MONOA typography — added for the Launch experience (and available to future
// MONOA surfaces) without disturbing the rest of the app.
//
// IMPORTANT: these two calls only expose CSS custom properties
// (--font-bricolage / --font-hanken) via `.variable`. They do NOT set
// `font-family` on anything by themselves — that only happens where a
// stylesheet explicitly writes `font-family: var(--font-bricolage)`, which
// today is only app/welcome/launch.module.css. Every existing route keeps
// rendering in Inter (app/layout.tsx's `inter.className`), unchanged.
import { Bricolage_Grotesque, Hanken_Grotesk } from "next/font/google";

export const bricolageGrotesque = Bricolage_Grotesque({
  subsets: ["latin"],
  weight: "variable",
  variable: "--font-bricolage",
  display: "swap",
});

export const hankenGrotesk = Hanken_Grotesk({
  subsets: ["latin"],
  weight: "variable",
  variable: "--font-hanken",
  display: "swap",
});
