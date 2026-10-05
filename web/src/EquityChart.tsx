import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { CurvePoint } from "./api";
import { fmtMoney, fmtTime } from "./format";

const NARROW = 600;

function niceTicks(lo: number, hi: number, n = 4): number[] {
  if (hi <= lo) return [lo];
  const raw = (hi - lo) / n;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(v);
  return out;
}

/** Single-series equity line with a crosshair tooltip. One series, so no legend. */
export function EquityChart({ points, initial }: { points: CurvePoint[]; initial: number }) {
  const boxRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const fillId = useId();
  const [hover, setHover] = useState<number | null>(null);
  // Draw at the real pixel width so labels stay readable and the chart keeps some height on a phone.
  const [W, setW] = useState(900);
  const narrow = W < NARROW;
  const H = narrow ? 220 : 280;
  const PAD = { top: 12, right: 8, bottom: 28, left: narrow ? 52 : 64 };
  const hasData = points.length > 0;

  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(280, Math.round(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, [hasData]);

  const geo = useMemo(() => {
    if (points.length === 0) return null;
    const xs = points.map((p) => p.bar_ts);
    const ys = points.map((p) => p.equity);
    const x0 = xs[0];
    const x1 = xs[xs.length - 1] === x0 ? x0 + 1 : xs[xs.length - 1];
    let lo = Math.min(...ys, initial);
    let hi = Math.max(...ys, initial);
    const pad = Math.max((hi - lo) * 0.08, hi * 0.002);
    lo -= pad;
    hi += pad;
    const sx = (t: number) => PAD.left + ((t - x0) / (x1 - x0)) * (W - PAD.left - PAD.right);
    const sy = (v: number) => PAD.top + (1 - (v - lo) / (hi - lo)) * (H - PAD.top - PAD.bottom);
    const d = points.map((p, i) => `${i ? "L" : "M"}${sx(p.bar_ts).toFixed(1)},${sy(p.equity).toFixed(1)}`).join("");
    const area = `${d}L${sx(xs[xs.length - 1]).toFixed(1)},${H - PAD.bottom}L${sx(x0).toFixed(1)},${H - PAD.bottom}Z`;
    const yTicks = niceTicks(lo, hi);
    const xTicks = (narrow ? [0, 0.5, 1] : [0, 0.33, 0.66, 1]).map((f) => x0 + f * (x1 - x0));
    return { sx, sy, d, area, yTicks, xTicks, x0, x1 };
    // PAD, H and narrow all follow W.
  }, [points, initial, W]);

  if (!geo) return <p className="muted">No equity yet. Jev's account gets one point per logged hour.</p>;

  const onMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const rect = svgRef.current!.getBoundingClientRect();
    const x = ((e.clientX - rect.left) / rect.width) * W;
    const t = geo.x0 + ((x - PAD.left) / (W - PAD.left - PAD.right)) * (geo.x1 - geo.x0);
    let best = 0;
    for (let i = 1; i < points.length; i++)
      if (Math.abs(points[i].bar_ts - t) < Math.abs(points[best].bar_ts - t)) best = i;
    setHover(best);
  };

  const hp = hover !== null ? points[hover] : null;
  const tipLeft = hp ? (geo.sx(hp.bar_ts) / W) * 100 : 0;

  return (
    // Green when above the starting equity, red when below, like a price chart.
    <div ref={boxRef} className={`chart ${points[points.length - 1].equity >= initial ? "up" : "down"}`}>
      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label="Paper equity per bar"
        onPointerMove={onMove}
        onPointerLeave={() => setHover(null)}
      >
        <defs>
          <linearGradient id={fillId} x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopOpacity={0.18} className="stop" />
            <stop offset="100%" stopOpacity={0} className="stop" />
          </linearGradient>
        </defs>
        {geo.yTicks.map((v) => (
          <g key={v}>
            <line className="grid" x1={PAD.left} x2={W - PAD.right} y1={geo.sy(v)} y2={geo.sy(v)} />
            <text className="tick" x={PAD.left - 8} y={geo.sy(v)} textAnchor="end" dominantBaseline="middle">
              {fmtMoney(v, 0)}
            </text>
          </g>
        ))}
        <line className="ref" x1={PAD.left} x2={W - PAD.right} y1={geo.sy(initial)} y2={geo.sy(initial)} />
        {geo.xTicks.map((t, i) => (
          <text key={i} className="tick" x={geo.sx(t)} y={H - 8} textAnchor={i === 0 ? "start" : i === geo.xTicks.length - 1 ? "end" : "middle"}>
            {narrow ? fmtTime(t).slice(5) : fmtTime(t)}
          </text>
        ))}
        <path d={geo.area} fill={`url(#${fillId})`} stroke="none" />
        <path className="line" d={geo.d} />
        {hp && (
          <g>
            <line className="crosshair" x1={geo.sx(hp.bar_ts)} x2={geo.sx(hp.bar_ts)} y1={PAD.top} y2={H - PAD.bottom} />
            <circle className="dot" cx={geo.sx(hp.bar_ts)} cy={geo.sy(hp.equity)} r={4} />
          </g>
        )}
      </svg>
      {hp && (
        <div className="tooltip" style={{ left: `${tipLeft}%`, transform: `translateX(${tipLeft > 60 ? "-105%" : "5%"})` }}>
          <strong>{fmtMoney(hp.equity)}</strong>
          <span>{fmtTime(hp.bar_ts)}</span>
          {hp.cash !== undefined && <span>in coins {Math.max(0, (1 - hp.cash / hp.equity) * 100).toFixed(0)}%</span>}
        </div>
      )}
    </div>
  );
}
