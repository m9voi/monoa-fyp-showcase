// Shared CoreOS API model types (Phase 2 — replaces `any` across the app).

export interface AuthUser {
  id: string;
  email: string;
  name: string;
}

export interface Transaction {
  id: string;
  amount: number;
  amount_sen?: number;
  category: string;
  date: string;
  type: "expense" | "income";
  description?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface CategorySlice {
  category: string;
  amount: number;
  amount_sen?: number;
  pct: number;
}

export interface FinanceSnapshot {
  currency: string;
  period_label: string;
  starting_balance: number;
  total_income: number;
  total_expenses: number;
  available_balance: number;
  daily_budget: number;
  weekly_budget: number;
  monthly_budget: number;
  today_spending: number;
  week_spending: number;
  month_spending: number;
  daily_remaining: number;
  weekly_remaining: number;
  monthly_remaining: number;
  daily_used_pct: number;
  weekly_used_pct: number;
  monthly_used_pct: number;
  daily_over_budget: boolean;
  weekly_over_budget: boolean;
  monthly_over_budget: boolean;
  category_breakdown: CategorySlice[];
}

export interface FinanceMeta {
  categories: string[];
  types: string[];
}

export interface Recommendation {
  id: string;
  user_id?: string;
  module: string;
  type: string;
  message: string;
  status: string;
  target_id?: string | null;
  suggested_action?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface FinanceData {
  transactions: Transaction[];
  active_recommendations: Recommendation[];
  snapshot: FinanceSnapshot;
}

export interface FinanceSettings {
  starting_balance: number;
  weekly_budget: number;
  monthly_budget: number;
  daily_budget: number;
}

export interface MutationResult {
  status: string;
  transaction?: Transaction;
  snapshot?: FinanceSnapshot;
}

export interface TransactionHistory {
  transactions: Transaction[];
}

// Phase 5G — the STABLE, provider-neutral identity of a real place linked to an
// event (Map / Activity -> Schedule). Only identity + a small snapshot: never
// volatile facts (rating / open-now / today's hours) — those are re-fetched live
// via POST /api/places/resolve when the place is viewed.
export interface EventPlaceRef {
  provider: string;              // "google" | "osm" | future
  provider_place_id: string;     // the provider's stable id
  place_id: string;              // "<provider>:<provider_place_id>"
  name: string;
  address: string | null;
  latitude: number;
  longitude: number;
}

// A stored CoreOS event (the row, not an occurrence).
export interface CoreEvent {
  id: string;
  title: string;
  location?: string | null;
  start_time: string;
  end_time?: string | null;
  notes?: string | null;
  all_day: boolean;
  status: "confirmed" | "cancelled";
  source_type: string;
  external_id?: string | null;
  recurrence: "none" | "daily" | "weekly" | "weekdays";
  recurrence_until?: string | null;
  related_task_id?: string | null;
  place?: EventPlaceRef | null;  // Phase 5G: linked real place, or null
  environment?: "indoor" | "outdoor" | "uncertain";  // B5
  created_at?: string | null;
  updated_at?: string | null;
}

// A concrete occurrence of an event within a query window (recurrence-expanded).
export interface EventOccurrence {
  id: string;
  title: string;
  location?: string | null;
  notes?: string | null;
  all_day: boolean;
  status: "confirmed" | "cancelled";
  source_type: string;
  external_id?: string | null;
  related_task_id?: string | null;
  recurrence: string;
  recurrence_until?: string | null;
  start: string;
  end: string;
  occurrence_date: string;
  is_recurring_instance: boolean;
  series_start: string;
  place?: EventPlaceRef | null;  // Phase 5G: linked real place, or null
  environment?: "indoor" | "outdoor" | "uncertain";  // B5
}

// ---- B5: contextual weather-aware schedule conflicts ----
export interface WeatherConflict {
  event_id: string;
  fingerprint: string;
  hazard_type: string;
  severity_tier: number;
  headline: string;
  // B6: "occurrence" when this is one occurrence of a recurring outdoor series.
  scope?: "series" | "occurrence";
  occurrence_date?: string | null;
  occurrence_label?: string | null;
  event: { id: string; title?: string | null; start: string; end: string };
  forecast: { provider?: string | null; fetched_at?: string | null; stale: boolean };
  decision: "kept" | "moved" | null;
}

export interface WeatherConflictsResponse {
  conflicts: WeatherConflict[];
  unresolved: WeatherConflict[];
  provider_unavailable: boolean;
  attribution: string;
}

export interface WeatherCandidate {
  start: string;
  end: string;
  summary: string;
  basis: string;
}

export interface WeatherAlternativesResponse {
  proposal_id: string | null;
  event: { id: string; title?: string | null; start: string; end: string };
  hazard: { hazard_type: string; severity_tier: number; headline: string };
  candidates: WeatherCandidate[];
  message: string | null;
  scope?: "series" | "occurrence";
  occurrence_date?: string | null;
}

export interface WeatherDecisionResponse {
  status: "kept" | "moved" | "cancelled";
  decision_id?: string;
  decision?: "kept" | "moved";
  already: boolean;
  scope?: "series" | "occurrence";
  occurrence_date?: string | null;
  from?: { start: string; end: string } | null;
  resolution?: { event_id: string; start: string; end: string; occurrence_date?: string | null } | null;
  event?: CoreEvent | null;
}

// ---- Phase 5G: resolve a linked place on Schedule -> Map "View place" ----
export interface PlaceResolveResponse {
  status: "LIVE" | "SNAPSHOT";
  reason:
    | "NO_LIVE_PROVIDER"
    | "PROVIDER_NOT_CONFIGURED"
    | "PROVIDER_NO_MATCH"
    | PlacesErrorKind
    | null;
  provider: string;
  attribution: string;
  // LIVE -> a full CorePlace with fresh volatile facts.
  // SNAPSHOT -> identity + name/address/coords only; volatile fields are null.
  place: CorePlace | (Partial<CorePlace> & Pick<CorePlace,
    "place_id" | "provider" | "provider_place_id" | "name" | "latitude" | "longitude" | "maps_url">);
}

export interface CoreTask {
  id: string;
  title: string;
  notes?: string | null;
  is_completed: boolean;
  due_date?: string | null;
  planned_at?: string | null;
  scheduled_end?: string | null;   // B7: end of an optional task work window
  reminder_at?: string | null;
  reminder_status?: string | null; // NONE | PENDING | DUE | ACKNOWLEDGED (B11.1)
  completed_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

// ---- B11.1: in-app DUE reminder delivery ----
export interface DueReminder {
  task_id: string;
  title: string;
  reminder_at: string;
  due_date?: string | null;
}

export interface DueRemindersResponse {
  reminders: DueReminder[];
  // false when the account's in-app reminders preference is OFF (data untouched)
  notifications_enabled: boolean;
}

// ---- B7: task work window <-> event occurrence overlaps ----
export interface TaskConflict {
  fingerprint: string;
  conflict_type: "task_event_overlap";
  scope: "series" | "occurrence";
  occurrence_date?: string | null;
  occurrence_label?: string | null;
  explanation: string;
  task: { id: string; title?: string | null; start: string; end: string; due_date?: string | null };
  event: { id: string; title?: string | null; start: string; end: string; recurring: boolean };
  proposal:
    | { task_id: string; current_start: string; current_end: string; start: string; end: string; summary: string }
    | null;
  decision: "kept" | "moved" | null;
}

export interface TaskCandidate {
  start: string;
  end: string;
  summary: string;
}

export interface TaskAlternativesResponse {
  proposal_id: string | null;
  task: { id: string; title?: string | null; start: string; end: string; due_date?: string | null };
  candidates: TaskCandidate[];
  message: string | null;
  scope: "series" | "occurrence";
  occurrence_date?: string | null;
}

export interface TaskDecisionResponse {
  status: "kept" | "moved" | "cancelled";
  decision_id?: string;
  decision?: "kept" | "moved";
  conflict_type?: "task_event_overlap";
  scope?: "series" | "occurrence";
  occurrence_date?: string | null;
  already: boolean;
  resolution?: { task_id: string; start: string; end: string } | null;
  task?: CoreTask | null;
}

export interface ConflictProposal {
  event_id: string;
  event_title?: string | null;
  current_start: string;
  current_end: string;
  start: string;
  end: string;
  // B6: set when the proposal moves ONE occurrence of a recurring series.
  is_recurring_instance?: boolean;
  occurrence_date?: string | null;
  scope?: "series" | "occurrence";
}

export interface ScheduleConflict {
  event_ids: string[];
  a: EventOccurrence;
  b: EventOccurrence;
  // B3: conflict-decision enrichment (present on GET /api/schedule[/day|/month]).
  conflict_type?: "event_overlap" | "recurring_overlap";
  // B6: "occurrence" -> the decision changes only the named occurrence.
  scope?: "series" | "occurrence";
  occurrence_date?: string | null;
  occurrence_label?: string | null;
  decidable?: boolean;
  fingerprint?: string | null;
  explanation?: string;
  proposal?: ConflictProposal | null;
  decision?: "kept" | "accepted" | null;
}

export interface ScheduleConflictDecisionResponse {
  status: "kept" | "accepted";
  decision_id: string;
  decision: "kept" | "accepted";
  conflict_type: string;
  scope?: "series" | "occurrence";
  occurrence_date?: string | null;
  already: boolean;
  resolution?: { event_id: string; start: string; end: string; occurrence_date?: string | null } | null;
  event?: CoreEvent | null;
}

export interface ScheduleData {
  range: { from: string; to: string };
  events: EventOccurrence[];
  all_events: CoreEvent[];
  tasks: CoreTask[];
  conflicts: ScheduleConflict[];
  unresolved_conflicts?: ScheduleConflict[];
  task_conflicts?: TaskConflict[];
  unresolved_task_conflicts?: TaskConflict[];
  recommendations: Recommendation[];
  timezone: string;
}

export interface MonthData {
  year: number;
  month: number;
  range: { from: string; to: string };
  day_counts: Record<string, { events: number; tasks: number }>;
  conflicts: ScheduleConflict[];
}

export interface ActivityData {
  active_recommendations: Recommendation[];
}

export type DashboardSourceStatus = "ok" | "error";

export interface DashboardFinance {
  currency: string;
  today_spending: number;
  daily_budget: number;
  daily_used_pct: number;
  daily_over_budget: boolean;
}

export interface DashboardNextEvent {
  title: string;
  start: string;
}

export interface DashboardPriorityCard {
  rank: number;
  kind: string;
  message: string;
}

export interface DashboardData {
  // Preserved for backward compatibility: total spending, the single next
  // item (event outranks task), and the plain-language fallback summary.
  total_spending: number | null;
  next_task: string;
  hero_insight: string;

