import { useMemo, useState } from "react";
import type { JevAction } from "../lib/api";
import { fmtCompact, fmtDelta, fmtPct, fmtPrice, fmtUsd } from "../lib/format";
import type { LiveCoin, LiveMarket } from "../lib/live";
import { Coin, LivePrice, Sparkline, Star, tone } from "../components/ui";

// The coin list: every tracked coin with its live price, the day's movers, and
// what Jev thinks of each one.

export interface JevCoin {
  holding: boolean;
  buying: boolean;
  /** Jev's latest hourly call on the coin */
  last?: JevAction;
}

type SortKey = "rank" | "name" | "price" | "change" | "volume" | "range";
type ListTab = "all" | "watch" | "jev";

const SORTS: { id: SortKey; label: string }[] = [
  { id: "volume", label: "Volume" },
  { id: "change", label: "24h change" },
  { id: "price", label: "Price" },
  { id: "name", label: "Name" },
  { id: "range", label: "Near 24h high" },
];

/** Where the price sits in the day's range, 0 at the low and 1 at the high. */
const rangePos = (c: LiveCoin) =>
  c.price !== null && c.high_24h !== null && c.low_24h !== null && c.high_24h > c.low_24h
    ? (c.price - c.low_24h) / (c.high_24h - c.low_24h)
    : null;

function value(c: LiveCoin, k: SortKey): number | string {
  switch (k) {
    case "name":
      return c.symbol;
    case "price":
      return c.price ?? -Infinity;
    case "change":
      return c.change_pct_24h ?? -Infinity;
    case "range":
      return rangePos(c) ?? -Infinity;
    default:
      return c.volume_usd_24h ?? -Infinity;
  }
}

function Range({ c }: { c: LiveCoin }) {
  const p = rangePos(c);
  return (
    <span className="range" title={`24h low ${fmtPrice(c.low_24h)} · high ${fmtPrice(c.high_24h)}`}>
      <span className="range-track">{p !== null && <span className="range-dot" style={{ left: `${p * 100}%` }} />}</span>
    </span>
  );
}

function JevBadge({ j }: { j?: JevCoin }) {
  if (!j) return <span className="muted">–</span>;
  if (j.holding) return <span className="tag enter" title="Jev's paper portfolio holds it">holding</span>;
  if (j.buying) return <span className="tag" title="Jev's paper portfolio is buying it next hour">buying</span>;
  const a = j.last;
  if (!a || a.status !== "answered") return <span className="muted">–</span>;
  return (
    <span className="jev-odds" title={`Jev's last call: p(up) ${a.p_up.toFixed(2)}, p(down) ${a.p_down.toFixed(2)}`}>
      <span className="up">{Math.round(a.p_up * 100)}</span>
      <span className="muted">/</span>
      <span className="down">{Math.round(a.p_down * 100)}</span>
    </span>
  );
}

