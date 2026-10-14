"use client";
// Phase 5F — the ONE minimal weather ACCEPTANCE surface (not a product feature).
//
// A compact, read-only "expected conditions over the next couple of hours" line
// at the activity's search origin. It exists only to verify the Weather Engine
// end-to-end (origin -> /api/weather/context -> provider -> normalised model ->
// UI). It is NOT a decision that every Activity view must permanently show
// generic weather:
//   * weather stays CONTEXTUAL INFRASTRUCTURE — future Activity/Schedule/Home
//     integration should surface/elevate it only when it MATERIALLY matters;
//   * activity ranking is NOT weather-aware in 5F, and there is no
//     weather_sensitivity model yet;
//   * premium dynamic icons and the AMBIENT -> ATTENTION -> ACTION hierarchy
//     are deferred.
//
// Every displayed value is derived from the SAME forecast window, so the
// "next 2 hrs" label is structurally truthful. Nothing is fabricated: an
// unknown condition says "Conditions unknown"; no window / a provider failure
// says "Weather unavailable" — never fake numbers.

import { useEffect, useRef, useState } from "react";
import { CloudRain, CloudSun, Cloud, Sun, CloudFog, Snowflake, Zap, CircleHelp } from "lucide-react";
import { apiFetch } from "../lib/api";
import type { WeatherContextResponse, WeatherCondition } from "../lib/types";
import type { SearchOrigin } from "../map/searchOrigin";

type State =
  | { phase: "loading" }
  | { phase: "ok"; res: WeatherContextResponse }
  | { phase: "unavailable" };

const CONDITION_LABEL: Record<WeatherCondition, string> = {
  clear: "Clear", mostly_clear: "Mostly clear", partly_cloudy: "Partly cloudy",
  cloudy: "Cloudy", fog: "Fog", drizzle: "Drizzle", rain: "Rain",
  heavy_rain: "Heavy rain", freezing_rain: "Freezing rain", snow: "Snow",
  heavy_snow: "Heavy snow", thunderstorm: "Thunderstorm", unknown: "Conditions unknown",
};

function conditionIcon(c: WeatherCondition) {
  const p = { size: 13, className: "shrink-0" };
  switch (c) {
    case "clear": return <Sun {...p} className="shrink-0 text-amber-300" />;
    case "mostly_clear":
    case "partly_cloudy": return <CloudSun {...p} className="shrink-0 text-amber-200/80" />;
    case "cloudy": return <Cloud {...p} className="shrink-0 text-muted" />;
    case "fog": return <CloudFog {...p} className="shrink-0 text-muted" />;
    case "drizzle":
    case "rain":
    case "heavy_rain":
    case "freezing_rain": return <CloudRain {...p} className="shrink-0 text-blue-300" />;
    case "snow":
    case "heavy_snow": return <Snowflake {...p} className="shrink-0 text-cyan-200" />;
    case "thunderstorm": return <Zap {...p} className="shrink-0 text-amber-300" />;
    default: return <CircleHelp {...p} className="shrink-0 text-muted" />;
  }
}

/** A read-only forecast line for "around the next couple of hours" at `origin`.
 *  Does not affect activity results in any way. The parent gives this a `key`
 *  tied to the origin, so a changed origin remounts it — the effect stays a
 *  one-shot with no synchronous setState. */
export default function WeatherLine({ origin }: { origin: SearchOrigin }) {
  const [state, setState] = useState<State>({ phase: "loading" });
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    const start = new Date();
    const end = new Date(start.getTime() + 2 * 60 * 60 * 1000);
    void (async () => {
      try {
        const res = await apiFetch<WeatherContextResponse>("/api/weather/context", {
          method: "POST",
          kind: "provider",
          body: JSON.stringify({
            origin: { latitude: origin.latitude, longitude: origin.longitude },
            start_time: start.toISOString(),      // tz-aware (Z)
            end_time: end.toISOString(),
          }),
        });
        if (!mounted.current) return;
        // This line is explicitly a FORECAST ("next 2 hrs"). Every value it
        // shows is derived from the aggregated window, so it only renders when
        // the engine returned one. A current-conditions-only response is
        // "unavailable" for THIS surface (the API still carries `current` for
        // other future consumers).
        setState(res.status === "OK" && res.window ? { phase: "ok", res } : { phase: "unavailable" });
      } catch {
        if (!mounted.current) return;
        setState({ phase: "unavailable" });
      }
    })();
    return () => { mounted.current = false; };
    // one-shot: origin identity is fixed for this mount (parent re-keys on change)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (state.phase === "loading") {
    return (
      <p className="text-[11px] text-muted flex items-center gap-1.5">
        <span className="w-2 h-2 rounded-full bg-white/25 animate-pulse" /> Checking conditions…
      </p>
    );
  }
  if (state.phase === "unavailable") {
    return <p className="text-[11px] text-muted">Weather unavailable right now.</p>;
  }

  // EVERY value below is derived from the same forecast window (`window`), so
  // the "next 2 hrs" label is structurally truthful — there is no mixing of
  // current-conditions and forecast values.
  const w = state.res.window!;
  const cond = w.dominant_condition as WeatherCondition;
  const temp = w.temperature_c;                       // representative temp OVER the window
  const phrase = w.phrases?.[0] ?? null;              // factual, non-alarmist
  const pp = w.precipitation_probability_max;

  return (
    <p className="text-[11px] text-secondary flex items-center flex-wrap gap-x-2 gap-y-0.5">
      {conditionIcon(cond)}
      <span className="text-secondary">{CONDITION_LABEL[cond]}</span>
      {temp != null && <span>· {Math.round(temp)}°</span>}
      {phrase && <span className="text-amber-300/85">· {phrase}</span>}
      {phrase == null && pp != null && pp >= 30 && (
        <span className="text-muted">· {pp}% chance of rain</span>
      )}
      <span className="text-muted">· next 2 hrs</span>
    </p>
  );
}
