import type { Direction, ForwardMetrics, ForwardRow, ForwardSummary } from "../lib/api";
import { TZ, ago, fmtPct, fmtTime } from "../lib/format";

// Jev's hourly forward calls (collect workflow, data-log branch) against what happened.

const pct0 = (v: number | null | undefined) => (v == null ? "n/a" : `${Math.round(v * 100)}%`);
const fmtUsd = (v: number) => `$${v < 1 ? v.toFixed(4) : v.toFixed(2)}`;
const num = (v: number | null | undefined, d = 3) => (v == null ? "n/a" : v.toFixed(d));

function Bars({ m }: { m: ForwardMetrics }) {
  const { majority, always_flat } = m.baselines;
  const items = [
    { label: "Jev", value: m.hit_rate, jev: true },
    { label: majority.label === "flat" ? "Always flat (also the most common)" : "Always flat", value: always_flat.hit_rate, jev: false },
    // The most common realized class, known only in hindsight.
    ...(majority.label && majority.label !== "flat"
      ? [{ label: `Always ${majority.label} (most common)`, value: majority.hit_rate, jev: false }]
      : []),
  ];
  return (
    <div className="bars" role="img" aria-label={items.map((i) => `${i.label} ${pct0(i.value)}`).join(", ")}>
      {items.map((i) => (
        <div className="bar-row" key={i.label} title={`${i.label}: ${pct0(i.value)} of ${m.scored} scored calls`}>
          <span className="bar-label">{i.label}</span>
          <span className="bar-track">
            <span className={`bar-fill ${i.jev ? "jev" : ""}`} style={{ width: `${(i.value ?? 0) * 100}%` }} />
          </span>
          <span className="bar-value num">{pct0(i.value)}</span>
        </div>
      ))}
    </div>
  );
}

