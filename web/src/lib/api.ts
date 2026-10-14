// Typed client for the read-only API (src/jevtrade/api/app.py). GET only.

export interface Position {
  symbol: string;
  qty: number;
  entry_price: number;
  entry_ts: number;
  mark: number;
  hours_held: number;
  notional: number;
  unrealized_pnl: number;
  entry_reason: string;
}

export interface CurvePoint {
  bar_ts: number;
  equity: number;
  cash?: number;
}

export interface Trade {
  symbol: string;
  entry_ts: number;
  entry_price: number;
  exit_ts: number;
  exit_price: number;
  qty: number;
  fees: number;
  pnl: number;
  ret: number;
  bars_held: number;
  exit_reason: string;
  entry_reason: string;
}

export interface Question {
  id: string;
  instructions: string;
  /** option id -> criterion text, in the order Jev sees them */
  options: Record<string, string>;
  type: "choice" | "noul";
}

export interface Config {
  exchange: string;
  symbols: string[];
  timeframe: string;
  jev_model: string;
  /** hours after a call that it is scored */
  horizon_bars: number;
  prompt_version: string;
  mandate: string;
  questions: { entry: Question; manage: Question };
  initial_equity: number;
  sizes: Record<BuySize, number>;
  /** one fill's cost as the mandate quotes it, a fraction */
  side_cost: number;
}

// Jev forward log (the collect workflow's data-log branch): /api/forward/*

export type BuySize = "small" | "normal" | "large";
export type EntryChoice = "buy_small" | "buy_normal" | "buy_large" | "wait";
export type ManageChoice = "hold" | "sell";
export type Choice = EntryChoice | ManageChoice;

export const CHOICE_LABEL: Record<Choice, string> = {
  buy_small: "Buy small", buy_normal: "Buy", buy_large: "Buy large", wait: "Wait", hold: "Hold", sell: "Sell",
};

/** Calls grouped by what Jev chose, scored over the horizon (returns are fractions). */
export interface ChoiceStats {
  n: number;
  mean_ret: number | null;
  /** share whose price rose */
  up_rate: number | null;
  /** share where the choice beat its alternative */
  right_rate: number | null;
}

export interface ForwardMetrics {
  decisions: number;
  answered: number;
  abstain: number;
  error: number;
  scored: number;
  pending: number;
  /** candle of the oldest call still waiting for its outcome (UTC ms); null when none */
  oldest_pending_ts: number | null;
  right: number;
  right_rate: number | null;
  /** a buy is right when the price beat this round trip of costs (a fraction) */
  round_trip: number;
  /** every scored entry call, bought or not: the market Jev chose from */
  entry_all: ChoiceStats;
  entry: Record<EntryChoice, ChoiceStats>;
  buys: ChoiceStats;
  manage: Record<ManageChoice, ChoiceStats>;
  /** entry calls by p(buy), all sizes together */
  calibration: (ChoiceStats & { lo: number; hi: number })[];
  cost_usd: number;
}

export interface ForwardSummary {
  source: "github" | "local" | "off";
  location: string | null;
  fetched_at: number | null;
  last_called_at: string | null;
  last_candle_ts: number | null;
  files: number;
  error: string | null;
  overall: ForwardMetrics;
  per_symbol: Record<string, ForwardMetrics>;
}

export interface ForwardOutcome {
  horizon_bars: number;
  close_at_horizon: number;
  ret_pct: number;
  direction: "up" | "flat" | "down";
  min_low_pct: number;
  adverse_move: boolean;
}

export interface ForwardRow {
  symbol: string;
  candle_ts: number;
  candle_open: string;
  close: number;
  called_at: string;
  status: "answered" | "abstain" | "error";
  abstain_reason: string | null;
  served_model: string | null;
  latency_ms: number;
  cost_usd: number | null;
  /** entry: the account didn't hold the coin; manage: it did */
  question: "entry" | "manage" | null;
  choice: Choice | null;
  p_choice: number | null;
  /** entry calls: the three buy sizes together */
  p_buy: number | null;
  p_sell: number | null;
  /** the account and position Jev was shown */
  shown: { account: Record<string, number>; position: Record<string, number> | null } | null;
  outcome: ForwardOutcome | null;
  right: boolean | null;
}

export type ActionKind = "buy" | "sell" | "hold" | "wait" | "no_cash" | "none";
export const ACTION_LABEL: Record<ActionKind, string> = {
  buy: "buy", sell: "sell", hold: "hold", wait: "wait", no_cash: "no cash", none: "–",
};

/** One of Jev's calls and what the account did with it (/api/forward/paper). */
export interface JevAction {
  symbol: string;
  bar_ts: number;
  close: number | null;
  status: "answered" | "abstain" | "error" | null;
  question: "entry" | "manage" | null;
  choice: Choice | null;
  /** Jev's probability for each option */
  p: Record<string, number>;
  action: ActionKind;
  reason: string;
  size_frac: number;
}