function Movers({ title, coins, open, metric }: { title: string; coins: LiveCoin[]; open: (s: string) => void; metric: "change" | "volume" }) {
  return (
    <section className="card movers">
      <h2>{title}</h2>
      {coins.length === 0 ? (
        <p className="muted small">Waiting for prices…</p>
      ) : (
        <ol>
          {coins.map((c) => (
            <li key={c.symbol}>
              <button className="mover" onClick={() => open(c.symbol)}>
                <Coin symbol={c.symbol} name={c.name} />
                <span className="mover-num">
                  <LivePrice value={fmtUsd(c.price)} dir={c.dir} seq={c.seq} className="num" />
                  {metric === "change" ? (
                    <span className={`pill ${tone(c.change_pct_24h) ?? ""}`}>{fmtPct(c.change_pct_24h)}</span>
                  ) : (
                    <span className="at">${fmtCompact(c.volume_usd_24h)}</span>
                  )}
                </span>
              </button>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

const HEADLINE = ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD"];

export function MarketView({
  market,
  jev,
  watch,
  toggleWatch,
  open,
}: {
  market: LiveMarket;
  jev: Record<string, JevCoin>;
  watch: Set<string>;
  toggleWatch: (s: string) => void;
  open: (s: string) => void;
}) {
  const [tab, setTab] = useState<ListTab>("all");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({ key: "volume", desc: true });
  const coins = market.coins;
  const priced = coins.filter((c) => c.price !== null);

  const rank = useMemo(() => {
    const r: Record<string, number> = {};
    [...coins].sort((a, b) => (b.volume_usd_24h ?? -1) - (a.volume_usd_24h ?? -1)).forEach((c, i) => (r[c.symbol] = i + 1));
    return r;
  }, [coins]);

  const byChange = [...priced].filter((c) => c.change_pct_24h !== null).sort((a, b) => b.change_pct_24h! - a.change_pct_24h!);
  const gainers = byChange.filter((c) => c.change_pct_24h! > 0).slice(0, 5);
  const losers = byChange.filter((c) => c.change_pct_24h! < 0).reverse().slice(0, 5);
  const active = [...priced].sort((a, b) => (b.volume_usd_24h ?? 0) - (a.volume_usd_24h ?? 0)).slice(0, 5);
  const ups = byChange.filter((c) => c.change_pct_24h! > 0).length;
  const downs = byChange.filter((c) => c.change_pct_24h! < 0).length;
  const totalVol = priced.reduce((s, c) => s + (c.volume_usd_24h ?? 0), 0);
  const jevCount = coins.filter((c) => jev[c.symbol]?.holding || jev[c.symbol]?.buying).length;

  const q = query.trim().toLowerCase();
  const rows = coins
    .filter((c) => (tab === "watch" ? watch.has(c.symbol) : tab === "jev" ? jev[c.symbol]?.holding || jev[c.symbol]?.buying : true))
    .filter((c) => !q || c.symbol.toLowerCase().includes(q) || c.name.toLowerCase().includes(q))
    .sort((a, b) => {
      if (sort.key === "rank") return rank[a.symbol] - rank[b.symbol];
      const x = value(a, sort.key);
      const y = value(b, sort.key);
      const d = typeof x === "string" ? x.localeCompare(y as string) : (x as number) - (y as number);
      return sort.desc ? -d : d;
    });

  const th = (key: SortKey, label: string, cls = "") => (
    <th className={`sortable ${cls}`} aria-sort={sort.key === key ? (sort.desc ? "descending" : "ascending") : "none"}>
      <button onClick={() => setSort((s) => ({ key, desc: s.key === key ? !s.desc : key !== "name" }))}>
        {label}
        <span className="caret" aria-hidden="true">{sort.key === key ? (sort.desc ? "▼" : "▲") : ""}</span>
      </button>
    </th>
  );

  return (
    <>
      <section className="headline">
        {HEADLINE.map((s) => market.bySymbol[s]).filter(Boolean).map((c) => (
          <button key={c.symbol} className="card index" onClick={() => open(c.symbol)}>
            <span className="index-top">
              <Coin symbol={c.symbol} name={c.name} />
              <span className={`pill ${tone(c.change_pct_24h) ?? ""}`}>{fmtPct(c.change_pct_24h)}</span>
            </span>
            <LivePrice value={fmtUsd(c.price)} dir={c.dir} seq={c.seq} className="index-price" />
            <Sparkline points={c.price !== null ? [...c.spark, c.price] : c.spark} width={220} height={40} baseline={c.open_24h} />
          </button>
        ))}
        <div className="card index breadth">
          <span className="index-top"><strong>Market today</strong><span className="muted small">{priced.length} coins</span></span>
          <div className="breadth-bar" role="img" aria-label={`${ups} up, ${downs} down`}>
            <span className="b-up" style={{ flex: ups || 0.0001 }} />
            <span className="b-down" style={{ flex: downs || 0.0001 }} />
          </div>
          <div className="breadth-legend small">
            <span className="up">▲ {ups} up</span>
            <span className="down">▼ {downs} down</span>
          </div>
          <div className="muted small">24h volume ${fmtCompact(totalVol)}</div>
        </div>
      </section>

      <div className="three">
        <Movers title="Top gainers" coins={gainers} open={open} metric="change" />
        <Movers title="Top losers" coins={losers} open={open} metric="change" />
        <Movers title="Most traded" coins={active} open={open} metric="volume" />
      </div>

      <section className="card list">
        <div className="card-head">
          <div className="seg" role="tablist" aria-label="Coin list">
            {([
              ["all", `All ${coins.length}`],
              ["watch", `★ Watchlist ${watch.size}`],
              ["jev", `Jev holds ${jevCount}`],
            ] as [ListTab, string][]).map(([id, label]) => (
              <button key={id} role="tab" aria-selected={tab === id} className={tab === id ? "on" : ""} onClick={() => setTab(id)}>
                {label}
              </button>
            ))}
          </div>
          <div className="list-tools">
            <input className="search" type="search" placeholder="Search coin" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search coin" />
            <select
              className="sort-pick"
              aria-label="Sort by"
              value={sort.key}
              onChange={(e) => setSort({ key: e.target.value as SortKey, desc: e.target.value !== "name" })}
            >
              {SORTS.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
            </select>
          </div>
        </div>
        {market.error && !coins.length ? (
          <p className="muted">Can't load prices: {market.error}</p>
        ) : rows.length === 0 ? (
          <p className="muted">
            {tab === "watch" ? "No coins on your watchlist yet. Tap ★ on any coin to add it." : tab === "jev" ? "Jev isn't holding or buying anything right now." : q ? `No coin matches "${query}".` : "Loading prices…"}
          </p>
        ) : (
          <div className="scroll tall">
            <table className="coins">
              <thead>
                <tr>
                  <th className="star-col" aria-label="Watchlist" />
                  {th("rank", "#", "num rank-col")}
                  {th("name", "Coin")}
                  {th("price", "Price", "num")}
                  {th("change", "24h", "num")}
                  <th className="num hide-sm">24h change</th>
                  {th("range", "24h range", "hide-md")}
                  {th("volume", "Volume 24h", "num hide-sm")}
                  <th className="hide-sm">Last 24h</th>
                  <th className="num hide-md" title="Jev's last hourly call: p(up) / p(down), in %">Jev</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((c) => (
                  <tr key={c.symbol} className="clickable" onClick={() => open(c.symbol)}>
                    <td className="star-col"><Star on={watch.has(c.symbol)} onClick={() => toggleWatch(c.symbol)} label={c.symbol} /></td>
                    <td className="num muted rank-col">{rank[c.symbol]}</td>
                    <td>
                      <a className="coin-link" href={`#/coin/${c.symbol.replace("/", "-")}`} onClick={(e) => e.preventDefault()}>
                        <Coin symbol={c.symbol} name={c.name} />
                      </a>
                    </td>
                    <td className="num"><LivePrice value={fmtUsd(c.price)} dir={c.dir} seq={c.seq} /></td>
                    <td className="num"><span className={`pill ${tone(c.change_pct_24h) ?? ""}`}>{fmtPct(c.change_pct_24h)}</span></td>
                    <td className={`num hide-sm ${tone(c.change_24h) ?? ""}`}>{fmtDelta(c.change_24h)}</td>
                    <td className="hide-md"><Range c={c} /></td>
                    <td className="num hide-sm">${fmtCompact(c.volume_usd_24h)}</td>
                    <td className="hide-sm"><Sparkline points={c.price !== null && c.spark.length ? [...c.spark, c.price] : c.spark} baseline={c.open_24h} /></td>
                    <td className="num hide-md"><JevBadge j={jev[c.symbol]} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="muted small list-note">
          Coinbase prices, live. 24h figures are rolling. The watchlist is kept in this browser.
        </p>
      </section>
    </>
  );
}
