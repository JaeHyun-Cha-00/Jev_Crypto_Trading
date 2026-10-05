import { useCallback, useEffect, useState } from "react";
import { ApiError, get, getForwardRows, getForwardSummary } from "./api";
import type { BacktestRow, Config, Decision, EquityPoint, ForwardRow, ForwardSummary, Status, Trade } from "./api";
import { EquityChart } from "./EquityChart";
import { ForwardLog } from "./ForwardLog";
import { ago, fmtMoney, fmtPct, fmtPrice, fmtTime } from "./format";

const REFRESH_MS = 60_000;
const RANGES = [
  { id: "24h", label: "24 hours", hours: 24 },
  { id: "7d", label: "7 days", hours: 24 * 7 },
  { id: "30d", label: "30 days", hours: 24 * 30 },
  { id: "all", label: "All", hours: Infinity },
] as const;
type RangeId = (typeof RANGES)[number]["id"];

interface Data {
  status: Status | null;
  config: Config | null;
  equity: EquityPoint[];
  trades: Trade[];
  decisions: Decision[];
  reports: string[];
  backtests: BacktestRow[];
}

const empty: Data = { status: null, config: null, equity: [], trades: [], decisions: [], reports: [], backtests: [] };

interface Forward {
  summary: ForwardSummary | null;
  rows: ForwardRow[];
  error: string | null;
}

function Tile({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: "up" | "down" }) {
  return (
    <div className="tile">
      <div className="tile-label">{label}</div>
      <div className={`tile-value ${tone ?? ""}`}>{value}</div>
      {sub && <div className="tile-sub">{sub}</div>}
    </div>
  );
}

function Health({ status }: { status: Status | null }) {
  const hb = ago(status?.heartbeat_at ?? null);
  const ok = hb.minutes < 90;
  return (
    <span className={`health ${ok ? "good" : "warning"}`} role="status">
      <span aria-hidden="true">{ok ? "●" : "▲"}</span> {ok ? "Loop running" : "Loop not seen"} · heartbeat {hb.text}
    </span>
  );
}

function tone(v: number | null | undefined): "up" | "down" | undefined {
  return v === null || v === undefined || v === 0 ? undefined : v > 0 ? "up" : "down";
}

