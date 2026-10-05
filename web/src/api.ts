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

export interface Status {
  run_id: string;
  model: string;
  model_version: string;
  started_at: string;
  updated_at: string;
  heartbeat_at: string | null;
  last_bar_ts: number;
  equity: number;
  cash: number;
  initial_equity: number;
  total_return: number;
  drawdown: number;
  trades: number;
  realized_pnl: number;
  win_rate: number | null;
  risk: { day_start_equity: number; day_return: number; consecutive_losses: number; cooldown_until_ts: number };
  positions: Position[];
  pending: { symbol: string; kind: string; reason: string; size_frac: number }[];
}

export interface EquityPoint {
  bar_ts: number;
  equity: number;
  cash: number;
  exposure: number;
  mode: string;
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
}

export interface Decision {
  id: number;
  symbol: string;
  bar_ts: number;
  model_version: string;
  probs: Record<string, Record<string, number>>;
  abstain: number;
  abstain_reason: string | null;
  cost_usd: number | null;
  policy_action: string | null;
  policy_reason: string | null;
}

export interface Config {
  exchange: string;
  symbols: string[];
  timeframe: string;
  model: string;
  horizon_bars: number;
  flat_band_pct: number;
  sizing: Record<string, number | string | null>;
}

export interface BacktestRow {
  run_id: string;
  model: string;
  start: string;
  end: string;
  total_return: number;
  max_drawdown: number;
  sharpe: number | null;
  trades: number;
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

export const getForwardSummary = () => get<ForwardSummary>("/forward/summary");
export const getForwardRows = (params: { symbol?: string; limit?: number } = {}) =>
  get<ForwardRow[]>("/forward/rows", params);

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