  date: string;
  schedule_status: DashboardSourceStatus;
  schedule_error: string | null;
  finance_status: DashboardSourceStatus;
  finance_error: string | null;
  activity_status: DashboardSourceStatus;
  activity_error: string | null;

  finance: DashboardFinance | null;
  next_event_today: DashboardNextEvent | null;
  events_remaining_today: number | null;
  open_tasks_today: number | null;
  unresolved_conflicts_today: number | null;
  activity: { active_recommendations: Recommendation[] } | null;
  priority: DashboardPriorityCard[];
}

export interface MapLocation {
  id?: string | number;
  name: string;
  activity?: string | null;
  latitude?: number | null;
  longitude?: number | null;
  lat?: number | null;
  lng?: number | null;
  address?: string | null;
  distance?: number | null;
  rationale?: string | null;
}

export interface MapLocationsResponse {
  locations: MapLocation[];
  message?: string | null;
}

// Response of POST /api/location/validate (Phase 5B). Server-authoritative,
// stateless coordinate validation — echoes the normalised values back.
export interface LocationValidateResponse {
  status: "VALID";
  latitude: number;
  longitude: number;
  accuracy_m: number | null;
  timestamp_ms: number | null;
}

// ---- Verified places (Phase 5C) -----------------------------------------
export type PlacesSearchStatus = "OK" | "NO_RESULTS" | "PROVIDER_ERROR";
export type PlacesErrorKind =
  | "PROVIDER_NOT_CONFIGURED"
  | "PROVIDER_UNAVAILABLE"
  | "PROVIDER_TIMEOUT"
  | "PROVIDER_RATE_LIMITED"
  | "INVALID_PROVIDER_RESPONSE";

// Explicit per-category provenance. `identity.verified` is the ONLY "verified"
// flag — it never implies verified hours / ratings / operational status.
export interface PlaceEvidence {
  identity: { source: string; verified: boolean };
  operational: { source: string | null; status: string | null };
  hours: { source: string | null; has_hours: boolean; open_now: boolean | null };
  quality: { source: string | null; rating: number | null; rating_count: number | null };
  contact: { source: string | null; website: string | null; phone: string | null };
}

export interface CorePlace {
  place_id: string;
  provider: string;
  provider_place_id: string;
  name: string;
  latitude: number;
  longitude: number;
  address: string | null;
  distance_m: number;
  distance_kind: "straight_line";
  activity: string;
  activity_match: "exact" | "broad";
  provider_types: string[];
  rating: number | null;
  rating_count: number | null;
  opening_hours: string | string[] | null;
  open_now: boolean | null;
  business_status: string | null;
  website: string | null;
  phone: string | null;
  maps_url: string;
  evidence: PlaceEvidence;
}

export interface PlacesSearchResponse {
  status: PlacesSearchStatus;
  provider: string;
  activity: string;
  radius_m: number;
  places: CorePlace[];
  attribution: string;
  error: PlacesErrorKind | null;
}

// ---- Geocoding (Phase 5D — typed area -> explicit search origin) --------
export interface GeoResult {
  id: string;
  label: string;
  latitude: number;
  longitude: number;
  provider: string;
  result_type: string | null;
}

export interface GeoSearchResponse {
  status: PlacesSearchStatus; // "OK" | "NO_RESULTS" | "PROVIDER_ERROR"
  provider: string;
  query: string;
  results: GeoResult[];
  attribution: string;
  error: PlacesErrorKind | null;
}

// ---- Activity Engine (Phase 5E — activity intent -> ranked real places) ----
export type ActivityRelevanceClass = "strong" | "likely" | "possible";
export type ActivityEvidenceTier = "strong" | "moderate" | "weak" | "none";

export interface ActivityRelevance {
  class: ActivityRelevanceClass;
  score: number;                 // 0..1, deterministic
  tier: ActivityEvidenceTier;
  reason: string;                // truthful, evidence-grounded (not premium copy)
  signals: string[];
  conflicts: string[];
  is_public_geography: boolean;
}

// a CorePlace with the engine's per-activity assessment attached
export type ActivityCandidate = CorePlace & { activity_relevance: ActivityRelevance };

export interface ActivitySearchResponse {
  status: PlacesSearchStatus;    // "OK" | "NO_RESULTS" | "PROVIDER_ERROR"
  activity_id: string;
  provider: string;
  origin_source: "device" | "manual";
  distance_confident: boolean;   // false when a device fix is too coarse (Phase 5D tier)
  candidates: ActivityCandidate[];
  radius_m: number;              // B9: radius the adaptive 8/12/20 km ladder settled on
  attribution: string;
  error: PlacesErrorKind | null;
}

// ---- Weather context (Phase 5F — contextual infrastructure, not a feature) ----
export type WeatherCondition =
  | "clear" | "mostly_clear" | "partly_cloudy" | "cloudy" | "fog"
  | "drizzle" | "rain" | "heavy_rain" | "freezing_rain"
  | "snow" | "heavy_snow" | "thunderstorm" | "unknown";
export type WeatherErrorKind =
  | "WEATHER_TIMEOUT" | "WEATHER_NETWORK" | "WEATHER_UNAVAILABLE"
  | "WEATHER_RATE_LIMITED" | "WEATHER_BAD_RESPONSE" | "WEATHER_NO_FORECAST";

export interface WeatherCurrent {
  temperature_c: number | null;
  apparent_temperature_c: number | null;
  condition: WeatherCondition;
  wmo_code: number | null;
  precipitation_mm: number | null;
  wind_speed_kmh: number | null;
  wind_gusts_kmh: number | null;
  is_day: boolean | null;
  observed_at: string | null;
}

export interface WeatherPrimitives {
  precipitation_risk: "low" | "moderate" | "high" | "unknown";
  storm_risk: "none" | "possible" | "likely" | "unknown";
  heat_context: "normal" | "warm" | "hot" | "extreme" | "unknown";
  cold_context: "normal" | "cold" | "freezing" | "unknown";
  wind_context: "calm" | "breezy" | "windy" | "strong" | "unknown";
  visibility_context: "normal" | "reduced" | "poor" | "unknown";
  outdoor_context: "normal" | "caution" | "poor" | "unknown";
}

export interface WeatherWindow {
  requested_start: string;
  requested_end: string;
  hours_covered: number;
  hours_expected: number;
  partial: boolean;
  temperature_c: number | null;
  temperature_min_c: number | null;
  temperature_max_c: number | null;
  apparent_temperature_c: number | null;
  apparent_min_c: number | null;
  apparent_max_c: number | null;
  precipitation_probability_max: number | null;   // % — a probability, never a certainty
  precipitation_mm_sum: number | null;
  snowfall_mm_sum: number | null;
  wind_speed_max_kmh: number | null;
  wind_gusts_max_kmh: number | null;
  visibility_min_m: number | null;
  conditions: WeatherCondition[];
  dominant_condition: WeatherCondition;
  thunderstorm: boolean;
  freezing: boolean;
  snow: boolean;
  primitives: WeatherPrimitives;
  phrases: string[];              // factual, non-alarmist ("rain likely", "high heat")
}

export interface WeatherContextResponse {
  status: "OK" | "PROVIDER_ERROR";
  provider: string;
  attribution: string;
  timezone: string;              // TARGET-location IANA timezone
  timezone_abbreviation: string;
  utc_offset_seconds: number;
  fetched_at: string;            // forecast freshness (ISO-8601 UTC)
  stale: boolean;
  current: WeatherCurrent | null;
  window: WeatherWindow | null;
  window_error: WeatherErrorKind | null;  // "WEATHER_NO_FORECAST" when a window was requested but is out of horizon
  error: WeatherErrorKind | null;
}

export interface UserPreferences {
  notifications?: number | boolean;
  location_services?: number | boolean;
  dark_mode?: number | boolean;
  appearance_mode?: "system" | "light" | "dark" | null;
}

export interface ActionOutcome {
  status: string;
  message: string;
  data?: Record<string, unknown> | null;
}

// ---- Mo — human-in-the-loop (B2) ----------------------------------------
export interface MoSuggestion {
  key: string;
  label: string;
  route: string;
}

// B8 — a curated, server-owned MUTATING suggestion. The client only ever echoes
// `token` back (an opaque HMAC-signed blob); it never authors the task, times,
// or action type. Selecting one builds a normal B2 proposal — nothing mutates
// until Confirm.
export interface MoMutatingSuggestion {
  key: string;
  label: string;
  kind: "schedule_task_block" | "complete_task";
  token: string;
  preview?: {
    title?: string;
    start?: string;
    end?: string;
    summary?: string;
  };
}

export interface MoProposal {
  proposal_id: string;
  // "stale" (B8.1) — the suggestion no longer matches live data; terminal,
  // not user-caused, not retryable. Distinct from "cancelled".
  status: "pending" | "executing" | "executed" | "cancelled" | "expired" | "failed" | "stale";
  action_type:
    | "create_transaction"
    | "create_task"
    | "create_event"
    | "schedule_task_block"
    | "complete_task"
    | "update_task"
    | "delete_task"
    | "update_event"
    | "delete_event"
    | "batch_operations";
  result_kind?: "transaction" | "task" | "event" | "batch" | null;
  result_ref?: string | null;
  summary: string;
  effect: string;
  display: Record<string, unknown>;
  expires_at: string;
  confirmed_at?: string | null;
  client_token?: string | null;
  exec_message?: string | null;
  failure_reason?: string | null;
}

/**
 * POST /api/process-input
 *
 * The "executed" | "failed" | "stale" | "cancelled" kinds are only ever
 * returned when the submitted text was a typed "yes"/"no" fallback for the
 * user's single active pending proposal (Section 9/10 — confirmation vs
 * clarification must be structurally distinct; a typed "yes" may confirm
 * EXACTLY that one proposal and nothing else). They mirror
 * MoConfirmResponse/MoCancelResponse exactly on purpose — the OmniBar routes
 * them through the same view-building logic as a real Confirm/Cancel.
 */
export type MoUnderstandResponse =
  | {
      kind: "answer";
      message: string;
      suggested_actions: MoSuggestion[];
      mutating_suggestions?: MoMutatingSuggestion[];
    }
  | {
      kind: "clarification_required";
      message: string;
      suggested_actions: MoSuggestion[];
      mutating_suggestions?: MoMutatingSuggestion[];
    }
  | {
      kind: "unsupported";
      message: string;
      suggested_actions: MoSuggestion[];
      mutating_suggestions?: MoMutatingSuggestion[];
    }
  | {
      kind: "error";
      message: string;
      retryable?: boolean;
      suggested_actions: MoSuggestion[];
      mutating_suggestions?: MoMutatingSuggestion[];
    }
  | {
      kind: "proposal_required";
      proposal: MoProposal;
      message: string;
      suggested_actions: MoSuggestion[];
    }
  | { kind: "executed"; proposal: MoProposal; message: string; suggested_actions: MoSuggestion[] }
  | { kind: "failed"; proposal: MoProposal; message: string; retryable?: boolean; suggested_actions: MoSuggestion[] }
  | { kind: "stale"; proposal: MoProposal; message: string; suggested_actions: MoSuggestion[] }
  | { kind: "cancelled"; proposal: MoProposal };

/** POST /api/process-input/suggested/{token} — mutating-suggestion -> proposal. */
export type MoPrepareResponse =
  | {
      kind: "proposal_required";
      proposal: MoProposal;
      message: string;
      suggested_actions: MoSuggestion[];
    };

/** POST /api/process-input/{id}/confirm */
export type MoConfirmResponse =
  | { kind: "executed"; proposal: MoProposal; message: string; suggested_actions: MoSuggestion[] }
  | { kind: "failed"; proposal: MoProposal; message: string; retryable?: boolean; suggested_actions: MoSuggestion[] }
  | { kind: "stale"; proposal: MoProposal; message: string; suggested_actions: MoSuggestion[] };

/** POST /api/process-input/{id}/cancel */
export interface MoCancelResponse {
  kind: "cancelled";
  proposal: MoProposal;
}

/** GET /api/process-input/pending */
export interface MoPendingResponse {
  proposal: MoProposal | null;
}
