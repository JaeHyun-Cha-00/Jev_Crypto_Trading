export const fmtMoney = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined
    ? "n/a"
    : v.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });

export const fmtPct = (v: number | null | undefined, signed = true) =>
  v === null || v === undefined ? "n/a" : `${signed && v > 0 ? "+" : ""}${(v * 100).toFixed(2)}%`;

export const fmtPrice = (v: number | null | undefined) =>
  v === null || v === undefined ? "n/a" : v >= 100 ? fmtMoney(v, 2) : v.toPrecision(5);

const pad = (n: number) => String(n).padStart(2, "0");

/** The viewer's local time, as YYYY-MM-DD HH:mm. The store and API stay in UTC. */
export const fmtTime = (ms: number) => {
  const d = new Date(ms);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
};

/** Short name of the viewer's time zone right now, e.g. "PDT"; falls back to the UTC offset. */
export const TZ = (() => {
  try {
    const part = new Intl.DateTimeFormat(undefined, { timeZoneName: "short" })
      .formatToParts(new Date())
      .find((p) => p.type === "timeZoneName");
    if (part) return part.value;
  } catch {
    /* no Intl */
  }
  const off = -new Date().getTimezoneOffset();
  return `UTC${off >= 0 ? "+" : "-"}${pad(Math.floor(Math.abs(off) / 60))}:${pad(Math.abs(off) % 60)}`;
})();

/** Full zone name for the footer, e.g. "America/Los_Angeles". */
export const TZ_NAME = (() => {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone;
  } catch {
    return TZ;
  }
})();

export function ago(iso: string | null): { text: string; minutes: number } {
  if (!iso) return { text: "never", minutes: Infinity };
  const minutes = (Date.now() - new Date(iso).getTime()) / 60_000;
  if (minutes < 1) return { text: "just now", minutes };
  if (minutes < 120) return { text: `${Math.round(minutes)} min ago`, minutes };
  return { text: `${(minutes / 60).toFixed(1)} h ago`, minutes };
}

const compact = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 2 });

/** 1234567 -> "1.23M" */
export const fmtCompact = (v: number | null | undefined) => (v === null || v === undefined ? "n/a" : compact.format(v));

/** Price with a $ and enough digits for sub-cent coins. */
export const fmtUsd = (v: number | null | undefined) =>
  v === null || v === undefined ? "n/a" : `$${v >= 1 ? fmtMoney(v, v >= 100_000 ? 0 : 2) : v.toPrecision(4)}`;

/** Local wall-clock time with seconds, for the trade tape. */
export const fmtClock = (ms: number) => {
  const d = new Date(ms);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
};

const sig = (v: number, digits: number) => v.toLocaleString(undefined, { maximumSignificantDigits: digits });

/** Signed price change, no exponents for sub-cent coins: "+159.29", "-0.0049", "+0.0000001". */
export const fmtDelta = (v: number | null | undefined) =>
  v === null || v === undefined ? "n/a" : `${v > 0 ? "+" : v < 0 ? "-" : ""}${Math.abs(v) >= 1 ? fmtMoney(Math.abs(v), 2) : sig(Math.abs(v), 3)}`;

/** A trade or book size: compact when large, three significant digits when small. */
export const fmtSize = (v: number) => (v >= 1000 ? fmtCompact(v) : sig(v, 3));
