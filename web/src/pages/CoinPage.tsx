import { useEffect, useMemo, useState } from "react";
import { TIMEFRAMES, getForwardRows } from "../lib/api";
import type { Book, ForwardRow, JevPaper, Timeframe } from "../lib/api";
import { CandleChart } from "../components/CandleChart";
import type { ChartLine, ChartMark } from "../components/CandleChart";
import { TZ, fmtClock, fmtCompact, fmtDelta, fmtMoney, fmtSize, fmtPct, fmtPrice, fmtTime, fmtUsd } from "../lib/format";
import { useBook, useFills } from "../lib/live";
import type { FeedStatus, LiveCoin } from "../lib/live";
import { Coin, LivePrice, Star, tone } from "../components/ui";

// One coin, broker-app style: live price, candles, order book, trade tape, and
// everything Jev has said and done about it.

function OrderBook({ book, error, last }: { book: Book | null; error: string | null; last: number | null }) {
  if (!book) return <p className="muted small">{error ? `Can't load the order book: ${error}` : "Loading order book…"}</p>;
  const rows = 10;
  const asks = book.asks.slice(0, rows);
  const bids = book.bids.slice(0, rows);
  const cum = (xs: [number, number][]) => xs.reduce<number[]>((a, [, s]) => [...a, (a[a.length - 1] ?? 0) + s], []);
  const ca = cum(asks);
  const cb = cum(bids);
  const max = Math.max(ca[ca.length - 1] ?? 0, cb[cb.length - 1] ?? 0) || 1;
  const bidTotal = cb[cb.length - 1] ?? 0;
  const askTotal = ca[ca.length - 1] ?? 0;
  const line = (side: "ask" | "bid", [p, s]: [number, number], c: number) => (
    <div className={`book-row ${side}`} key={`${side}-${p}`}>
      <span className="depth" style={{ width: `${(c / max) * 100}%` }} />
      <span className={`num ${side === "ask" ? "down" : "up"}`}>{fmtPrice(p)}</span>
      <span className="num">{fmtSize(s)}</span>
      <span className="num muted">{fmtCompact(p * s)}</span>
    </div>
  );
  return (
    <div className="book">
      <div className="book-row head muted small"><span>Price</span><span className="num">Size</span><span className="num">$</span></div>
      {asks.map((a, i) => ({ a, c: ca[i] })).reverse().map(({ a, c }) => line("ask", a, c))}
      <div className="book-mid">
        <strong className="num">{fmtPrice(last ?? book.mid)}</strong>
        <span className="muted small">spread {book.spread !== null ? fmtPrice(book.spread) : "n/a"} ({fmtPct(book.spread_pct, false)})</span>
      </div>
      {bids.map((b, i) => line("bid", b, cb[i]))}
      <div className="book-balance small" title="Size within the levels shown">
        <span className="up">Bids {Math.round((bidTotal / (bidTotal + askTotal || 1)) * 100)}%</span>
        <span className="bal-bar"><span style={{ width: `${(bidTotal / (bidTotal + askTotal || 1)) * 100}%` }} /></span>
        <span className="down">{Math.round((askTotal / (bidTotal + askTotal || 1)) * 100)}% Asks</span>
      </div>
    </div>
  );
}

function Tape({ symbol }: { symbol: string }) {
  const fills = useFills(symbol);
  if (!fills.length) return <p className="muted small">Waiting for trades…</p>;
  return (
    <div className="tape">
      <div className="tape-row head muted small"><span>Time ({TZ})</span><span className="num">Price</span><span className="num">Size</span></div>
      {fills.slice(0, 30).map((f) => (
        <div className={`tape-row ${f.side}`} key={`${f.id}-${f.time}`}>
          <span className="muted">{fmtClock(Date.parse(f.time))}</span>
          <span className={`num ${f.side === "buy" ? "up" : "down"}`}>{fmtPrice(f.price)}</span>
          <span className="num">{fmtSize(f.size)}</span>
        </div>
      ))}
    </div>
  );
}

const CALL_LABEL: Record<string, string> = { up: "Up", flat: "Flat", down: "Down" };
const ACTION_LABEL: Record<string, string> = { enter: "buy", exit: "sell", hold: "hold", skip: "pass" };

