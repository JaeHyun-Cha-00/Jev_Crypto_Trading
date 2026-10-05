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
