import { useCallback, useEffect, useMemo, useState } from "react";
import { get, getForwardRows, getForwardSummary, getJevPaper } from "./lib/api";
import type { Config, ForwardRow, ForwardSummary, JevHour, JevPaper } from "./lib/api";
import { CoinPage } from "./pages/CoinPage";
import { EquityChart } from "./components/EquityChart";
import { ForwardLog } from "./pages/ForwardLog";
import { TZ, TZ_NAME, ago, fmtMoney, fmtPct, fmtPrice, fmtTime } from "./lib/format";
import { useLiveMarket, useWatchlist } from "./lib/live";
import type { FeedStatus } from "./lib/live";
import { MarketView } from "./pages/Market";
import type { JevCoin } from "./pages/Market";
import { Coin, tone } from "./components/ui";

// Two views. Market: every tracked coin live from Coinbase, broker-app style, with
// a page per coin. Jev: its hourly forward calls (collect workflow, data-log branch)
// and the paper account the policy would have run on them. Simulated fills, never
// real orders.

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

/** Live while the Coinbase socket streams; otherwise prices refresh every few seconds. */
function FeedHealth({ status }: { status: FeedStatus }) {
  const ok = status === "live";
  return (
    <span className={`health ${ok ? "good" : "warning"}`} role="status" title={ok ? "Prices stream from Coinbase" : "Coinbase stream unavailable; prices refresh every 5 seconds"}>
      <span className="pip" aria-hidden="true" />
      <span className="feed-label">{ok ? "Live prices" : status === "connecting" ? "Connecting" : "Delayed prices"}</span>
    </span>
  );
}

type Route = { view: "market" } | { view: "coin"; symbol: string } | { view: "jev" };

function parseRoute(hash: string): Route {
  const coin = /^#\/coin\/([A-Za-z0-9]+)-([A-Za-z0-9]+)$/.exec(hash);
  if (coin) return { view: "coin", symbol: `${coin[1].toUpperCase()}/${coin[2].toUpperCase()}` };
  if (hash === "#/jev") return { view: "jev" };
  return { view: "market" };
}

