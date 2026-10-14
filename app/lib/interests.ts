// Who Am I — Product Completion Batch 02. A bounded, curated interest
// vocabulary per category (never free text) — mirrors the backend's
// INTEREST_OPTIONS in main.py exactly. Keep the two in sync by hand; a
// mismatch only ever shows up as a value the backend rejects with 422; it
// can never silently persist something the user didn't actually choose.

export type InterestCategory = "game" | "sport" | "movie_tv" | "general";

export const INTEREST_CATEGORIES: InterestCategory[] = ["game", "sport", "movie_tv", "general"];

export const INTEREST_CATEGORY_LABELS: Record<InterestCategory, string> = {
  game: "Games",
  sport: "Sports",
  movie_tv: "Movies & TV",
  general: "General",
};

export const INTEREST_OPTIONS: Record<InterestCategory, string[]> = {
  game: [
    "Action", "RPG", "Strategy", "Shooter", "Sports Games",
    "Puzzle", "Simulation", "Adventure", "Racing", "Multiplayer/Online",
  ],
  sport: [
    "Football", "Basketball", "Badminton", "Tennis", "Running",
    "Swimming", "Cycling", "Gym/Fitness", "Esports", "Martial Arts",
  ],
  movie_tv: [
    "Action", "Comedy", "Drama", "Sci-Fi", "Anime",
    "Documentary", "Horror", "Thriller", "Romance", "K-Drama",
  ],
  general: [
    "Fitness / Gym", "Running", "Studying", "Technology", "AI",
    "Travel", "Food", "Music", "Gaming culture", "Movies / entertainment",
    "Outdoor activities",
  ],
};

export type UserInterests = Record<InterestCategory, string[]>;

export const EMPTY_INTERESTS: UserInterests = { game: [], sport: [], movie_tv: [], general: [] };

/** Total interests saved across every category — for the Who Am I overview. */
export function totalInterestCount(interests: UserInterests | null | undefined): number {
  if (!interests) return 0;
  return INTEREST_CATEGORIES.reduce((sum, cat) => sum + (interests[cat]?.length ?? 0), 0);
}
