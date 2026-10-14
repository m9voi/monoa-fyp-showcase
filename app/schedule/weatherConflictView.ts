// B5 — pure view helpers for the Plan weather-conflict surface. No React, no fetch.
import type { WeatherConflict } from "../lib/types";

export type WeatherUiState =
  | "idle"
  | "keeping"
  | "kept"
  | "searching"
  | "candidates"
  | "confirming"
  | "moved"
  | "stale"
  | "error"
  | "no_alt";

export function unresolvedWeather(conflicts: WeatherConflict[] | undefined): WeatherConflict[] {
  return (conflicts ?? []).filter((c) => !c.decision);
}

export function weatherKey(c: WeatherConflict): string {
  return c.fingerprint || c.event_id;
}

/** lucide icon name for a hazard — paired with text, never colour-only. */
export function hazardIcon(hazardType: string): "CloudLightning" | "CloudRain" | "Thermometer" | "Wind" | "Snowflake" | "CloudAlert" {
  switch (hazardType) {
    case "thunderstorm": return "CloudLightning";
    case "heavy_rain": return "CloudRain";
    case "heavy_snow": return "Snowflake";
    case "heat": return "Thermometer";
    case "wind": return "Wind";
    case "cold": return "Snowflake";
    default: return "CloudAlert";
  }
}

export function hazardLabel(hazardType: string): string {
  return (
    {
      thunderstorm: "Thunderstorms",
      heavy_rain: "Heavy rain",
      heavy_snow: "Heavy snow",
      heat: "High heat",
      wind: "Strong wind",
      cold: "Freezing",
    } as Record<string, string>
  )[hazardType] ?? "Severe weather";
}

export function tierLabel(tier: number): string {
  return tier >= 3 ? "Severe" : tier === 2 ? "Likely" : "Possible";
}

const WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2026-09-21" -> "Mon 21 Sep". Empty string on bad input. */
export function occurrenceLabel(isoDate: string | null | undefined): string {
  if (!isoDate) return "";
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(isoDate);
  if (!m) return "";
  const y = Number(m[1]), mo = Number(m[2]), day = Number(m[3]);
  const d = new Date(Date.UTC(y, mo - 1, day));
  if (
    Number.isNaN(d.getTime()) ||
    d.getUTCFullYear() !== y || d.getUTCMonth() !== mo - 1 || d.getUTCDate() !== day
  ) return "";
  return `${WD[(d.getUTCDay() + 6) % 7]} ${d.getUTCDate()} ${MON[d.getUTCMonth()]}`;
}

/** True when the weather decision would change only one recurring occurrence. */
export function isOccurrenceScoped(c: WeatherConflict): boolean {
  return c.scope === "occurrence" || !!c.occurrence_date;
}

export function isWeatherBusy(s: WeatherUiState): boolean {
  return s === "keeping" || s === "searching" || s === "confirming";
}
