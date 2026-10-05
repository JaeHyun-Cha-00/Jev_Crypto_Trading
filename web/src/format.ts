export const fmtMoney = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined
    ? "n/a"
    : v.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });

export const fmtPct = (v: number | null | undefined, signed = true) =>
  v === null || v === undefined ? "n/a" : `${signed && v > 0 ? "+" : ""}${(v * 100).toFixed(2)}%`;

export const fmtPrice = (v: number) => (v >= 100 ? fmtMoney(v, 2) : v.toPrecision(5));

/** UTC, matching the store and reports. */
export const fmtTime = (ms: number) => new Date(ms).toISOString().slice(0, 16).replace("T", " ");

export function ago(iso: string | null): { text: string; minutes: number } {
  if (!iso) return { text: "never", minutes: Infinity };
  const minutes = (Date.now() - new Date(iso).getTime()) / 60_000;
  if (minutes < 1) return { text: "just now", minutes };
  if (minutes < 120) return { text: `${Math.round(minutes)} min ago`, minutes };
  return { text: `${(minutes / 60).toFixed(1)} h ago`, minutes };
}