function JevPanel({ symbol, coin, paper, rows }: { symbol: string; coin?: LiveCoin; paper: JevPaper | null; rows: ForwardRow[] }) {
  const base = symbol.split("/")[0];
  const pos = paper?.positions.find((p) => p.symbol === symbol);
  const pending = paper?.pending.find((p) => p.symbol === symbol && p.kind === "enter");
  const last = paper?.actions.find((a) => a.symbol === symbol);
  const trades = (paper?.trades ?? []).filter((t) => t.symbol === symbol);
  const stats = paper?.per_symbol[symbol];
  const price = coin?.price ?? pos?.mark ?? null;
  const live = pos && price !== null ? pos.qty * (price - pos.entry_price) : null;
  const scored = rows.filter((r) => r.hit !== null);
  const hits = scored.filter((r) => r.hit).length;

  return (
    <section className="card">
      <div className="card-head">
        <h2>Jev on {base}</h2>
        <span className="muted small">Simulated portfolio, no real orders</span>
      </div>
      <div className="tiles inner">
        <div className="tile">
          <div className="tile-label">Position</div>
          {pos ? (
            <>
              <div className="tile-value">${fmtMoney(pos.qty * (price ?? pos.mark), 0)}</div>
              <div className="tile-sub">{pos.qty.toPrecision(4)} {base} @ {fmtPrice(pos.entry_price)}</div>
            </>
          ) : (
            <>
              <div className="tile-value">{pending ? "Buying" : "None"}</div>
              <div className="tile-sub">{pending ? `${fmtPct(pending.size_frac, false)} of equity next hour` : "Jev isn't holding it"}</div>
            </>
          )}
        </div>
        <div className="tile">
          <div className="tile-label">Unrealized P&amp;L (live)</div>
          <div className={`tile-value ${tone(live) ?? ""}`}>{live === null ? "–" : fmtMoney(live)}</div>
          <div className="tile-sub">{pos && price !== null ? `${fmtPct(price / pos.entry_price - 1)} · stop ${fmtPrice(pos.stop_price)}` : "before trading costs"}</div>
        </div>
        <div className="tile">
          <div className="tile-label">Last call</div>
          <div className="tile-value">
            {last && last.status === "answered" ? (
              <><span className="up">{Math.round(last.p_up * 100)}%</span> <span className="muted small">up</span></>
            ) : "–"}
          </div>
          <div className="tile-sub">
            {last ? `${fmtTime(last.bar_ts)} · p(down) ${last.status === "answered" ? Math.round(last.p_down * 100) + "%" : "–"} · ${ACTION_LABEL[last.action]}` : "no call yet"}
          </div>
        </div>
        <div className="tile">
          <div className="tile-label">Closed trades</div>
          <div className={`tile-value ${tone(stats?.pnl) ?? ""}`}>{stats ? fmtMoney(stats.pnl) : "–"}</div>
          <div className="tile-sub">{trades.length} trade{trades.length === 1 ? "" : "s"} · {stats?.buys ?? 0} buys</div>
        </div>
        <div className="tile">
          <div className="tile-label">Direction calls right</div>
          <div className="tile-value">{scored.length ? fmtPct(hits / scored.length, false) : "–"}</div>
          <div className="tile-sub">{hits} of {scored.length} scored</div>
        </div>
      </div>

      <h3>Hourly calls</h3>
      {rows.length === 0 ? (
        <p className="muted small">No calls on {base} logged yet.</p>
      ) : (
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>Hour ({TZ})</th><th className="num">Close</th><th>Call</th><th className="num">Confidence</th>
                <th className="num">p(−3%)</th><th className="num">Moved</th><th>Result</th>
              </tr>
            </thead>
            <tbody>
              {rows.slice(0, 48).map((r) => (
                <tr key={`${r.candle_ts}-${r.called_at}`}>
                  <td>{fmtTime(r.candle_ts)}</td>
                  <td className="num">{fmtPrice(r.close)}</td>
                  <td>{r.status === "answered" && r.predicted ? <span className={`tag ${r.predicted === "up" ? "buy" : r.predicted === "down" ? "sell" : ""}`}>{CALL_LABEL[r.predicted]}</span> : <span className="muted">{r.status}</span>}</td>
                  <td className="num">{r.confidence === null ? "–" : `${Math.round(r.confidence * 100)}%`}</td>
                  <td className="num">{r.p_adverse === null ? "–" : `${Math.round(r.p_adverse * 100)}%`}</td>
                  <td className={`num ${tone(r.outcome?.ret_pct) ?? ""}`}>{r.outcome ? `${r.outcome.ret_pct > 0 ? "+" : ""}${r.outcome.ret_pct.toFixed(2)}%` : "pending"}</td>
                  <td>{r.hit === null ? <span className="muted">–</span> : <span className={`tag ${r.hit ? "hit" : "miss"}`}>{r.hit ? "right" : "wrong"}</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export function CoinPage({
  symbol,
  coin,
  status,
  paper,
  starred,
  toggleStar,
  theme,
  back,
}: {
  symbol: string;
  coin?: LiveCoin;
  status: FeedStatus;
  paper: JevPaper | null;
  starred: boolean;
  toggleStar: () => void;
  theme: string;
  back: () => void;
}) {
  const [tf, setTf] = useState<Timeframe>(() => {
    try {
      const v = localStorage.getItem("timeframe");
      if (TIMEFRAMES.some((t) => t.id === v)) return v as Timeframe;
    } catch {
      /* storage blocked */
    }
    return "1h";
  });
  const pickTf = (t: Timeframe) => {
    setTf(t);
    try {
      localStorage.setItem("timeframe", t);
    } catch {
      /* storage blocked */
    }
  };
  const { book, error: bookError } = useBook(symbol);
  const [side, setSide] = useState<"book" | "trades">("book");
  const [rows, setRows] = useState<ForwardRow[]>([]);

  useEffect(() => {
    let alive = true;
    setRows([]);
    const load = () => getForwardRows({ symbol, limit: 200 }).then((r) => alive && setRows(r)).catch(() => undefined);
    load();
    const id = setInterval(load, 60_000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [symbol]);

  useEffect(() => {
    const base = symbol.split("/")[0];
    document.title = coin?.price != null ? `${base} ${fmtUsd(coin.price)} · Jev` : `${base} · Jev`;
    return () => {
      document.title = "Jev";
    };
  }, [symbol, coin?.price]);

  const pos = paper?.positions.find((p) => p.symbol === symbol);
  const marks = useMemo<ChartMark[]>(() => {
    const out: ChartMark[] = [];
    for (const t of paper?.trades ?? []) {
      if (t.symbol !== symbol) continue;
      out.push({ ts: t.entry_ts, kind: "buy", text: "Jev buy" });
      out.push({ ts: t.exit_ts, kind: "sell", text: `Jev sell ${fmtPct(t.ret)}` });
    }
    if (pos) out.push({ ts: pos.entry_ts, kind: "buy", text: "Jev buy" });
    return out;
  }, [paper, symbol, pos]);
  const lines = useMemo<ChartLine[]>(
    () => (pos ? [{ price: pos.entry_price, title: "Jev entry", kind: "entry" }, { price: pos.stop_price, title: "Stop", kind: "stop" }] : []),
    [pos?.entry_price, pos?.stop_price],
  );

  const c = coin;
  const base = symbol.split("/")[0];
  return (
    <>
      <div className="coin-nav">
        <button className="back" onClick={back}>
          <span aria-hidden="true">←</span> Market
        </button>
      </div>

      <section className="card coin-head">
        <div className="coin-title">
          <Coin symbol={symbol} name={c?.name ?? base} size={40} />
          <Star on={starred} onClick={toggleStar} label={symbol} />
          {pos && <span className="tag enter">Jev holding</span>}
        </div>
        <div className="coin-quote-row">
          <div>
            <LivePrice value={fmtUsd(c?.price)} dir={c?.dir ?? 0} seq={c?.seq ?? 0} className="big-price" />
            <div className="deltas">
              <span className={`pill ${tone(c?.change_pct_24h) ?? ""}`}>{fmtPct(c?.change_pct_24h)}</span>
              <span className={tone(c?.change_24h) ?? ""}>
                {c?.change_24h == null ? "" : fmtDelta(c.change_24h)}
              </span>
              <span>24h</span>
              <span className={`live-dot ${status === "live" ? "on" : ""}`} title={status === "live" ? "Streaming from Coinbase" : "Refreshing every few seconds"}>
                {status === "live" ? "Live" : "Delayed"}
              </span>
            </div>
          </div>
          <dl className="quote-stats">
            <div><dt>Bid</dt><dd className="num up">{fmtPrice(c?.best_bid ?? book?.bids[0]?.[0] ?? null)}</dd></div>
            <div><dt>Ask</dt><dd className="num down">{fmtPrice(c?.best_ask ?? book?.asks[0]?.[0] ?? null)}</dd></div>
            <div><dt>24h high</dt><dd className="num">{fmtPrice(c?.high_24h)}</dd></div>
            <div><dt>24h low</dt><dd className="num">{fmtPrice(c?.low_24h)}</dd></div>
            <div><dt>24h open</dt><dd className="num">{fmtPrice(c?.open_24h)}</dd></div>
            <div><dt>Volume 24h</dt><dd className="num">{fmtCompact(c?.volume_24h)} {base}</dd></div>
            <div><dt>Volume 24h ($)</dt><dd className="num">${fmtCompact(c?.volume_usd_24h)}</dd></div>
            <div><dt>Volume 30d</dt><dd className="num">{fmtCompact(c?.volume_30d)} {base}</dd></div>
          </dl>
        </div>
      </section>

      <div className="coin-grid">
        <section className="card chart-card">
          <div className="card-head">
            <h2>Chart</h2>
            <div className="seg" role="group" aria-label="Candle size">
              {TIMEFRAMES.map((t) => (
                <button key={t.id} className={tf === t.id ? "on" : ""} onClick={() => pickTf(t.id)} aria-pressed={tf === t.id}>
                  {t.label}
                </button>
              ))}
            </div>
          </div>
          <CandleChart symbol={symbol} timeframe={tf} theme={theme} marks={marks} lines={lines} />
          <p className="muted small">Coinbase {symbol} candles in your local time. Arrows mark Jev's buys and sells.</p>
        </section>
        <section className="card book-card">
          <div className="seg wide" role="tablist" aria-label="Order book or trades">
            <button role="tab" aria-selected={side === "book"} className={side === "book" ? "on" : ""} onClick={() => setSide("book")}>Order book</button>
            <button role="tab" aria-selected={side === "trades"} className={side === "trades" ? "on" : ""} onClick={() => setSide("trades")}>Trades</button>
          </div>
          {side === "book" ? <OrderBook book={book} error={bookError} last={c?.price ?? null} /> : <Tape symbol={symbol} />}
        </section>
      </div>

      <JevPanel symbol={symbol} coin={c} paper={paper} rows={rows} />
    </>
  );
}
