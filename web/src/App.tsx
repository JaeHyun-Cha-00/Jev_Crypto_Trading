import { useCallback, useEffect, useState } from "react";
import { get, getForwardRows, getForwardSummary, getJevPaper } from "./api";
import type { Config, ForwardRow, ForwardSummary, JevPaper } from "./api";
import { EquityChart } from "./EquityChart";
import { ForwardLog } from "./ForwardLog";
import { TZ, TZ_NAME, ago, fmtMoney, fmtPct, fmtPrice, fmtTime } from "./format";

// Only Jev: its hourly forward calls (collect workflow, data-log branch) and the
// paper account the policy would have run on them. Simulated fills, never real orders.

const REFRESH_MS = 60_000;
const RANGES = [
  { id: "24h", label: "24H", hours: 24 },
  { id: "7d", label: "7D", hours: 24 * 7 },
  { id: "30d", label: "30D", hours: 24 * 30 },
  { id: "all", label: "All", hours: Infinity },
] as const;
type RangeId = (typeof RANGES)[number]["id"];

interface Data {
  config: Config | null;
  paper: JevPaper | null;
  summary: ForwardSummary | null;
  rows: ForwardRow[];
}

const empty: Data = { config: null, paper: null, summary: null, rows: [] };

/** Live while the hourly collector has called Jev recently. */
function Health({ lastCall }: { lastCall: string | null }) {
  const hb = ago(lastCall);
  const ok = hb.minutes < 90;
  return (
    <span className={`health ${ok ? "good" : "warning"}`} role="status" title={`last Jev call ${hb.text}`}>
      <span className="pip" aria-hidden="true" />
      {ok ? "Live" : "No recent Jev call"}
      <span className="label"> · {hb.text}</span>
    </span>
  );
}

const ACTION_LABEL: Record<string, string> = { enter: "buy", exit: "sell", hold: "hold", skip: "pass" };

