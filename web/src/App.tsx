import { useCallback, useEffect, useState } from "react";
import { ApiError, get, getForwardRows, getForwardSummary } from "./api";
import type { BacktestRow, Config, Decision, EquityPoint, ForwardRow, ForwardSummary, Status, Trade } from "./api";
import { EquityChart } from "./EquityChart";
import { ForwardLog } from "./ForwardLog";
import { ago, fmtMoney, fmtPct, fmtPrice, fmtTime } from "./format";

const REFRESH_MS = 60_000;
const RANGES = [
  { id: "24h", label: "24H", hours: 24 },
  { id: "7d", label: "7D", hours: 24 * 7 },
  { id: "30d", label: "30D", hours: 24 * 30 },
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
    <span className={`health ${ok ? "good" : "warning"}`} role="status" title={`heartbeat ${hb.text}`}>
      <span className="pip" aria-hidden="true" />
      {ok ? "Live" : "Loop not seen"}
      <span className="label"> · {hb.text}</span>
    </span>
  );
}

function tone(v: number | null | undefined): "up" | "down" | undefined {
  return v === null || v === undefined || v === 0 ? undefined : v > 0 ? "up" : "down";
}

const COIN_COLORS: Record<string, string> = {
  BTC: "#f7931a", ETH: "#627eea", SOL: "#9945ff", XRP: "#23292f", DOGE: "#c2a633", ADA: "#0033ad", LTC: "#345d9d",
};

/** A round badge for the base asset, the way coin apps mark a market. */
function Coin({ symbol, showQuote = true }: { symbol: string; showQuote?: boolean }) {
  const [base, quote] = symbol.split("/");
  const hue = [...base].reduce((h, c) => h + c.charCodeAt(0) * 37, 0) % 360;
  return (
    <span className="coin-cell">
      <span className="coin" style={{ background: COIN_COLORS[base] ?? `hsl(${hue} 55% 48%)` }} aria-hidden="true">
        {base.slice(0, 1)}
      </span>
      <span>
        {base}
        {showQuote && quote && <span className="coin-quote">/{quote}</span>}
      </span>
    </span>
  );
}

/** Up, flat and down probabilities as one stacked bar. */
function Odds({ p }: { p: Record<string, number> }) {
  const w = (k: string) => `${Math.max(0, (p[k] ?? 0) * 100)}%`;
  return (
    <span className="odds" aria-hidden="true">
      <span className="o-up" style={{ width: w("up") }} />
      <span className="o-flat" style={{ width: w("flat") }} />
      <span className="o-down" style={{ width: w("down") }} />
    </span>
  );
}

type Theme = "light" | "dark";

function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(() => {
    try {
      const saved = localStorage.getItem("theme");
      if (saved === "light" || saved === "dark") return saved;
    } catch {
      /* storage blocked */
    }
    return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  });
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);
  const toggle = () => {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next);
    try {
      localStorage.setItem("theme", next);
    } catch {
      /* storage blocked */
    }
  };
  return [theme, toggle];
}

const Icon = {
  refresh: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M21 12a9 9 0 1 1-2.64-6.36" /><path d="M21 4v5h-5" />
    </svg>
  ),
  sun: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
      <circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
    </svg>
  ),
  moon: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
    </svg>
  ),
};