/** One hour of Jev's calls (/api/forward/paper `hours`): what it chose to buy, what the account did, how the buys did. */
export interface JevHour {
  bar_ts: number;
  /** coins logged this hour and how many Jev answered; 0 when the collector skipped the hour */
  asked: number;
  answered: number;
  /** coins Jev chose to buy, most confident first; ret and net (after a round trip of costs) once the horizon
   * has closed, else null. action: buy, or no_cash when the cash ran out */
  picks: { symbol: string; choice: EntryChoice; p: number; action: ActionKind; ret: number | null; net: number | null }[];
  bought: string[];
  sold: { symbol: string; why: string }[];
  /** the average logged coin's return over the same hours; null until the horizon closes */
  market: number | null;
  /** when this hour's horizon closes (UTC ms) */
  resolves_at: number;
}

export interface JevPaper {
  initial_equity: number;
  fee_bps: number;
  slippage_bps: number;
  /** per-side spread from the mid, bps: deep coins pay min, the thinnest max */
  spread_bps: { min: number; max: number };
  /** share of equity each buy size spends */
  sizes: Record<BuySize, number>;
  side_cost: number;
  prompt_version: string;
  equity: number;
  cash: number;
  total_return: number;
  drawdown: number;
  last_bar_ts: number | null;
  trades: Trade[];
  positions: Position[];
  curve: CurvePoint[];
  /** newest first, capped by ?actions= */
  actions: JevAction[];
  /** every buy, newest first */
  buys: JevAction[];
  /** every hour since tracking started, newest first, capped by ?hours= */
  hours: JevHour[];
  /** hourly calls replayed, all symbols */
  calls: number;
  counts: Partial<Record<ActionKind, number>>;
  per_symbol: Record<string, { trades: number; pnl: number; buys: number }>;
  /** first candle the portfolio counts (forward_log.start, UTC ms); earlier calls trade nothing */
  tracking_since: number | null;
}

export const getForwardSummary = () => get<ForwardSummary>("/forward/summary");
export const getForwardRows = (params: { symbol?: string; limit?: number } = {}) =>
  get<ForwardRow[]>("/forward/rows", params);
export const getJevPaper = (actions = 300, hours = 168) => get<JevPaper>("/forward/paper", { actions, hours });

/** The simulated account at finer steps: each hour's cash and coins valued at Coinbase candle closes. */
export interface JevPaperDetail {
  step_ms: number | null;
  points: { ts: number; equity: number }[];
}
export const getJevPaperDetail = (hours?: number) =>
  get<JevPaperDetail>("/forward/paper/detail", { hours: hours !== undefined && Number.isFinite(hours) ? hours : undefined });

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function get<T>(path: string, params: Record<string, string | number | undefined> = {}): Promise<T> {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined) q.set(k, String(v));
  const res = await fetch(`/api${path}${q.size ? `?${q}` : ""}`, { headers: { Accept: "application/json" } });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

// Live market (/api/market/*): public Coinbase data for the tracked coins.

export interface CoinTicker {
  symbol: string;
  name: string;
  price: number | null;
  open_24h: number | null;
  high_24h: number | null;
  low_24h: number | null;
  change_24h: number | null;
  change_pct_24h: number | null;
  /** base currency */
  volume_24h: number | null;
  volume_usd_24h: number | null;
  volume_30d: number | null;
  /** last 24 hours, 15-minute closes, oldest first; empty until the first refresh */
  spark: number[];
}

export interface Tickers {
  updated_at: number | null;
  spark_updated_at: number | null;
  error: string | null;
  coins: CoinTicker[];
}

export interface Candle {
  /** candle open, UTC ms */
  ts: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface Book {
  symbol: string;
  bids: [number, number][];
  asks: [number, number][];
  mid: number | null;
  spread: number | null;
  spread_pct: number | null;
}

export interface Fill {
  id: number | null;
  time: string;
  price: number;
  size: number;
  /** the taker's side */
  side: "buy" | "sell";
}

export const TIMEFRAMES = [
  { id: "1m", label: "1m", sec: 60 },
  { id: "5m", label: "5m", sec: 300 },
  { id: "15m", label: "15m", sec: 900 },
  { id: "1h", label: "1H", sec: 3600 },
  { id: "6h", label: "6H", sec: 21600 },
  { id: "1d", label: "1D", sec: 86400 },
] as const;
export type Timeframe = (typeof TIMEFRAMES)[number]["id"];

export const getTickers = () => get<Tickers>("/market/tickers");
export const getCandles = (symbol: string, timeframe: Timeframe) => get<Candle[]>("/market/candles", { symbol, timeframe });
export const getBook = (symbol: string, depth = 14) => get<Book>("/market/book", { symbol, depth });
export const getFills = (symbol: string, limit = 40) => get<Fill[]>("/market/trades", { symbol, limit });
