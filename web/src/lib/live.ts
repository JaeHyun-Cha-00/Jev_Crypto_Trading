// Live prices: Coinbase's public websocket feed straight from the browser, with the
// API's /market/* routes as the baseline (sparklines, names) and the fallback when
// the socket is down. Nothing here needs keys or can place orders.

import { useEffect, useRef, useState } from "react";
import { getBook, getFills, getTickers } from "./api";
import type { Book, CoinTicker, Fill } from "./api";

const FEED_URL = "wss://ws-feed.exchange.coinbase.com";
const POLL_MS = 15_000;      // baseline refresh (sparklines, and prices if the socket is down)
const FALLBACK_POLL_MS = 5_000;
const FLUSH_MS = 500;        // batch socket ticks into one render

export const productId = (symbol: string) => symbol.replace("/", "-");
export const symbolOf = (product: string) => product.replace("-", "/");

export interface TickerMsg {
  product_id: string;
  price: string;
  open_24h: string;
  high_24h: string;
  low_24h: string;
  volume_24h: string;
  volume_30d?: string;
  best_bid?: string;
  best_ask?: string;
  time?: string;
}

export interface MatchMsg {
  type: "match" | "last_match";
  product_id: string;
  trade_id: number;
  price: string;
  size: string;
  /** maker side */
  side: "buy" | "sell";
  time: string;
}

export type FeedStatus = "connecting" | "live" | "down";

type Listener<T> = (m: T) => void;

/** One shared websocket. Reconnects with backoff; resubscribes whatever is still watched. */
class CoinbaseFeed {
  private ws: WebSocket | null = null;
  private tickers = new Map<string, number>();   // product -> watchers
  private matches = new Map<string, number>();
  private tickL = new Set<Listener<TickerMsg>>();
  private matchL = new Set<Listener<MatchMsg>>();
  private statusL = new Set<Listener<FeedStatus>>();
  private retry = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  status: FeedStatus = "connecting";

  private setStatus(s: FeedStatus) {
    this.status = s;
    this.statusL.forEach((f) => f(s));
  }

  private send(type: "subscribe" | "unsubscribe", channel: string, products: string[]) {
    if (products.length && this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type, product_ids: products, channels: [channel] }));
    }
  }

  private connect() {
    if (this.ws || typeof WebSocket === "undefined") return;
    this.setStatus("connecting");
    let ws: WebSocket;
    try {
      ws = new WebSocket(FEED_URL);
    } catch {
      this.setStatus("down");
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      this.retry = 0;
      this.setStatus("live");
      this.send("subscribe", "ticker_batch", [...this.tickers.keys()]);
      this.send("subscribe", "matches", [...this.matches.keys()]);
    };
    ws.onmessage = (ev) => {
      let m: { type?: string } & Record<string, unknown>;
      try {
        m = JSON.parse(ev.data as string);
      } catch {
        return;
      }
      if (m.type === "ticker") this.tickL.forEach((f) => f(m as unknown as TickerMsg));
      else if (m.type === "match" || m.type === "last_match") this.matchL.forEach((f) => f(m as unknown as MatchMsg));
    };
    ws.onclose = () => {
      this.ws = null;
      this.setStatus("down");
      if (this.tickers.size || this.matches.size) {
        const wait = Math.min(30_000, 1_000 * 2 ** this.retry++);
        this.timer = setTimeout(() => {
          this.timer = null;
          this.connect();
        }, wait);
      }
    };
    ws.onerror = () => ws.close();
  }

  private watch(map: Map<string, number>, channel: string, products: string[]) {
    const added = products.filter((p) => {
      map.set(p, (map.get(p) ?? 0) + 1);
      return map.get(p) === 1;
    });
    if (!this.ws && !this.timer) this.connect();
    else this.send("subscribe", channel, added);
    return () => {
      const gone = products.filter((p) => {
        const n = (map.get(p) ?? 1) - 1;
        if (n <= 0) map.delete(p);
        else map.set(p, n);
        return n <= 0;
      });
      this.send("unsubscribe", channel, gone);
    };
  }

  watchTickers(products: string[], f: Listener<TickerMsg>) {
    this.tickL.add(f);
    const stop = this.watch(this.tickers, "ticker_batch", products);
    return () => {
      this.tickL.delete(f);
      stop();
    };
  }

  watchMatches(product: string, f: Listener<MatchMsg>) {
    const g: Listener<MatchMsg> = (m) => m.product_id === product && f(m);
    this.matchL.add(g);
    const stop = this.watch(this.matches, "matches", [product]);
    return () => {
      this.matchL.delete(g);
      stop();
    };
  }

  onStatus(f: Listener<FeedStatus>) {
    this.statusL.add(f);
    return () => void this.statusL.delete(f);
  }
}

