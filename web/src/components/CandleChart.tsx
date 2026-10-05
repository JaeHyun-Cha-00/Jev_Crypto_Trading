import { useEffect, useRef, useState } from "react";
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineStyle,
  TickMarkType,
  createChart,
  createSeriesMarkers,
} from "lightweight-charts";
import type { IChartApi, IPriceLine, ISeriesApi, ISeriesMarkersPluginApi, SeriesMarker, Time, UTCTimestamp } from "lightweight-charts";
import { TIMEFRAMES, getCandles } from "../lib/api";
import type { Candle, Timeframe } from "../lib/api";
import { fmtCompact, fmtPrice, fmtTime } from "../lib/format";
import { feed, productId } from "../lib/live";

export interface ChartMark {
  /** UTC ms */
  ts: number;
  kind: "buy" | "sell";
  text: string;
}

export interface ChartLine {
  price: number;
  title: string;
  kind: "entry" | "stop";
}

const pad = (n: number) => String(n).padStart(2, "0");
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Axis labels in the viewer's local time; the chart's own clock is UTC seconds. */
function tickLabel(t: Time, type: TickMarkType): string {
  const d = new Date((t as number) * 1000);
  switch (type) {
    case TickMarkType.Year:
      return String(d.getFullYear());
    case TickMarkType.Month:
      return MONTHS[d.getMonth()];
    case TickMarkType.DayOfMonth:
      return String(d.getDate());
    default:
      return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
}

function priceFormat(p: number) {
  const precision = p >= 100 ? 2 : p >= 1 ? 4 : Math.min(10, Math.ceil(-Math.log10(p)) + 3);
  return { type: "price" as const, precision, minMove: 10 ** -precision };
}

function colors() {
  const css = getComputedStyle(document.documentElement);
  const v = (name: string) => css.getPropertyValue(name).trim();
  return { surface: v("--surface"), ink: v("--ink-2"), grid: v("--grid"), axis: v("--axis"), up: v("--up"), down: v("--down"), accent: v("--accent"), warning: v("--warning") };
}

/** "#078a55" -> "rgba(7, 138, 85, a)"; the chart draws on a canvas, which can't take color-mix(). */
function alpha(hex: string, a: number): string {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex);
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  return `rgba(${n >> 16}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}

const toBar = (c: Candle) => ({ time: (c.ts / 1000) as UTCTimestamp, open: c.open, high: c.high, low: c.low, close: c.close });

/** Coinbase candles for one coin, with volume, Jev's buys and sells, and a live last candle. */
export function CandleChart({
  symbol,
  timeframe,
  theme,
  marks,
  lines,
}: {
  symbol: string;
  timeframe: Timeframe;
  theme: string;
  marks: ChartMark[];
  lines: ChartLine[];
}) {
  const box = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const candles = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volume = useRef<ISeriesApi<"Histogram"> | null>(null);
  const markers = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const bars = useRef<Candle[]>([]);
  const [hover, setHover] = useState<Candle | null>(null);
  const [last, setLast] = useState<Candle | null>(null);
  const [state, setState] = useState<"loading" | "ok" | "error">("loading");
  const [error, setError] = useState<string>("");
  const sec = TIMEFRAMES.find((t) => t.id === timeframe)!.sec;

  useEffect(() => {
    if (!box.current) return;
    const c = createChart(box.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "transparent" }, fontSize: 11, attributionLogo: false },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { scaleMargins: { top: 0.08, bottom: 0.26 } },
      timeScale: { timeVisible: true, secondsVisible: false, tickMarkFormatter: tickLabel, rightOffset: 4 },
      localization: { timeFormatter: (t: Time) => fmtTime((t as number) * 1000) },
    });
    const s = c.addSeries(CandlestickSeries, { borderVisible: false });
    const v = c.addSeries(HistogramSeries, { priceFormat: { type: "volume" }, priceScaleId: "", lastValueVisible: false, priceLineVisible: false });
    v.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
    c.subscribeCrosshairMove((p) => {
      const t = p.time as number | undefined;
      setHover(t === undefined ? null : bars.current.find((b) => b.ts === t * 1000) ?? null);
    });
    chart.current = c;
    candles.current = s;
    volume.current = v;
    markers.current = createSeriesMarkers(s, []);
    return () => {
      c.remove();
      chart.current = null;
    };
  }, []);

  // Theme colours.
  useEffect(() => {
    const k = colors();
    chart.current?.applyOptions({
      layout: { textColor: k.ink },
      grid: { vertLines: { color: k.grid }, horzLines: { color: k.grid } },
      rightPriceScale: { borderColor: k.axis },
      timeScale: { borderColor: k.axis },
    });
    candles.current?.applyOptions({ upColor: k.up, downColor: k.down, wickUpColor: k.up, wickDownColor: k.down });
    if (bars.current.length) volume.current?.setData(volBars(bars.current));
  }, [theme]);

  const volBars = (bs: Candle[]) => {
    const k = colors();
    return bs.map((b) => ({
      time: (b.ts / 1000) as UTCTimestamp,
      value: b.volume,
      color: alpha(b.close >= b.open ? k.up : k.down, 0.4),
    }));
  };

  // Data, then live trades into the last candle.
  useEffect(() => {
    let alive = true;
    setState("loading");
    bars.current = [];
    setLast(null);
    getCandles(symbol, timeframe)
      .then((cs) => {
        if (!alive || !candles.current || !volume.current) return;
        bars.current = cs;
        if (cs.length) candles.current.applyOptions({ priceFormat: priceFormat(cs[cs.length - 1].close) });
        candles.current.setData(cs.map(toBar));
        volume.current.setData(volBars(cs));
        const n = cs.length;
        chart.current?.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 120), to: n + 4 });
        setLast(cs[n - 1] ?? null);
        setState("ok");
      })
      .catch((e) => {
        if (!alive) return;
        setError(e instanceof Error ? e.message : String(e));
        setState("error");
      });
    const stop = feed.watchMatches(productId(symbol), (m) => {
      const bs = bars.current;
      if (m.type === "last_match") return;   // already in the candles we loaded
      if (!bs.length || !candles.current || !volume.current) return;
      const price = Number(m.price);
      const size = Number(m.size);
      const t = Date.parse(m.time);
      const open = Math.floor(t / 1000 / sec) * sec * 1000;
      const lastBar = bs[bs.length - 1];
      if (open < lastBar.ts) return;
      let b: Candle;
      if (open === lastBar.ts) {
        b = { ...lastBar, high: Math.max(lastBar.high, price), low: Math.min(lastBar.low, price), close: price, volume: lastBar.volume + size };
        bs[bs.length - 1] = b;
      } else {
        b = { ts: open, open: lastBar.close, high: Math.max(lastBar.close, price), low: Math.min(lastBar.close, price), close: price, volume: size };
        bs.push(b);
      }
      candles.current.update(toBar(b));
      volume.current.update(volBars([b])[0]);
      setLast(b);
    });
    return () => {
      alive = false;
      stop();
    };
  }, [symbol, timeframe, sec]);

  // Jev's buys and sells, snapped to the candle they fall in.
  useEffect(() => {
    const k = colors();
    const ms: SeriesMarker<Time>[] = marks
      .map((m) => ({
        time: (Math.floor(m.ts / 1000 / sec) * sec) as UTCTimestamp,
        position: m.kind === "buy" ? ("belowBar" as const) : ("aboveBar" as const),
        shape: m.kind === "buy" ? ("arrowUp" as const) : ("arrowDown" as const),
        color: m.kind === "buy" ? k.up : k.down,
        text: m.text,
      }))
      .sort((a, b) => (a.time as number) - (b.time as number));
    markers.current?.setMarkers(ms);
  }, [marks, sec, theme, state]);

  // Entry and stop for a position Jev holds.
  useEffect(() => {
    const s = candles.current;
    if (!s) return;
    const k = colors();
    const made: IPriceLine[] = lines.map((l) =>
      s.createPriceLine({ price: l.price, title: l.title, color: l.kind === "stop" ? k.down : k.accent, lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: true }),
    );
    return () => made.forEach((p) => s.removePriceLine(p));
  }, [lines, theme]);

  const shown = hover ?? last;
  const chg = shown ? shown.close / shown.open - 1 : null;
  return (
    <div className="candle-wrap">
      <div className="ohlc small" aria-live="off">
        {shown ? (
          <>
            <span className="muted">{fmtTime(shown.ts)}</span>
            <span>O <b>{fmtPrice(shown.open)}</b></span>
            <span>H <b>{fmtPrice(shown.high)}</b></span>
            <span>L <b>{fmtPrice(shown.low)}</b></span>
            <span>C <b className={chg !== null && chg < 0 ? "down" : "up"}>{fmtPrice(shown.close)}</b></span>
            <span>Vol <b>{fmtCompact(shown.volume)}</b></span>
          </>
        ) : (
          <span className="muted">{state === "error" ? `Can't load candles: ${error}` : "Loading candles…"}</span>
        )}
      </div>
      <div className="candles" ref={box} />
    </div>
  );
}
