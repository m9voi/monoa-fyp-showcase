// Display + form helpers for the Planning/Schedule UI.
// CoreOS operates in Asia/Kuala_Lumpur (UTC+08:00, no DST). The backend is
// authoritative for all stored timestamps; the frontend only formats/parses.

export const MYT_OFFSET = "+08:00";
export const TZ = "Asia/Kuala_Lumpur";

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString("en-MY", { hour: "numeric", minute: "2-digit", hour12: true, timeZone: TZ });
}

export function fmtDayLabel(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-MY", { weekday: "short", day: "numeric", month: "short", timeZone: TZ });
}

export function fmtLongDate(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString("en-MY", { weekday: "long", day: "numeric", month: "long", year: "numeric", timeZone: TZ });
}

/** The MYT calendar date ('YYYY-MM-DD') that an ISO instant falls on. */
export function mytDateKey(iso: string): string {
  const d = new Date(iso);
  // shift into MYT then read the date parts
  const shifted = new Date(d.getTime() + 8 * 3600 * 1000);
  return shifted.toISOString().slice(0, 10);
}

export function todayKey(): string {
  return mytDateKey(new Date().toISOString());
}

export function ymd(dateKey: string): { y: number; m: number; d: number } {
  const [y, m, d] = dateKey.split("-").map(Number);
  return { y, m, d };
}

/** First day (Mon) .. last day of the 6-row grid covering a given month. */
export function monthGrid(year: number, month1: number): string[][] {
  const first = new Date(Date.UTC(year, month1 - 1, 1));
  // JS getUTCDay: 0=Sun..6=Sat -> we want Monday-first
  const startOffset = (first.getUTCDay() + 6) % 7;
  const gridStart = new Date(first.getTime() - startOffset * 86400000);
  const weeks: string[][] = [];
  for (let w = 0; w < 6; w++) {
    const row: string[] = [];
    for (let d = 0; d < 7; d++) {
      const cur = new Date(gridStart.getTime() + (w * 7 + d) * 86400000);
      row.push(cur.toISOString().slice(0, 10));
    }
    weeks.push(row);
  }
  return weeks;
}

export function weekOf(dateKey: string): string[] {
  const { y, m, d } = ymd(dateKey);
  const dt = new Date(Date.UTC(y, m - 1, d));
  const monOffset = (dt.getUTCDay() + 6) % 7;
  const mon = new Date(dt.getTime() - monOffset * 86400000);
  return Array.from({ length: 7 }, (_, i) => new Date(mon.getTime() + i * 86400000).toISOString().slice(0, 10));
}

export function addMonths(year: number, month1: number, delta: number): { year: number; month: number } {
  const idx = (year * 12 + (month1 - 1)) + delta;
  return { year: Math.floor(idx / 12), month: (idx % 12) + 1 };
}

export function monthName(month1: number): string {
  return ["January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December"][month1 - 1];
}

/** ISO string (with +08:00) -> value for <input type="datetime-local"> (MYT wall time). */
export function isoToLocalInput(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const myt = new Date(d.getTime() + 8 * 3600 * 1000);
  return myt.toISOString().slice(0, 16);
}

/** <input type="datetime-local"> value (interpreted as MYT wall time) -> ISO with +08:00. */
export function localInputToIso(v: string): string {
  if (!v) return "";
  // v is 'YYYY-MM-DDTHH:mm' meaning MYT wall time
  return `${v}:00${MYT_OFFSET}`;
}

export function dateToInput(dateKey: string): string {
  return dateKey;
}