export const feed = new CoinbaseFeed();

export function useFeedStatus(): FeedStatus {
  const [s, setS] = useState<FeedStatus>(feed.status);
  useEffect(() => feed.onStatus(setS), []);
  return s;
}

export interface LiveCoin extends CoinTicker {
  best_bid: number | null;
  best_ask: number | null;
  /** direction of the last price change, for the flash */
  dir: 1 | -1 | 0;
  /** bumps on every price change */
  seq: number;
}

export interface LiveMarket {
  coins: LiveCoin[];
  bySymbol: Record<string, LiveCoin>;
  status: FeedStatus;
  error: string | null;
  updatedAt: number | null;
}

const num = (v: string | undefined) => (v === undefined || v === "" ? null : Number(v));

function applyTick(c: LiveCoin, t: TickerMsg): LiveCoin {
  const price = Number(t.price);
  const open = num(t.open_24h);
  const vol = num(t.volume_24h);
  const change = open ? price - open : c.change_24h;
  return {
    ...c,
    price,
    open_24h: open ?? c.open_24h,
    high_24h: num(t.high_24h) ?? c.high_24h,
    low_24h: num(t.low_24h) ?? c.low_24h,
    change_24h: change,
    change_pct_24h: open ? price / open - 1 : c.change_pct_24h,
    volume_24h: vol ?? c.volume_24h,
    volume_usd_24h: vol !== null ? vol * price : c.volume_usd_24h,
    volume_30d: num(t.volume_30d) ?? c.volume_30d,
    best_bid: num(t.best_bid) ?? c.best_bid,
    best_ask: num(t.best_ask) ?? c.best_ask,
    dir: c.price === null || price === c.price ? c.dir : price > c.price ? 1 : -1,
    seq: c.price === price ? c.seq : c.seq + 1,
  };
}

