// Typed client for the read-only API (src/jevtrade/api/app.py). GET only.

export interface Position {
  symbol: string;
  qty: number;
  entry_price: number;
  entry_ts: number;
  stop_price: number;
  mark: number;
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

export interface Config {
  exchange: string;
  symbols: string[];
  timeframe: string;
  horizon_bars: number;
  flat_band_pct: number;
  sizing: Record<string, number | string | null>;
}

// Jev forward log (the collect workflow's data-log branch): /api/forward/*

export type Direction = "up" | "flat" | "down";

export interface ForwardMetrics {
  decisions: number;
  answered: number;
  abstain: number;
  error: number;
  scored: number;
  pending: number;
  hits: number;
  hit_rate: number | null;
  realized: Record<Direction, number>;
  baselines: {
    majority: { label: Direction | null; hit_rate: number | null };
    always_flat: { label: "flat"; hit_rate: number | null };
  };
  /** rows: predicted, columns: realized, both in `labels` order */
  confusion: { labels: Direction[]; matrix: number[][] };
  calibration: { lo: number; hi: number; n: number; mean_confidence: number | null; hit_rate: number | null }[];
  direction_scores: { n: number; brier: number | null; log_loss: number | null; brier_base_rate: number | null };
  adverse_move_scores: {
    n: number;
    brier: number | null;
    log_loss: number | null;
    base_rate: number | null;
    brier_base_rate: number | null;
  };
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
  direction: Direction;
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
  predicted: Direction | null;
  confidence: number | null;
  regime: string | null;
  /** adverse_move Noul: P(yes) */
  p_adverse: number | null;
  outcome: ForwardOutcome | null;
  hit: boolean | null;
}

/** Jev's simulated account: the policy and simulator replayed over the logged answers (/api/forward/paper). */
export interface JevAction {
  symbol: string;
  bar_ts: number;
  close: number;
  status: "answered" | "abstain" | "error" | null;
  p_up: number;
  p_down: number;
  action: "enter" | "exit" | "hold" | "skip";
  reason: string;
  size_frac: number;
}

export interface JevPaper {
  initial_equity: number;
  fee_bps: number;
  slippage_bps: number;
  /** per-side spread from the mid, bps: deep coins pay min, the thinnest max */
  spread_bps: { min: number; max: number };
  /** "robinhood": spreads come from Robinhood quotes the collect run logged; "estimate": the configured model */
  spread_source: "robinhood" | "estimate";
  /** coins with at least one logged Robinhood quote */
  quoted_symbols: number;
  /** a buy is at most this share of the coin's median hourly dollar volume; null = no cap */
  max_volume_frac: number | null;
  policy: {
    entry_threshold: number; min_edge: number; exit_threshold: number; exit_min_edge: number;
    stop_loss_pct: number; max_holding_bars: number | null;
  };
  /** skill gate: buys only while Jev's recent buy signals paid; null = no gate */
  gate: {
    lookback_hours: number; min_signals: number; open: boolean; signals: number;
    /** mean return of those signals after a round trip of costs, and vs the average coin */
    avg_net: number | null; avg_excess: number | null; reason: string | null;
  } | null;
  equity: number;
  cash: number;
  total_return: number;
  drawdown: number;
  last_bar_ts: number | null;
  trades: Trade[];
  positions: Position[];
  /** fill_after: when the run that queued it happened (UTC ms); it fills once that hour is logged */
  pending: { symbol: string; kind: string; reason: string; size_frac: number; ref_close: number; fill_after: number | null }[];
  curve: CurvePoint[];
  /** newest first, capped by ?actions= */
  actions: JevAction[];
  /** every buy, newest first */
  buys: JevAction[];
  /** hourly calls replayed, all symbols */
  calls: number;
  counts: Partial<Record<JevAction["action"], number>>;
  per_symbol: Record<string, { trades: number; pnl: number; buys: number }>;
  /** first candle the portfolio counts (forward_log.start, UTC ms); earlier calls trade nothing */
  tracking_since: number | null;
}

export const getForwardSummary = () => get<ForwardSummary>("/forward/summary");
export const getForwardRows = (params: { symbol?: string; limit?: number } = {}) =>
  get<ForwardRow[]>("/forward/rows", params);
export const getJevPaper = (actions = 300) => get<JevPaper>("/forward/paper", { actions });

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
