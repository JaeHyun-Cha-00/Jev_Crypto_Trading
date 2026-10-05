import { useMemo, useRef, useState } from "react";
import type { EquityPoint } from "./api";
import { fmtMoney, fmtTime } from "./format";

const W = 900;
const H = 260;
const PAD = { top: 12, right: 16, bottom: 28, left: 72 };

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
export function EquityChart({ points, initial }: { points: EquityPoint[]; initial: number }) {
  const svgRef = useRef<SVGSVGElement>(null);
  const [hover, setHover] = useState<number | null>(null);

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
    const yTicks = niceTicks(lo, hi);
    const xTicks = [0, 0.33, 0.66, 1].map((f) => x0 + f * (x1 - x0));
    return { sx, sy, d, yTicks, xTicks, x0, x1 };
  }, [points, initial]);

  if (!geo) return <p className="muted">No equity yet. The paper loop records one point per closed bar.</p>;

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
    <div className="chart">
      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label="Paper equity per bar"
        onPointerMove={onMove}
        onPointerLeave={() => setHover(null)}
      >
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
          <text key={i} className="tick" x={geo.sx(t)} y={H - 8} textAnchor={i === 0 ? "start" : i === 3 ? "end" : "middle"}>
            {fmtTime(t)}
          </text>
        ))}
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
          <span>exposure {(hp.exposure * 100).toFixed(0)}%{hp.mode === "risk_only" ? ", risk-only catch-up" : ""}</span>
        </div>
      )}
    </div>
  );
}