/** Every tracked coin, updated by the socket in real time and by the API every 15 seconds. */
export function useLiveMarket(): LiveMarket {
  const [state, setState] = useState<LiveMarket>({ coins: [], bySymbol: {}, status: feed.status, error: null, updatedAt: null });
  const status = useFeedStatus();
  const pending = useRef(new Map<string, TickerMsg>());
  const lastTick = useRef(new Map<string, number>());
  const [symbols, setSymbols] = useState<string[]>([]);

  // Baseline from the API; faster while the socket is down.
  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const t = await getTickers();
        if (!alive) return;
        setState((s) => {
          const bySymbol: Record<string, LiveCoin> = {};
          for (const c of t.coins) {
            const prev = s.bySymbol[c.symbol];
            // Keep socket prices that are newer than the API's 5-second cache.
            const fresh = prev && Date.now() - (lastTick.current.get(c.symbol) ?? 0) < 20_000;
            bySymbol[c.symbol] = fresh
              ? { ...prev, name: c.name, spark: c.spark }
              : {
                  ...c,
                  best_bid: prev?.best_bid ?? null,
                  best_ask: prev?.best_ask ?? null,
                  dir: prev && prev.price !== null && c.price !== null && c.price !== prev.price ? (c.price > prev.price ? 1 : -1) : prev?.dir ?? 0,
                  seq: prev ? prev.seq + (prev.price !== c.price ? 1 : 0) : 0,
                };
          }
          return { ...s, bySymbol, coins: t.coins.map((c) => bySymbol[c.symbol]), error: t.error, updatedAt: Date.now() };
        });
        setSymbols((old) => (old.length === t.coins.length ? old : t.coins.map((c) => c.symbol)));
      } catch (e) {
        if (alive) setState((s) => ({ ...s, error: e instanceof Error ? e.message : String(e) }));
      }
    };
    load();
    const id = setInterval(load, status === "live" ? POLL_MS : FALLBACK_POLL_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [status]);

  // Socket ticks, batched.
  useEffect(() => {
    if (!symbols.length) return;
    const stop = feed.watchTickers(symbols.map(productId), (t) => pending.current.set(symbolOf(t.product_id), t));
    const id = setInterval(() => {
      if (!pending.current.size) return;
      const batch = pending.current;
      pending.current = new Map();
      setState((s) => {
        const bySymbol = { ...s.bySymbol };
        for (const [sym, t] of batch) {
          if (!bySymbol[sym]) continue;
          bySymbol[sym] = applyTick(bySymbol[sym], t);
          lastTick.current.set(sym, Date.now());
        }
        return { ...s, bySymbol, coins: s.coins.map((c) => bySymbol[c.symbol]), updatedAt: Date.now() };
      });
    }, FLUSH_MS);
    return () => {
      stop();
      clearInterval(id);
    };
  }, [symbols]);

  return { ...state, status };
}

/** Recent fills for one coin: the API's last 40, then each new one from the socket. */
export function useFills(symbol: string): Fill[] {
  const [fills, setFills] = useState<Fill[]>([]);
  useEffect(() => {
    let alive = true;
    setFills([]);
    getFills(symbol)
      .then((f) => alive && setFills((cur) => mergeFills(cur, f)))
      .catch(() => undefined);
    const stop = feed.watchMatches(productId(symbol), (m) => {
      const f: Fill = { id: m.trade_id, time: m.time, price: Number(m.price), size: Number(m.size), side: m.side === "buy" ? "sell" : "buy" };
      setFills((cur) => mergeFills(cur, [f]));
    });
    return () => {
      alive = false;
      stop();
    };
  }, [symbol]);
  return fills;
}

function mergeFills(a: Fill[], b: Fill[]): Fill[] {
  const seen = new Set<number | null>();
  return [...a, ...b]
    .filter((f) => (f.id === null ? true : seen.has(f.id) ? false : (seen.add(f.id), true)))
    .sort((x, y) => (y.id ?? 0) - (x.id ?? 0) || y.time.localeCompare(x.time))
    .slice(0, 60);
}

/** The order book, re-read every 2 seconds while the tab is visible. */
export function useBook(symbol: string): { book: Book | null; error: string | null } {
  const [book, setBook] = useState<Book | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    setBook(null);
    const load = () => {
      if (document.hidden) return;
      getBook(symbol)
        .then((b) => {
          if (!alive) return;
          setBook(b);
          setError(null);
        })
        .catch((e) => alive && setError(e instanceof Error ? e.message : String(e)));
    };
    load();
    const id = setInterval(load, 2_000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [symbol]);
  return { book, error };
}

/** Coins the viewer starred, kept in this browser only. */
export function useWatchlist(): [Set<string>, (symbol: string) => void] {
  const [list, setList] = useState<Set<string>>(() => {
    try {
      const raw = localStorage.getItem("watchlist");
      if (raw) return new Set(JSON.parse(raw) as string[]);
    } catch {
      /* storage blocked */
    }
    return new Set(["BTC/USD", "ETH/USD", "SOL/USD"]);
  });
  const toggle = (symbol: string) =>
    setList((cur) => {
      const next = new Set(cur);
      if (next.has(symbol)) next.delete(symbol);
      else next.add(symbol);
      try {
        localStorage.setItem("watchlist", JSON.stringify([...next]));
      } catch {
        /* storage blocked */
      }
      return next;
    });
  return [list, toggle];
}
