// B5 — deterministic (no-AI) indoor/outdoor/uncertain classification for an
// event created from an Activity/place context. The DEFAULT is always
// "uncertain", never "outdoor". A purpose-built venue type in the real place
// data can refine it; the activity alone never forces "outdoor" for a sport
// that is commonly played indoors.

export type Environment = "indoor" | "outdoor" | "uncertain";

// Explicit, reviewed map. Only activities that are outdoor *by nature* (open
// geography / no facility) are "outdoor". Court/hall/pool sports stay
// "uncertain" so the real place decides — an indoor badminton/futsal hall must
// remain indoor.
const ACTIVITY_ENV: Record<string, Environment> = {
  running: "outdoor",
  jogging: "outdoor",
  cycling: "outdoor",
  hiking: "outdoor",
  trail: "outdoor",
  golf: "outdoor",
  football: "outdoor", // an open-field match; the indoor small-sided game is "futsal"
  // court / hall / pool sports — the place context decides
  badminton: "uncertain",
  futsal: "uncertain",
  basketball: "uncertain",
  tennis: "uncertain",
  squash: "uncertain",
  volleyball: "uncertain",
  swimming: "uncertain",
  gym: "uncertain",
  yoga: "uncertain",
  bowling: "uncertain",
};

// Substrings that appear in real OSM/Google place-type tokens.
const OUTDOOR_TYPE_HINTS = [
  "park", "garden", "pitch", "leisure=pitch", "leisure=park", "leisure=garden",
  "leisure=track", "sport=athletics", "athletics", "golf_course", "golf course",
  "playground", "dog_park", "beach", "natural=", "recreation_ground", "common",
];
const INDOOR_TYPE_HINTS = [
  "gym", "fitness_cent", "fitness centre", "fitness center", "sports_hall",
  "sports hall", "bowling_alley", "bowling alley", "covered=yes", "indoor=yes",
  "swimming_pool", // an OSM building=... pool tag; open lidos are rare here
];

export const ENV_BASE_FOR_ACTIVITY = ACTIVITY_ENV;

export function environmentForActivity(
  activityId: string | null | undefined,
  providerTypes: readonly string[] | null | undefined,
): Environment {
  const base: Environment = ACTIVITY_ENV[(activityId || "").trim().toLowerCase()] ?? "uncertain";
  const types = (providerTypes ?? []).map((t) => String(t).toLowerCase());
  const hasOutdoor = types.some((t) => OUTDOOR_TYPE_HINTS.some((h) => t.includes(h)));
  const hasIndoor = types.some((t) => INDOOR_TYPE_HINTS.some((h) => t.includes(h)));

  if (hasIndoor && !hasOutdoor) return "indoor";
  if (hasOutdoor && !hasIndoor) return base === "indoor" ? "uncertain" : "outdoor";
  // no signal, or conflicting signals -> keep the reviewed base (never invent "outdoor")
  return base;
}

export const ENVIRONMENT_LABEL: Record<Environment, string> = {
  indoor: "Indoor",
  outdoor: "Outdoor",
  uncertain: "Not sure",
};

export const ENVIRONMENT_HINT: Record<Environment, string> = {
  indoor: "No weather warnings.",
  outdoor: "May show a weather warning near the time.",
  uncertain: "No weather warnings unless you set this to Outdoor.",
};