/** Hash routes (#/, #/coin/BTC-USD, #/jev) so the back button and links work. */
function useRoute(): [Route, (hash: string) => void] {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash));
  useEffect(() => {
    const on = () => {
      setRoute(parseRoute(window.location.hash));
      window.scrollTo(0, 0);
    };
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return [route, (hash: string) => (window.location.hash = hash)];
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
          {paper?.policy.entry_threshold ?? 0.55} and beats p(down) by {paper?.policy.min_edge ?? 0.1}
          {paper?.gate ? ", while the skill gate is open" : ""}.
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

/** Why the account passed on a pick, from the first word of the policy's reason. */
const PASSED: Record<string, string> = {
  hold: "already held", skill_gate: "held back by the skill gate", no_capacity: "not bought: no room left",
  max_daily_loss: "not bought: daily loss limit", cooldown: "not bought: cooling down after losses",
  min_trade_interval: "not bought: sold too recently",
};

const ticker = (s: string) => s.split("/")[0];
/** "MM-DD HH:mm" local, for times inside a row. */
const fmtShort = (ms: number) => fmtTime(ms).slice(5);

/** What the account did in one hour, for the coins shown. */
function didThisHour(h: JevHour, picks: JevHour["picks"], symbol: string): string[] {
  const out: string[] = [];
  const bought = h.bought.filter((s) => !symbol || s === symbol);
  if (bought.length) out.push(`Bought ${bought.map(ticker).join(", ")}`);
  for (const s of h.sold) if (!symbol || s.symbol === symbol) out.push(`Sold ${ticker(s.symbol)} (${s.why.replace(/_/g, " ")})`);
  const passed = new Map<string, number>();
  for (const p of picks) {
    if (p.action === "enter" || !p.why) continue;
    const label = PASSED[p.why] ?? p.why.replace(/_/g, " ");
    passed.set(label, (passed.get(label) ?? 0) + 1);
  }
  for (const [label, n] of passed) out.push(`${n} ${label}`);
  return out;
}

const PICKS_SHOWN = 8;

/** Jev's picks hour by hour, bought or not, and how they did once the question's horizon closed. */
function Hours({ paper, symbol, horizon }: { paper: JevPaper | null; symbol: string; horizon: number }) {
  const [open, setOpen] = useState<Set<number>>(() => new Set());
  const hours = paper?.hours ?? [];
  const pol = paper?.policy;
  if (hours.length === 0) return <p className="muted">No hours logged yet.</p>;
  return (
    <>
      <p className="muted small hours-note">
        Every hour Jev is asked about each coin. Its picks are the coins with p(up) ≥ {pol?.entry_threshold} and
        p(up) − p(down) ≥ {pol?.min_edge}{paper?.gate ? "; the account buys them only while the skill gate is open" : ""}.
        After {horizon} hours each hour shows how its picks did, before and after a round trip of costs, next to the average coin.
      </p>
      <div className="scroll">
        <table>
          <thead>
            <tr>
              <th>Hour ({TZ})</th><th>Jev's picks, p(up)</th><th>What the account did</th>
              <th className="num">Picks after {horizon}h</th><th className="num">Average coin</th>
            </tr>
          </thead>
          <tbody>
            {hours.map((h) => {
              const picks = h.picks.filter((p) => !symbol || p.symbol === symbol);
              const done = picks.filter((p) => p.ret != null && p.net != null);
              const avg = (k: "ret" | "net") => done.reduce((s, p) => s + (p[k] ?? 0), 0) / done.length;
              const did = didThisHour(h, picks, symbol);
              const later = <span className="muted" title={`known at ${fmtTime(h.resolves_at)} ${TZ}`}>after {fmtShort(h.resolves_at)}</span>;
              const all = open.has(h.bar_ts) || picks.length <= PICKS_SHOWN + 1;
              return (
                <tr key={h.bar_ts}>
                  <td>
                    {fmtShort(h.bar_ts)}
                    {h.asked > 0 && <div className="at small" title="coins Jev answered / coins asked">{h.answered}/{h.asked} answered</div>}
                  </td>
                  <td className="picks-cell">
                    {!h.asked ? (
                      <span className="muted">Not asked: the hourly run skipped this hour</span>
                    ) : picks.length === 0 ? (
                      <span className="muted">{symbol ? "Not picked" : "None"}</span>
                    ) : (
                      <span className="picks">
                        {(all ? picks : picks.slice(0, PICKS_SHOWN)).map((p) => (
                          <span
                            key={p.symbol}
                            className={`pick ${tone(p.ret) ?? ""}`}
                            title={`${p.symbol}: p(up) ${p.p_up.toFixed(2)}, p(down) ${p.p_down.toFixed(2)}` +
                              (p.ret != null ? `; ${fmtPct(p.ret)} after ${horizon} hours` : "")}
                          >
                            {ticker(p.symbol)} <span className="p">{p.p_up.toFixed(2)}</span>
                          </span>
                        ))}
                        {!all && (
                          <button className="pick more" onClick={() => setOpen(new Set(open).add(h.bar_ts))}>
                            +{picks.length - PICKS_SHOWN} more
                          </button>
                        )}
                      </span>
                    )}
                  </td>
                  <td className="reason">{did.length ? did.join(" · ") : <span className="muted">{h.asked ? "Nothing" : "–"}</span>}</td>
                  <td className="num">
                    {done.length ? (
                      <>
                        <span className={tone(avg("ret"))}>{fmtPct(avg("ret"))}</span>
                        <div className="at small">{fmtPct(avg("net"))} after costs</div>
                      </>
                    ) : picks.length ? later : "–"}
                  </td>
                  <td className="num">
                    {h.market != null ? <span className={tone(h.market)}>{fmtPct(h.market)}</span> : h.asked ? later : "–"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}

/** Jev's paper portfolio and forward log, as before. */
function JevView({ data }: { data: Data }) {
  const [range, setRange] = useState<RangeId>("all");
  const [symbol, setSymbol] = useState<string>("");
  const [activity, setActivity] = useState<"hours" | "buys" | "trades" | "calls">("hours");

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
  const gate = paper?.gate;

  const first = equity[0]?.equity;
  const last = equity[equity.length - 1]?.equity;
  const rangeReturn = first && last !== undefined ? last / first - 1 : null;
  const rangeLabel = RANGES.find((r) => r.id === range)!.label;

  return (
    <>
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
          <span aria-hidden="true">▲</span> Likely better than real trading. Prices are Coinbase hourly candles with
          Coinbase Advanced's taker fee and an estimated order-book spread, not real fills. Hours logged before candle highs and lows were
          recorded only check the{pol ? ` ${fmtPct(pol.stop_loss_pct, false)}` : ""} stop-loss on closes.
        </p>
        {gate && !gate.open && (
          <p className="muted small" role="status">
            Not buying for now: the skill gate is closed until Jev's recent buy signals make money after costs and beat the
            average coin. Held coins still sell on Jev's answer and the stop.
          </p>
        )}
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
              <dt>Buy when</dt><dd>p(up) ≥ {pol.entry_threshold} and p(up) − p(down) ≥ {pol.min_edge}{gate ? ", while the skill gate is open" : ""}</dd>
              {gate && (
                <>
                  <dt>Skill gate</dt>
                  <dd>
                    <span className={`pill ${gate.open ? "up" : ""}`}>{gate.open ? "Open" : "Closed"}</span>{" "}
                    Buys only while Jev's buy signals from the last {gate.lookback_hours / 24} days made money after costs and beat the average coin
                    {gate.avg_net != null && gate.avg_excess != null
                      ? ` (now ${fmtPct(gate.avg_net)} after costs, ${fmtPct(gate.avg_excess)} vs the average coin, ${gate.signals} signals).`
                      : ` (${gate.signals} of ${gate.min_signals} signals resolved so far).`}
                  </dd>
                </>
              )}
              <dt>Sell when</dt><dd>p(down) ≥ {pol.exit_threshold}{pol.exit_min_edge ? ` and p(down) − p(up) ≥ ${pol.exit_min_edge}` : ""}, or the stop{pol.max_holding_bars != null ? `, or ${pol.max_holding_bars} hours held` : ""}</dd>
              <dt>Stop-loss</dt><dd>{fmtPct(pol.stop_loss_pct, false)}</dd>
              {config && (
                <>
                  <dt>Position at stop</dt><dd>{fmtPct(Number(config.sizing.position_frac_at_stop), false)} of equity</dd>
                  {paper.max_volume_frac != null && (
                    <><dt>Thin coins</dt><dd>a buy is at most {fmtPct(paper.max_volume_frac, false)} of the coin's hourly dollar volume</dd></>
                  )}
                  <dt>Max position / gross</dt><dd>{fmtPct(Number(config.sizing.max_position_frac), false)} / {fmtPct(Number(config.sizing.max_gross_exposure), false)}</dd>
                  <dt>Direction question</dt><dd>±{config.flat_band_pct}% over {config.horizon_bars} hours</dd>
                </>
              )}
              <dt>Costs</dt><dd>
                {paper.fee_bps ? `${paper.fee_bps} bps fee + ` : "No fee, "}
                {fmtPct(paper.spread_bps.min / 10_000, false)}–{fmtPct(paper.spread_bps.max / 10_000, false)} spread per side
                {paper.slippage_bps ? ` + ${paper.slippage_bps} bps slippage` : ""}
                {" "}(Coinbase Advanced; spread wider for thin coins)
              </dd>
            </dl>
          )}
          <p className="muted small pending">
            Orders fill when the hourly run actually asked Jev (often late), at a price estimated within that hour.
            Stops trigger on the hour's low.
          </p>
        </section>
      </div>

      <section className="card">
        <div className="card-head">
          <h2>Jev's activity</h2>
          <div className="seg" role="tablist" aria-label="Activity">
            {(["hours", "buys", "trades", "calls"] as const).map((k) => (
              <button key={k} role="tab" aria-selected={activity === k} className={activity === k ? "on" : ""} onClick={() => setActivity(k)}>
                {k === "hours" ? "By hour" : k === "buys" ? "Buys" : k === "trades" ? "Trades" : "Every call"}
              </button>
            ))}
          </div>
        </div>
        {activity === "hours" ? (
          <Hours paper={paper} symbol={symbol} horizon={config?.horizon_bars ?? 24} />
        ) : activity === "trades" ? (
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
            {activity !== "buys"
              ? "No calls yet."
              : gate && !gate.open
                ? `No buys yet${symbol ? ` on ${symbol}` : ""}: the skill gate is closed, so Jev's picks aren't bought. By hour shows them.`
                : `No buys yet. Jev's p(up) hasn't reached ${pol?.entry_threshold ?? 0.55} with enough edge${symbol ? ` on ${symbol}` : ""}.`}
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
    </>
  );
}

export default function App() {
  const [data, setData] = useState<Data>(empty);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [theme, toggleTheme] = useTheme();
  const [route, go] = useRoute();
  const market = useLiveMarket();
  const [watch, toggleWatch] = useWatchlist();

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

  // What Jev holds, is buying, and last said, per coin, for the market list.
  const jev = useMemo(() => {
    const out: Record<string, JevCoin> = {};
    const paper = data.paper;
    if (!paper) return out;
    for (const a of paper.actions) if (!out[a.symbol]) out[a.symbol] = { holding: false, buying: false, last: a };
    for (const p of paper.positions) out[p.symbol] = { ...out[p.symbol], buying: false, holding: true };
    for (const p of paper.pending) if (p.kind === "enter") out[p.symbol] = { holding: out[p.symbol]?.holding ?? false, last: out[p.symbol]?.last, buying: true };
    return out;
  }, [data.paper]);

  const tab = route.view === "jev" ? "jev" : "market";
  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <a className="brand" href="#/" aria-label="Jev market">
            <span className="logo" aria-hidden="true">J</span>
            <h1>Jev</h1>
            <span className="badge" title="Simulated fills, never real orders">PAPER</span>
          </a>
          <nav className="tabs" aria-label="View">
            <a href="#/" className={tab === "market" ? "on" : ""} aria-current={tab === "market" ? "page" : undefined}>Market</a>
            <a href="#/jev" className={tab === "jev" ? "on" : ""} aria-current={tab === "jev" ? "page" : undefined}>Jev portfolio</a>
          </nav>
          <span className="spacer" />
          {tab === "jev" ? (
            <>
              <span className="health tz" title={`Times are shown in your local time zone, ${TZ_NAME}`}>{TZ}</span>
              <Health lastCall={data.summary?.last_called_at ?? null} />
            </>
          ) : (
            <FeedHealth status={market.status} />
          )}
          <button className="icon-btn refresh" onClick={load} aria-label="Refresh" title="Refresh">
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
        {route.view === "jev" ? (
          <JevView data={data} />
        ) : route.view === "coin" ? (
          <CoinPage
            key={route.symbol}
            symbol={route.symbol}
            coin={market.bySymbol[route.symbol]}
            status={market.status}
            paper={data.paper}
            starred={watch.has(route.symbol)}
            toggleStar={() => toggleWatch(route.symbol)}
            theme={theme}
            back={() => go("#/")}
          />
        ) : (
          <MarketView market={market} jev={jev} watch={watch} toggleWatch={toggleWatch} open={(s) => go(`#/coin/${s.replace("/", "-")}`)} />
        )}
      </main>
    </>
  );
}