export default function App() {
  const [data, setData] = useState<Data>(empty);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [range, setRange] = useState<RangeId>("7d");
  const [symbol, setSymbol] = useState<string>("");
  const [report, setReport] = useState<{ day: string; markdown: string } | null>(null);
  const [forward, setForward] = useState<Forward>({ summary: null, rows: [], error: null });

  const load = useCallback(async () => {
    setLoading(true);
    // The forward log is independent of the paper account: its failures stay in its own section.
    Promise.all([getForwardSummary(), getForwardRows({ limit: 300 })])
      .then(([summary, rows]) => setForward({ summary, rows, error: null }))
      .catch((e) => setForward((f) => ({ ...f, error: e instanceof Error ? e.message : String(e) })));
    try {
      const [config, equity, trades, decisions, reports, backtests] = await Promise.all([
        get<Config>("/config"),
        get<EquityPoint[]>("/paper/equity", { limit: 100_000 }),
        get<Trade[]>("/paper/trades", { limit: 200 }),
        get<Decision[]>("/decisions", { limit: 60 }),
        get<{ days: string[] }>("/reports"),
        get<BacktestRow[]>("/backtests"),
      ]);
      let status: Status | null = null;
      try {
        status = await get<Status>("/paper/status");
      } catch (e) {
        if (!(e instanceof ApiError && e.status === 404)) throw e;
      }
      setData({ status, config, equity, trades, decisions, reports: reports.days, backtests });
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, REFRESH_MS);
    return () => clearInterval(id);
  }, [load]);

  const openReport = async (day: string) => {
    const r = await get<{ day: string; markdown: string }>(`/reports/${day}`);
    setReport({ day: r.day, markdown: r.markdown });
  };

  const { status, config } = data;
  const hours = RANGES.find((r) => r.id === range)!.hours;
  const cutoff = data.equity.length ? data.equity[data.equity.length - 1].bar_ts - hours * 3_600_000 : 0;
  const equity = data.equity.filter((p) => p.bar_ts >= cutoff);
  const trades = data.trades.filter((t) => !symbol || t.symbol === symbol);
  const decisions = data.decisions.filter((d) => !symbol || d.symbol === symbol);
  const positions = (status?.positions ?? []).filter((p) => !symbol || p.symbol === symbol);

  return (
    <div className={`page ${loading ? "refreshing" : ""}`}>
      <header>
        <div>
          <h1>Jev paper trading</h1>
          <p className="muted">
            {status ? `${status.model} (${status.model_version})` : "Paper run not started"} · {config?.exchange}{" "}
            {config?.timeframe} · simulated fills, never real orders
          </p>
        </div>
        <Health status={status} />
      </header>

      {error && (
        <div className="banner" role="alert">
          <span aria-hidden="true">▲</span> Can't reach the API: {error}. Retrying every minute.
        </div>
      )}

      <div className="filters">
        <div className="seg" role="group" aria-label="Equity range">
          {RANGES.map((r) => (
            <button key={r.id} className={r.id === range ? "on" : ""} onClick={() => setRange(r.id)}>
              {r.label}
            </button>
          ))}
        </div>
        <label>
          Symbol{" "}
          <select value={symbol} onChange={(e) => setSymbol(e.target.value)}>
            <option value="">All</option>
            {config?.symbols.map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
        </label>
        <button className="ghost" onClick={load}>
          Refresh
        </button>
      </div>

      <section className="tiles">
        <Tile label="Equity" value={fmtMoney(status?.equity)} sub={`cash ${fmtMoney(status?.cash)}`} />
        <Tile
          label="Return since start"
          value={fmtPct(status?.total_return)}
          tone={tone(status?.total_return)}
          sub={`from ${fmtMoney(status?.initial_equity, 0)}`}
        />
        <Tile label="Today (UTC)" value={fmtPct(status?.risk.day_return)} tone={tone(status?.risk.day_return)} />
        <Tile label="Drawdown" value={fmtPct(status?.drawdown)} tone={status && status.drawdown < 0 ? "down" : undefined} />
        <Tile
          label="Closed trades"
          value={String(status?.trades ?? 0)}
          sub={`win rate ${status?.win_rate == null ? "n/a" : fmtPct(status.win_rate, false)} · pnl ${fmtMoney(status?.realized_pnl)}`}
        />
      </section>

      <section className="card">
        <h2>Equity</h2>
        <EquityChart points={equity} initial={status?.initial_equity ?? 10_000} />
      </section>

      <div className="two">
        <section className="card">
          <h2>Open positions</h2>
          {positions.length === 0 ? (
            <p className="muted">None.</p>
          ) : (
            <div className="scroll"><table>
              <thead>
                <tr><th>Symbol</th><th className="num">Qty</th><th className="num">Entry</th><th className="num">Stop</th><th className="num">Mark</th><th className="num">Unrealized</th></tr>
              </thead>
              <tbody>
                {positions.map((p) => (
                  <tr key={p.symbol}>
                    <td>{p.symbol}</td>
                    <td className="num">{p.qty.toPrecision(5)}</td>
                    <td className="num">{fmtPrice(p.entry_price)}</td>
                    <td className="num">{fmtPrice(p.stop_price)}</td>
                    <td className="num">{fmtPrice(p.mark)}</td>
                    <td className={`num ${tone(p.unrealized_pnl) ?? ""}`}>{fmtMoney(p.unrealized_pnl)}</td>
                  </tr>
                ))}
              </tbody>
            </table></div>
          )}
          {status?.pending.length ? (
            <p className="muted small">
              Pending at next open: {status.pending.map((p) => `${p.kind} ${p.symbol}`).join(", ")}
            </p>
          ) : null}
        </section>

        <section className="card">
          <h2>Sizing and limits</h2>
          {config && (
            <dl className="kv">
              <dt>Method</dt><dd>{String(config.sizing.sizing_method)}</dd>
              <dt>Stop-loss</dt><dd>{fmtPct(Number(config.sizing.stop_loss_pct), false)}</dd>
              <dt>Position at stop</dt><dd>{fmtPct(Number(config.sizing.position_frac_at_stop), false)} of equity</dd>
              <dt>Max position / gross</dt><dd>{fmtPct(Number(config.sizing.max_position_frac), false)} / {fmtPct(Number(config.sizing.max_gross_exposure), false)}</dd>
              <dt>Max daily loss</dt><dd>{fmtPct(Number(config.sizing.max_daily_loss_pct), false)}</dd>
              <dt>Max hold</dt><dd>{config.sizing.max_holding_bars == null ? "none, exits on the model's signal" : `${config.sizing.max_holding_bars} bars`}</dd>
              <dt>Direction question</dt><dd>±{config.flat_band_pct}% over {config.horizon_bars} bars</dd>
            </dl>
          )}
        </section>
      </div>

      <section className="card">
        <h2>Recent trades</h2>
        {trades.length === 0 ? (
          <p className="muted">No closed trades yet.</p>
        ) : (
          <div className="scroll">
            <table>
              <thead>
                <tr><th>Symbol</th><th>Entry (UTC)</th><th>Exit (UTC)</th><th className="num">Bars</th><th className="num">Return</th><th className="num">PnL</th><th>Exit reason</th></tr>
              </thead>
              <tbody>
                {trades.slice(0, 50).map((t) => (
                  <tr key={`${t.symbol}-${t.entry_ts}`}>
                    <td>{t.symbol}</td>
                    <td>{fmtTime(t.entry_ts)} @ {fmtPrice(t.entry_price)}</td>
                    <td>{fmtTime(t.exit_ts)} @ {fmtPrice(t.exit_price)}</td>
                    <td className="num">{t.bars_held}</td>
                    <td className={`num ${tone(t.ret) ?? ""}`}>{fmtPct(t.ret)}</td>
                    <td className={`num ${tone(t.pnl) ?? ""}`}>{fmtMoney(t.pnl)}</td>
                    <td>{t.exit_reason.split(":")[0]}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="card">
        <h2>Recent decisions</h2>
        {decisions.length === 0 ? (
          <p className="muted">No decisions yet.</p>
        ) : (
          <div className="scroll">
            <table>
              <thead>
                <tr><th>Bar (UTC)</th><th>Symbol</th><th className="num">p(up)</th><th className="num">p(flat)</th><th className="num">p(down)</th><th>Action</th><th>Reason</th></tr>
              </thead>
              <tbody>
                {decisions.map((d) => {
                  const p = d.probs.direction ?? {};
                  return (
                    <tr key={d.id}>
                      <td>{fmtTime(d.bar_ts)}</td>
                      <td>{d.symbol}</td>
                      <td className="num">{d.abstain ? "–" : (p.up ?? 0).toFixed(2)}</td>
                      <td className="num">{d.abstain ? "–" : (p.flat ?? 0).toFixed(2)}</td>
                      <td className="num">{d.abstain ? "–" : (p.down ?? 0).toFixed(2)}</td>
                      <td><span className={`tag ${d.policy_action ?? ""}`}>{d.policy_action ?? "n/a"}</span></td>
                      <td className="reason">{d.abstain ? `abstain: ${d.abstain_reason ?? ""}` : d.policy_reason}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <ForwardLog summary={forward.summary} rows={forward.rows} symbol={symbol} error={forward.error} />

      <div className="two">
        <section className="card">
          <h2>Daily reports</h2>
          {data.reports.length === 0 ? (
            <p className="muted">The paper loop writes one after each UTC day ends.</p>
          ) : (
            <ul className="days">
              {data.reports.map((d) => (
                <li key={d}>
                  <button className={report?.day === d ? "link on" : "link"} onClick={() => openReport(d)}>
                    {d}
                  </button>
                </li>
              ))}
            </ul>
          )}
          {report && <pre className="report">{report.markdown}</pre>}
        </section>

        <section className="card">
          <h2>Backtests</h2>
          {data.backtests.length === 0 ? (
            <p className="muted">None saved. Run python -m jevtrade.backtest.</p>
          ) : (
            <div className="scroll"><table>
              <thead>
                <tr><th>Run</th><th className="num">Return</th><th className="num">Max DD</th><th className="num">Sharpe</th><th className="num">Trades</th></tr>
              </thead>
              <tbody>
                {data.backtests.map((b) => (
                  <tr key={b.run_id}>
                    <td title={`${b.start} to ${b.end}`}>{b.run_id}</td>
                    <td className={`num ${tone(b.total_return) ?? ""}`}>{fmtPct(b.total_return)}</td>
                    <td className="num">{fmtPct(b.max_drawdown)}</td>
                    <td className="num">{b.sharpe ?? "n/a"}</td>
                    <td className="num">{b.trades}</td>
                  </tr>
                ))}
              </tbody>
            </table></div>
          )}
        </section>
      </div>

      <footer className="muted small">
        Read-only view of the paper account. Refreshes every minute. Last bar{" "}
        {status ? fmtTime(status.last_bar_ts) : "n/a"} UTC.
      </footer>
    </div>
  );
}
