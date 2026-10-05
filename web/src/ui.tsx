// Small pieces shared by the market pages and Jev's portfolio.

export function tone(v: number | null | undefined): "up" | "down" | undefined {
  return v === null || v === undefined || v === 0 ? undefined : v > 0 ? "up" : "down";
}

const COIN_COLORS: Record<string, string> = {
  BTC: "#f7931a", ETH: "#627eea", SOL: "#9945ff", XRP: "#23292f", DOGE: "#c2a633", ADA: "#0033ad", LTC: "#345d9d",
};

export function coinColor(base: string): string {
  const hue = [...base].reduce((h, c) => h + c.charCodeAt(0) * 37, 0) % 360;
  return COIN_COLORS[base] ?? `hsl(${hue} 55% 48%)`;
}

/** A round badge for the base asset, the way coin apps mark a market. */
export function Coin({ symbol, showQuote = true, name, size }: { symbol: string; showQuote?: boolean; name?: string; size?: number }) {
  const [base, quote] = symbol.split("/");
  const style = { background: coinColor(base), ...(size ? { width: size, height: size, fontSize: size * 0.45 } : {}) };
  return (
    <span className="coin-cell">
      <span className="coin" style={style} aria-hidden="true">
        {base.slice(0, 1)}
      </span>
      {name ? (
        <span className="coin-names">
          <span className="coin-name">{name}</span>
          <span className="coin-ticker">{base}</span>
        </span>
      ) : (
        <span>
          {base}
          {showQuote && quote && <span className="coin-quote">/{quote}</span>}
        </span>
      )}
    </span>
  );
}

/** A tiny line of recent closes, green if it ends above where it started. */
export function Sparkline({ points, width = 96, height = 30, baseline }: { points: number[]; width?: number; height?: number; baseline?: number | null }) {
  if (points.length < 2) return <span className="spark empty" style={{ width, height }} aria-hidden="true" />;
  const lo = Math.min(...points, baseline ?? Infinity);
  const hi = Math.max(...points, baseline ?? -Infinity);
  const span = hi - lo || 1;
  const x = (i: number) => (i / (points.length - 1)) * width;
  const y = (v: number) => 2 + (1 - (v - lo) / span) * (height - 4);
  const ref = baseline ?? points[0];
  const up = points[points.length - 1] >= ref;
  return (
    <svg className={`spark ${up ? "up" : "down"}`} width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden="true">
      <line x1="0" x2={width} y1={y(ref)} y2={y(ref)} className="spark-ref" />
      <polyline points={points.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ")} />
    </svg>
  );
}

export function Star({ on, onClick, label }: { on: boolean; onClick: () => void; label: string }) {
  return (
    <button
      className={`star ${on ? "on" : ""}`}
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      aria-pressed={on}
      aria-label={on ? `Remove ${label} from watchlist` : `Add ${label} to watchlist`}
      title={on ? "Remove from watchlist" : "Add to watchlist"}
    >
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M12 3.5l2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.9l-5.2 2.7 1-5.8-4.3-4.1 5.9-.9z" />
      </svg>
    </button>
  );
}

/** A price that flashes green or red each time it changes. */
export function LivePrice({ value, dir, seq, className = "" }: { value: string; dir: number; seq: number; className?: string }) {
  return (
    <span key={seq} className={`live-price ${seq > 0 ? (dir > 0 ? "flash-up" : dir < 0 ? "flash-down" : "") : ""} ${className}`}>
      {value}
    </span>
  );
}