function Confusion({ m }: { m: ForwardMetrics }) {
  const { labels, matrix } = m.confusion;
  const max = Math.max(1, ...matrix.flat());
  return (
    <table className="matrix">
      <caption className="muted small">Rows: Jev's call. Columns: what happened.</caption>
      <thead>
        <tr><th /> {labels.map((l) => <th key={l} className="num">{l}</th>)}<th className="num">calls</th></tr>
      </thead>
      <tbody>
        {labels.map((p, i) => (
          <tr key={p}>
            <th scope="row">{p}</th>
            {labels.map((a, j) => {
              const n = matrix[i][j];
              return (
                <td
                  key={a}
                  className={`num ${i === j ? "diag" : ""}`}
                  style={{ background: n ? `color-mix(in srgb, var(--series-1) ${Math.round((n / max) * 45)}%, transparent)` : undefined }}
                  title={`called ${p}, was ${a}: ${n}`}
                >
                  {n}
                </td>
              );
            })}
            <td className="num muted">{matrix[i].reduce((s, v) => s + v, 0)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Calibration({ m }: { m: ForwardMetrics }) {
  return (
    <table>
      <thead>
        <tr><th>Confidence</th><th className="num">Calls</th><th className="num">Avg conf.</th><th className="num">Hit rate</th></tr>
      </thead>
      <tbody>
        {m.calibration.map((b) => (
          <tr key={b.lo}>
            <td>{b.lo === 0 ? `< ${pct0(b.hi)}` : `${pct0(b.lo)}–${pct0(b.hi)}`}</td>
            <td className="num">{b.n}</td>
            <td className="num">{b.n ? pct0(b.mean_confidence) : "–"}</td>
            <td className="num">{b.n ? pct0(b.hit_rate) : "–"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const HOUR_MS = 3_600_000;

function Result({ r, horizon }: { r: ForwardRow; horizon: number }) {
  if (r.status !== "answered") return <span className="tag">{r.status}</span>;
  if (r.hit === null) return <span className="muted">result {fmtTime(r.candle_ts + (horizon + 1) * HOUR_MS).slice(5)}</span>;
  return r.hit ? <span className="tag hit">✓ hit</span> : <span className="tag miss">✗ miss</span>;
}

const dirLabel = (d: Direction | null | undefined) => d ?? "–";

export function ForwardLog({ summary, rows, symbol, error, horizon }: {
  summary: ForwardSummary | null;
  rows: ForwardRow[];
  symbol: string;
  error: string | null;
  /** hours each call looks ahead (decision.horizon_bars) */
  horizon: number;
}) {
  const m = summary ? (symbol ? summary.per_symbol[symbol] : summary.overall) : null;
  const shown = rows.filter((r) => !symbol || r.symbol === symbol).slice(0, 100);
  const best = m ? Math.max(m.baselines.always_flat.hit_rate ?? 0, m.baselines.majority.hit_rate ?? 0) : 0;
  const beats = m?.hit_rate != null && m.hit_rate > best;
  const ds = m?.direction_scores;
  const as = m?.adverse_move_scores;
  // A call is scored once the candle `horizon` hours after it has closed, i.e. horizon + 1 hours after its own candle opened.
  const firstScoredAt = m?.oldest_pending_ts != null ? m.oldest_pending_ts + (horizon + 1) * HOUR_MS : null;

  return (
    <section className="card">
      <h2>Jev's accuracy</h2>
      <p className="muted small">
        {summary?.source === "off"
          ? "Turned off (forward_log.source: off)."
          : <>Jev's hourly calls, each scored once the {horizon} hours it asked about have passed.
            {" "}Last call {ago(summary?.last_called_at ?? null).text}.</>}
      </p>
      {(error || summary?.error) && (
        <div className="banner small" role="alert">
          <span aria-hidden="true">▲</span> Couldn't refresh the forward log: {error ?? summary?.error}
          {summary?.overall.decisions ? ". Showing the last data loaded." : "."}
        </div>
      )}

      {!m || m.decisions === 0 ? (
        <p className="muted">{symbol && summary?.overall.decisions ? `No calls for ${symbol}.` : "No forward calls yet."}</p>
      ) : (
        <>
          <div className="tiles inner">
            <div className="tile">
              <div className="tile-label">Scored calls</div>
              <div className="tile-value">{m.scored}</div>
              <div className="tile-sub">{m.pending} pending · {m.abstain} abstain · {m.error} error</div>
            </div>
            <div className="tile">
              <div className="tile-label">Jev hit rate</div>
              <div className={`tile-value ${m.hit_rate == null ? "" : beats ? "up" : "down"}`}>{pct0(m.hit_rate)}</div>
              <div className="tile-sub">
                {m.hit_rate == null ? "waiting for outcomes" : (
                  <><span aria-hidden="true">{beats ? "▲" : "▼"}</span> {beats ? "beats" : "does not beat"} the best baseline ({pct0(best)})</>
                )}
              </div>
            </div>
            <div className="tile">
              <div className="tile-label">Direction Brier</div>
              <div className="tile-value">{num(ds?.brier)}</div>
              <div className="tile-sub">base rates {num(ds?.brier_base_rate)} · lower is better</div>
            </div>
            <div className="tile">
              <div className="tile-label">Cost so far</div>
              <div className="tile-value">{fmtUsd(m.cost_usd)}</div>
              <div className="tile-sub">{m.decisions} calls</div>
            </div>
          </div>

          {m.scored === 0 ? (
            <p className="notice" role="status">
              No calls scored yet. Each call is checked {horizon} hours after Jev made it
              {firstScoredAt != null && <>, so the first results arrive around <strong>{fmtTime(firstScoredAt)} {TZ}</strong></>}.
              {" "}{m.pending.toLocaleString()} calls are waiting.
            </p>
          ) : (
            <>
              <h3>Hit rate vs. baselines</h3>
              <Bars m={m} />
            </>
          )}

          {m.scored > 0 && <div className="two inner">
            <div>
              <h3>Confusion matrix</h3>
              <div className="scroll"><Confusion m={m} /></div>
            </div>
            <div>
              <h3>Calibration</h3>
              <div className="scroll"><Calibration m={m} /></div>
              <p className="muted small">
                Log loss {num(ds?.log_loss)}. Adverse move: Brier {num(as?.brier)} vs. {num(as?.brier_base_rate)} at
                its base rate of {pct0(as?.base_rate)} ({as?.n ?? 0} calls).
              </p>
            </div>
          </div>}

          <h3>Recent calls</h3>
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  <th title="When Jev was asked: just after this hour's candle closed">Decided ({TZ})</th><th>Symbol</th><th title="Jev's call and its own confidence">Call (conf.)</th><th>Regime</th><th className="num">P(adverse)</th>
                  <th>Realized</th><th className="num">Return</th><th>Result</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((r) => (
                  <tr key={`${r.symbol}-${r.candle_ts}`}>
                    <td>{fmtTime(r.candle_ts + HOUR_MS)}</td>
                    <td>{r.symbol}</td>
                    <td>{r.predicted ? `${r.predicted} ${pct0(r.confidence)}` : "–"}</td>
                    <td>{r.regime ?? "–"}</td>
                    <td className="num">{r.p_adverse == null ? "–" : pct0(r.p_adverse)}</td>
                    <td>{dirLabel(r.outcome?.direction)}</td>
                    <td className={`num ${r.outcome ? (r.outcome.ret_pct > 0 ? "up" : r.outcome.ret_pct < 0 ? "down" : "") : ""}`}>
                      {r.outcome ? fmtPct(r.outcome.ret_pct / 100) : "–"}
                    </td>
                    <td title={r.abstain_reason ?? undefined}><Result r={r} horizon={horizon} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}