function Tile({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: "up" | "down" }) {
  return (
    <div className="tile">
      <div className="tile-label">{label}</div>
      <div className={`tile-value ${tone ?? ""}`}>{value}</div>
      {sub && <div className="tile-sub">{sub}</div>}
    </div>
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

/** Every coin Jev bought: what it holds now, what it is about to buy, and what it sold. */
function Bought({ paper, symbol }: { paper: JevPaper | null; symbol: string }) {
  const mine = <T extends { symbol: string }>(xs: T[]) => xs.filter((x) => !symbol || x.symbol === symbol);
  const pending = mine(paper?.pending ?? []).filter((p) => p.kind === "enter");
  const held = mine(paper?.positions ?? []);
  const sold = mine(paper?.trades ?? []);
  const base = (s: string) => s.split("/")[0];
  const count = pending.length + held.length + sold.length;
  return (
    <section className="card bought">
      <div className="card-head">
        <h2>What Jev bought</h2>
        <span className="muted small">
          {paper?.tracking_since != null && `Since ${fmtTime(paper.tracking_since)} ${TZ}`}
          {count === 0 ? "" : ` · ${held.length} holding · ${pending.length} buying · ${sold.length} sold`}
        </span>
      </div>
      {count === 0 ? (
        <p className="muted">
          Jev hasn't bought anything{symbol ? ` on ${symbol}` : ""} yet. It buys when p(up) is at least{" "}
          {paper?.policy.entry_threshold ?? 0.55} and beats p(down) by {paper?.policy.min_edge ?? 0.1}.
        </p>
      ) : (
        <div className="scroll"><table>
          <thead>
            <tr>
              <th>Coin</th><th>Status</th><th>Bought ({TZ})</th><th className="num">Buy price</th><th className="num">Size</th>
              <th>Sold ({TZ})</th><th className="num">Sell price</th><th className="num">P&amp;L</th>
            </tr>
          </thead>
          <tbody>
            {pending.map((p) => (
              <tr key={`pending-${p.symbol}`}>
                <td><Coin symbol={p.symbol} /></td>
                <td><span className="tag">buying</span></td>
                <td className="muted">{p.fill_after != null ? `at ${fmtTime(p.fill_after)}` : "next hour's open"}</td>
                <td className="num">~{fmtPrice(p.ref_close)}</td>
                <td className="num">{fmtPct(p.size_frac, false)} of equity</td>
                <td>–</td><td className="num">–</td><td className="num">–</td>
              </tr>
            ))}
            {held.map((p) => (
              <tr key={`held-${p.symbol}`}>
                <td><Coin symbol={p.symbol} /></td>
                <td><span className="tag enter">holding</span></td>
                <td>{fmtTime(p.entry_ts)}</td>
                <td className="num">{fmtPrice(p.entry_price)}</td>
                <td className="num">${fmtMoney(p.qty * p.entry_price, 0)} <span className="at">{p.qty.toPrecision(3)} {base(p.symbol)}</span></td>
                <td className="muted">now</td>
                <td className="num">{fmtPrice(p.mark)}</td>
                <td className={`num ${tone(p.unrealized_pnl) ?? ""}`}>{fmtMoney(p.unrealized_pnl)} <span className="at">{fmtPct(p.mark / p.entry_price - 1)}</span></td>
              </tr>
            ))}
            {sold.map((t) => (
              <tr key={`sold-${t.symbol}-${t.entry_ts}`}>
                <td><Coin symbol={t.symbol} /></td>
                <td><span className="tag exit" title={t.exit_reason}>sold</span></td>
                <td>{fmtTime(t.entry_ts)}</td>
                <td className="num">{fmtPrice(t.entry_price)}</td>
                <td className="num">${fmtMoney(t.qty * t.entry_price, 0)} <span className="at">{t.qty.toPrecision(3)} {base(t.symbol)}</span></td>
                <td>{fmtTime(t.exit_ts)}</td>
                <td className="num">{fmtPrice(t.exit_price)}</td>
                <td className={`num ${tone(t.pnl) ?? ""}`}>{fmtMoney(t.pnl)} <span className="at">{fmtPct(t.ret)}</span></td>
              </tr>
            ))}
          </tbody>
        </table></div>
      )}
    </section>
  );
}

export default function App() {
  const [data, setData] = useState<Data>(empty);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [range, setRange] = useState<RangeId>("all");
  const [symbol, setSymbol] = useState<string>("");
  const [activity, setActivity] = useState<"buys" | "trades" | "calls">("buys");
  const [theme, toggleTheme] = useTheme();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [config, paper, summary, rows] = await Promise.all([
        get<Config>("/config"),
        getJevPaper(500),
        getForwardSummary(),
        getForwardRows({ limit: 300 }),
      ]);
      setData({ config, paper, summary, rows });
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

  const { config, paper, summary } = data;
  const curve = paper?.curve ?? [];
  const hours = RANGES.find((r) => r.id === range)!.hours;
  const cutoff = curve.length ? curve[curve.length - 1].bar_ts - hours * 3_600_000 : 0;
  const equity = curve.filter((p) => p.bar_ts >= cutoff);
  const mine = <T extends { symbol: string }>(xs: T[]) => xs.filter((x) => !symbol || x.symbol === symbol);
  const actions = mine(paper?.actions ?? []);
  const buys = mine(paper?.buys ?? []);
  const trades = mine(paper?.trades ?? []);
  const symbols = config?.symbols ?? Object.keys(paper?.per_symbol ?? {});
  // Coins Jev bought at least once (or is buying) get their own chip; the rest sit in a picker.
  const traded = symbols.filter((s) => (paper?.per_symbol[s]?.buys ?? 0) > 0 || paper?.pending.some((p) => p.symbol === s));
  const coinRows = Object.entries(paper?.per_symbol ?? {}).filter(([s, v]) => v.buys > 0 && (!symbol || s === symbol));
  const closed = paper?.trades ?? [];
  const wins = closed.filter((t) => t.pnl > 0).length;
  const realized = closed.reduce((s, t) => s + t.pnl, 0);
  const pol = paper?.policy;

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
          <span className="health tz" title={`Times are shown in your local time zone, ${TZ_NAME}`}>{TZ}</span>
          <Health lastCall={summary?.last_called_at ?? null} />
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

        <nav className="markets" aria-label="Coin">
          <button className={`chip all ${symbol === "" ? "on" : ""}`} onClick={() => setSymbol("")} aria-pressed={symbol === ""}>
            All coins
          </button>
          {traded.map((s) => (
            <button key={s} className={`chip ${symbol === s ? "on" : ""}`} onClick={() => setSymbol(s)} aria-pressed={symbol === s}>
              <Coin symbol={s} />
            </button>
          ))}
          <select
            className="chip all coin-pick"
            aria-label="Any coin"
            value={traded.includes(symbol) ? "" : symbol}
            onChange={(e) => setSymbol(e.target.value)}
          >
            <option value="">{traded.length ? `Other coins (${symbols.length - traded.length})` : `Pick a coin (${symbols.length})`}</option>
            {symbols.filter((s) => !traded.includes(s)).map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </nav>

        <Bought paper={paper} symbol={symbol} />

        <section className="card hero">
          <div className="hero-top">
            <div>
              <div className="hero-label">
                Jev paper portfolio
                {paper?.tracking_since != null && (
                  <span className="since"> · Tracking since {fmtTime(paper.tracking_since)} {TZ}</span>
                )}
              </div>
              <div className="balance">
                {fmtMoney(paper?.equity)}
                <span className="ccy">USD</span>
              </div>
              <div className="deltas">
                <span className={`pill ${tone(paper?.total_return) ?? ""}`}>{fmtPct(paper?.total_return)}</span>
                <span>since tracking started</span>
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
          <EquityChart points={equity} initial={paper?.initial_equity ?? 10_000} />
          <p className="caveat small" role="note">
            <span aria-hidden="true">▲</span> Likely better than real trading. The replay only has hourly closes, so the
            {pol ? ` ${fmtPct(pol.stop_loss_pct, false)}` : ""} stop-loss fires on a close, never on a dip within the hour.
            Real stops would trigger more often and fill lower.
          </p>
          <p className="muted small">
            Jev's own hourly answers run through the trading rules · {symbols.length > 4 ? `${symbols.length} coins` : symbols.join(", ") || "no coins yet"} ·
            simulated fills, never real orders
          </p>
        </section>

        <section className="tiles">
          <Tile label="Cash" value={fmtMoney(paper?.cash)} sub={`from ${fmtMoney(paper?.initial_equity, 0)} start`} />
          <Tile label="Buys" value={String(paper?.counts.enter ?? 0)} sub={`out of ${paper?.calls ?? 0} hourly calls`} />
          <Tile label="Drawdown" value={fmtPct(paper?.drawdown)} tone={paper && paper.drawdown < 0 ? "down" : undefined} />
          <Tile
            label="Closed trades"
            value={String(closed.length)}
            sub={`win rate ${closed.length ? fmtPct(wins / closed.length, false) : "n/a"}`}
          />
          <Tile label="Realized PnL" value={fmtMoney(realized)} tone={tone(realized)} />
        </section>

        <div className="two split">
          <section className="card">
            <h2>Jev by coin</h2>
            {coinRows.length === 0 ? (
              <p className="muted small">No buys{symbol ? ` on ${symbol}` : ""} yet across {symbol ? 1 : symbols.length} coin{symbol || symbols.length === 1 ? "" : "s"} watched.</p>
            ) : <div className="scroll"><table>
              <thead>
                <tr><th>Coin</th><th className="num">Buys</th><th className="num">Closed</th><th className="num">PnL</th></tr>
              </thead>
              <tbody>
                {coinRows.map(([s, v]) => (
                  <tr key={s}>
                    <td><Coin symbol={s} /></td>
                    <td className="num">{v.buys}</td>
                    <td className="num">{v.trades}</td>
                    <td className={`num ${tone(v.pnl) ?? ""}`}>{fmtMoney(v.pnl)}</td>
                  </tr>
                ))}
              </tbody>
            </table></div>}
            {coinRows.length > 0 && !symbol && symbols.length > coinRows.length && (
              <p className="muted small">{symbols.length - coinRows.length} other coin{symbols.length - coinRows.length === 1 ? "" : "s"} watched, no buys.</p>
            )}
          </section>

          <section className="card">
            <h2>Trading rules</h2>
            {pol && (
              <dl className="kv">
                <dt>Buy when</dt><dd>p(up) ≥ {pol.entry_threshold} and p(up) − p(down) ≥ {pol.min_edge}</dd>
                <dt>Sell when</dt><dd>p(down) ≥ {pol.exit_threshold}, the stop, or {pol.max_holding_bars ?? "no"} hours held</dd>
                <dt>Stop-loss</dt><dd>{fmtPct(pol.stop_loss_pct, false)}</dd>
                {config && (
                  <>
                    <dt>Position at stop</dt><dd>{fmtPct(Number(config.sizing.position_frac_at_stop), false)} of equity</dd>
                    <dt>Max position / gross</dt><dd>{fmtPct(Number(config.sizing.max_position_frac), false)} / {fmtPct(Number(config.sizing.max_gross_exposure), false)}</dd>
                    <dt>Direction question</dt><dd>±{config.flat_band_pct}% over {config.horizon_bars} hours</dd>
                  </>
                )}
                <dt>Costs</dt><dd>{paper.fee_bps} bps fee + {paper.slippage_bps} bps slippage per side</dd>
              </dl>
            )}
            <p className="muted small pending">
              Orders fill when the hourly run actually asked Jev (often late), at a price estimated within that hour.
              Stops only see hourly closes.
            </p>
          </section>
        </div>

        <section className="card">
          <div className="card-head">
            <h2>Jev's activity</h2>
            <div className="seg" role="tablist" aria-label="Activity">
              {(["buys", "trades", "calls"] as const).map((k) => (
                <button key={k} role="tab" aria-selected={activity === k} className={activity === k ? "on" : ""} onClick={() => setActivity(k)}>
                  {k === "buys" ? "Buys" : k === "trades" ? "Trades" : "Every call"}
                </button>
              ))}
            </div>
          </div>
          {activity === "trades" ? (
            trades.length === 0 ? (
              <p className="muted">No closed trades yet.</p>
            ) : (
              <div className="scroll">
                <table>
                  <thead>
                    <tr><th>Coin</th><th>Bought ({TZ})</th><th>Sold ({TZ})</th><th className="num">Hours</th><th className="num">Return</th><th className="num">PnL</th><th>Why sold</th></tr>
                  </thead>
                  <tbody>
                    {trades.slice(0, 100).map((t) => (
                      <tr key={`${t.symbol}-${t.entry_ts}`}>
                        <td><Coin symbol={t.symbol} /></td>
                        <td>{fmtTime(t.entry_ts)} <span className="at">@ {fmtPrice(t.entry_price)}</span></td>
                        <td>{fmtTime(t.exit_ts)} <span className="at">@ {fmtPrice(t.exit_price)}</span></td>
                        <td className="num">{t.bars_held}</td>
                        <td className={`num ${tone(t.ret) ?? ""}`}>{fmtPct(t.ret)}</td>
                        <td className={`num ${tone(t.pnl) ?? ""}`}>{fmtMoney(t.pnl)}</td>
                        <td>{t.exit_reason.split(":")[0].replace("_", " ")}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          ) : (activity === "buys" ? buys : actions).length === 0 ? (
            <p className="muted">
              {activity === "buys"
                ? `No buys yet. Jev's p(up) hasn't reached ${pol?.entry_threshold ?? 0.55} with enough edge${symbol ? ` on ${symbol}` : ""}.`
                : "No calls yet."}
            </p>
          ) : (
            <div className="scroll">
              <table>
                <thead>
                  <tr><th>Hour ({TZ})</th><th>Coin</th><th className="num">Price</th><th className="num">p(up)</th><th className="num">p(down)</th><th className="num">Odds</th><th>Action</th><th>Reason</th></tr>
                </thead>
                <tbody>
                  {(activity === "buys" ? buys : actions).slice(0, 200).map((a) => {
                    const answered = a.status === "answered";
                    const p = { up: a.p_up, down: a.p_down, flat: Math.max(0, 1 - a.p_up - a.p_down) };
                    return (
                      <tr key={`${a.symbol}-${a.bar_ts}`}>
                        <td>{fmtTime(a.bar_ts)}</td>
                        <td><Coin symbol={a.symbol} /></td>
                        <td className="num">{fmtPrice(a.close)}</td>
                        <td className="num up">{answered ? a.p_up.toFixed(2) : "–"}</td>
                        <td className="num down">{answered ? a.p_down.toFixed(2) : "–"}</td>
                        <td className="num">{answered ? <Odds p={p} /> : "–"}</td>
                        <td><span className={`tag ${a.action}`}>{ACTION_LABEL[a.action]}</span></td>
                        <td className="reason">{a.reason}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <ForwardLog summary={summary} rows={data.rows} symbol={symbol} error={null} />

        <footer className="muted small">
          Read-only view of Jev's forward log. Refreshes every minute. Last tracked hour{" "}
          {paper?.last_bar_ts ? `${fmtTime(paper.last_bar_ts)} ${TZ}` : "none yet"}. All times are your local time ({TZ_NAME}).
        </footer>
      </main>
    </>
  );
}
