// Date formatting helpers for trade records.
//
// Timestamps are stored in IST (see backend `now_ist()`) and rendered in the
// Asia/Kolkata timezone so an IST trade never shows the wrong calendar date.

const MONTHS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

/**
 * Format a trade timestamp as a readable IST date, e.g. `24 Sep 2026`.
 *
 * Returns the application's missing-value marker (`—`) when the timestamp is
 * absent or unparseable. The stored value is never modified — formatting is
 * display-only.
 */
export function formatTradeDate(value?: string | null): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  try {
    const parts = new Intl.DateTimeFormat("en-GB", {
      day: "2-digit",
      month: "numeric",
      year: "numeric",
      timeZone: "Asia/Kolkata",
    }).formatToParts(d);
    const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
    const month = parseInt(get("month"), 10);
    const day = get("day");
    const year = get("year");
    if (!day || !year || !month) return "—";
    return `${day} ${MONTHS[month - 1]} ${year}`;
  } catch {
    return "—";
  }
}

/**
 * Today's date in the Asia/Kolkata timezone as `YYYY-MM-DD`.
 *
 * Used by the Trade History "Today" filter so the correct INDIAN calendar
 * date is requested regardless of the browser's local timezone. Returns an
 * empty string when the environment cannot format in IST.
 */
export function todayISTDate(): string {
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      timeZone: "Asia/Kolkata",
    }).formatToParts(new Date());
    const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
    const year = get("year");
    const month = get("month");
    const day = get("day");
    if (!year || !month || !day) return "";
    return `${year}-${month}-${day}`;
  } catch {
    return "";
  }
}