export default function App() {
  const [data, setData] = useState<Data>(empty);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [range, setRange] = useState<RangeId>("7d");
  const [symbol, setSymbol] = useState<string>("");
  const [report, setReport] = useState<{ day: string; markdown: string } | null>(null);
  const [forward, setForward] = useState<Forward>({ summary: null, rows: [], error: null });
  const [activity, setActivity] = useState<"trades" | "decisions">("trades");
  const [theme, toggleTheme] = useTheme();

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

  const first = equity[0]?.equity;
  const last = equity[equity.length - 1]?.equity;
  const rangeReturn = first && last !== undefined ? last / first - 1 : null;
  const rangeLabel = RANGES.find((r) => r.id === range)!.label;

  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <div className="brand">
            <span className="logo" aria-hidden="true">J</span>
            <h1>Jev</h1>
            <span className="badge" title="Simulated fills, never real orders">PAPER</span>
          </div>
          <span className="spacer" />
          <Health status={status} />
          <button className="icon-btn" onClick={load} aria-label="Refresh" title="Refresh">
            {Icon.refresh}
          </button>
          <button
            className="icon-btn"
            onClick={toggleTheme}
            aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
            title={theme === "dark" ? "Light theme" : "Dark theme"}
          >
            {theme === "dark" ? Icon.sun : Icon.moon}
          </button>
        </div>
      </header>

      <main className={`page ${loading ? "refreshing" : ""}`}>
        {error && (
          <div className="banner" role="alert">
            <span aria-hidden="true">▲</span> Can't reach the API: {error}. Retrying every minute.
          </div>
        )}

        <section className="card hero">
          <div className="hero-top">
            <div>
              <div className="hero-label">Portfolio value</div>
              <div className="balance">
                {fmtMoney(status?.equity)}
                <span className="ccy">USD</span>
              </div>
              <div className="deltas">
                <span className={`pill ${tone(status?.total_return) ?? ""}`}>{fmtPct(status?.total_return)}</span>
                <span>since start</span>
                {rangeReturn !== null && range !== "all" && (
                  <>
                    <span className={`pill ${tone(rangeReturn) ?? ""}`}>{fmtPct(rangeReturn)}</span>
                    <span>{rangeLabel}</span>
                  </>
                )}
              </div>
            </div>
            <div className="seg" role="group" aria-label="Equity range">
              {RANGES.map((r) => (
                <button key={r.id} className={r.id === range ? "on" : ""} onClick={() => setRange(r.id)}>
                  {r.label}
                </button>
              ))}
            </div>
          </div>
          <EquityChart points={equity} initial={status?.initial_equity ?? 10_000} />
          <p className="muted small">
            {status ? `${status.model} (${status.model_version})` : "Paper run not started"} · {config?.exchange}{" "}
            {config?.timeframe} · simulated fills, never real orders
          </p>
        </section>

        <nav className="markets" aria-label="Symbol">
          <button className={`chip all ${symbol === "" ? "on" : ""}`} onClick={() => setSymbol("")} aria-pressed={symbol === ""}>
            All markets
          </button>
          {config?.symbols.map((s) => (
            <button key={s} className={`chip ${symbol === s ? "on" : ""}`} onClick={() => setSymbol(s)} aria-pressed={symbol === s}>
              <Coin symbol={s} />
            </button>
          ))}
        </nav>

        <section className="tiles">
          <Tile label="Cash" value={fmtMoney(status?.cash)} sub={`from ${fmtMoney(status?.initial_equity, 0)} start`} />
          <Tile label="Today (UTC)" value={fmtPct(status?.risk.day_return)} tone={tone(status?.risk.day_return)} />
          <Tile label="Drawdown" value={fmtPct(status?.drawdown)} tone={status && status.drawdown < 0 ? "down" : undefined} />
          <Tile
            label="Closed trades"
            value={String(status?.trades ?? 0)}
            sub={`win rate ${status?.win_rate == null ? "n/a" : fmtPct(status.win_rate, false)}`}
          />
          <Tile label="Realized PnL" value={fmtMoney(status?.realized_pnl)} tone={tone(status?.realized_pnl)} />
        </section>

        <div className="two split">
          <section className="card">
            <h2>Open positions</h2>
            {positions.length === 0 ? (
              <p className="muted">None.</p>
            ) : (
              <div className="scroll"><table>
                <thead>
                  <tr><th>Market</th><th className="num">Qty</th><th className="num">Entry</th><th className="num">Stop</th><th className="num">Mark</th><th className="num">Unrealized</th></tr>
                </thead>
                <tbody>
                  {positions.map((p) => (
                    <tr key={p.symbol}>
                      <td><Coin symbol={p.symbol} /></td>
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
              <p className="muted small pending">
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
          <div className="card-head">
            <h2>Activity</h2>
            <div className="seg" role="tablist" aria-label="Activity">
              <button role="tab" aria-selected={activity === "trades"} className={activity === "trades" ? "on" : ""} onClick={() => setActivity("trades")}>
                Trades
              </button>
              <button role="tab" aria-selected={activity === "decisions"} className={activity === "decisions" ? "on" : ""} onClick={() => setActivity("decisions")}>
                Decisions
              </button>
            </div>
          </div>
          {activity === "trades" ? (
            trades.length === 0 ? (
              <p className="muted">No closed trades yet.</p>
            ) : (
              <div className="scroll">
                <table>
                  <thead>
                    <tr><th>Market</th><th>Entry (UTC)</th><th>Exit (UTC)</th><th className="num">Bars</th><th className="num">Return</th><th className="num">PnL</th><th>Exit reason</th></tr>
                  </thead>
                  <tbody>
                    {trades.slice(0, 50).map((t) => (
                      <tr key={`${t.symbol}-${t.entry_ts}`}>
                        <td><Coin symbol={t.symbol} /></td>
                        <td>{fmtTime(t.entry_ts)} <span className="at">@ {fmtPrice(t.entry_price)}</span></td>
                        <td>{fmtTime(t.exit_ts)} <span className="at">@ {fmtPrice(t.exit_price)}</span></td>
                        <td className="num">{t.bars_held}</td>
                        <td className={`num ${tone(t.ret) ?? ""}`}>{fmtPct(t.ret)}</td>
                        <td className={`num ${tone(t.pnl) ?? ""}`}>{fmtMoney(t.pnl)}</td>
                        <td>{t.exit_reason.split(":")[0]}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          ) : decisions.length === 0 ? (
            <p className="muted">No decisions yet.</p>
          ) : (
            <div className="scroll">
              <table>
                <thead>
                  <tr><th>Bar (UTC)</th><th>Market</th><th className="num">p(up)</th><th className="num">p(flat)</th><th className="num">p(down)</th><th className="num">Odds</th><th>Action</th><th>Reason</th></tr>
                </thead>
                <tbody>
                  {decisions.map((d) => {
                    const p = d.probs.direction ?? {};
                    return (
                      <tr key={d.id}>
                        <td>{fmtTime(d.bar_ts)}</td>
                        <td><Coin symbol={d.symbol} /></td>
                        <td className="num up">{d.abstain ? "–" : (p.up ?? 0).toFixed(2)}</td>
                        <td className="num">{d.abstain ? "–" : (p.flat ?? 0).toFixed(2)}</td>
                        <td className="num down">{d.abstain ? "–" : (p.down ?? 0).toFixed(2)}</td>
                        <td className="num">{d.abstain ? "–" : <Odds p={p} />}</td>
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
      </main>
    </>
  );
}
