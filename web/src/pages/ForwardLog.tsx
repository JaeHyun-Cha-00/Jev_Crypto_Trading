import { CHOICE_LABEL } from "../lib/api";
import type { ChoiceStats, ForwardMetrics, ForwardRow, ForwardSummary } from "../lib/api";
import { TZ, ago, fmtPct, fmtTime } from "../lib/format";
import { tone } from "../components/ui";

// Jev's hourly choices (collect workflow, data-log branch) against what the price did next.

const pct0 = (v: number | null | undefined) => (v == null ? "n/a" : `${Math.round(v * 100)}%`);
const fmtUsd = (v: number) => `$${v < 1 ? v.toFixed(4) : v.toFixed(2)}`;
const ret = (v: number | null | undefined) => (v == null ? "–" : fmtPct(v));

/** Calls grouped by Jev's choice: how many, what the price did next, how often the choice was right. */
function Choices({ m }: { m: ForwardMetrics }) {
  const rows: { label: string; s: ChoiceStats; note?: string; strong?: boolean }[] = [
    { label: "All buys", s: m.buys, strong: true },
    { label: CHOICE_LABEL.buy_large, s: m.entry.buy_large },
    { label: CHOICE_LABEL.buy_normal, s: m.entry.buy_normal },
    { label: CHOICE_LABEL.buy_small, s: m.entry.buy_small },
    { label: CHOICE_LABEL.wait, s: m.entry.wait, strong: true },
    { label: "Every coin it could buy", s: m.entry_all, note: "buys and waits together: the market Jev chose from" },
    { label: CHOICE_LABEL.hold, s: m.manage.hold, strong: true },
    { label: CHOICE_LABEL.sell, s: m.manage.sell, strong: true },
  ];
  return (
    <table>
      <thead>
        <tr><th>Jev chose</th><th className="num">Calls</th><th className="num">Price next</th><th className="num">Rose</th><th className="num">Right</th></tr>
      </thead>
      <tbody>
        {rows.map(({ label, s, note, strong }) => (
          <tr key={label} title={note}>
            <td className={strong ? "" : "muted"}>{strong ? label : `· ${label}`}</td>
            <td className="num">{s.n}</td>
            <td className={`num ${tone(s.mean_ret) ?? ""}`}>{ret(s.mean_ret)}</td>
            <td className="num">{s.n ? pct0(s.up_rate) : "–"}</td>
            <td className="num">{label.startsWith("Every") || !s.n ? "–" : pct0(s.right_rate)}</td>
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
        <tr><th>p(buy)</th><th className="num">Calls</th><th className="num">Price next</th><th className="num">Rose</th></tr>
      </thead>
      <tbody>
        {m.calibration.map((b) => (
          <tr key={b.lo}>
            <td>{b.lo === 0 ? `< ${pct0(b.hi)}` : `${pct0(b.lo)}–${pct0(b.hi)}`}</td>
            <td className="num">{b.n}</td>
            <td className={`num ${tone(b.mean_ret) ?? ""}`}>{ret(b.mean_ret)}</td>
            <td className="num">{b.n ? pct0(b.up_rate) : "–"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const HOUR_MS = 3_600_000;

function Result({ r, horizon }: { r: ForwardRow; horizon: number }) {
  if (r.status !== "answered") return <span className="tag">{r.status}</span>;
  if (r.right === null) return <span className="muted">result {fmtTime(r.candle_ts + (horizon + 1) * HOUR_MS).slice(5)}</span>;
  return r.right ? <span className="tag hit">✓ right</span> : <span className="tag miss">✗ wrong</span>;
}

export function ForwardLog({ summary, rows, symbol, error, horizon }: {
  summary: ForwardSummary | null;
  rows: ForwardRow[];
  symbol: string;
  error: string | null;
  /** hours after a call that it is scored (decision.horizon_bars) */
  horizon: number;
}) {
  const m = summary ? (symbol ? summary.per_symbol[symbol] : summary.overall) : null;
  const shown = rows.filter((r) => !symbol || r.symbol === symbol).slice(0, 100);
  const edge = m?.buys.mean_ret != null && m.entry_all.mean_ret != null ? m.buys.mean_ret - m.entry_all.mean_ret : null;
  // A call is scored once the candle `horizon` hours after it has closed, i.e. horizon + 1 hours after its own candle opened.
  const firstScoredAt = m?.oldest_pending_ts != null ? m.oldest_pending_ts + (horizon + 1) * HOUR_MS : null;

  return (
    <section className="card">
      <h2>Jev's accuracy</h2>
      <p className="muted small">
        {summary?.source === "off"
          ? "Turned off (forward_log.source: off)."
          : <>Each of Jev's choices, scored by what the price did over the next {horizon} hours. A buy is right when the price rose
            by more than a round trip of costs ({pct0(m?.round_trip)}), a wait when it didn't; a hold is right when the price rose,
            a sell when it didn't. Last call {ago(summary?.last_called_at ?? null).text}.</>}
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
              <div className="tile-label">Choices right</div>
              <div className="tile-value">{pct0(m.right_rate)}</div>
              <div className="tile-sub">{m.right_rate == null ? "waiting for outcomes" : `${m.right} of ${m.scored}`}</div>
            </div>
            <div className="tile">
              <div className="tile-label">Buys, {horizon}h later</div>
              <div className={`tile-value ${tone(m.buys.mean_ret) ?? ""}`}>{ret(m.buys.mean_ret)}</div>
              <div className="tile-sub">
                {edge == null ? `${m.buys.n} buys scored` : (
                  <><span aria-hidden="true">{edge > 0 ? "▲" : "▼"}</span> {fmtPct(edge)} vs. every coin it could buy</>
                )}
              </div>
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
            <div className="two inner">
              <div>
                <h3>By choice</h3>
                <div className="scroll"><Choices m={m} /></div>
              </div>
              <div>
                <h3>Does a surer buy do better?</h3>
                <div className="scroll"><Calibration m={m} /></div>
                <p className="muted small">Calls on coins the account didn't hold, by the probability Jev gave the three buy sizes together.</p>
              </div>
            </div>
          )}

          <h3>Recent calls</h3>
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  <th title="When Jev was asked: just after this hour's candle closed">Decided ({TZ})</th><th>Symbol</th>
                  <th title="Whether the account held the coin when Jev was asked">Holding</th><th title="Jev's choice and its probability">Jev chose</th>
                  <th className="num">Price next</th><th>Result</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((r) => (
                  <tr key={`${r.symbol}-${r.candle_ts}`}>
                    <td>{fmtTime(r.candle_ts + HOUR_MS)}</td>
                    <td>{r.symbol}</td>
                    <td className="muted">{r.question === "manage" ? "yes" : r.question === "entry" ? "no" : "–"}</td>
                    <td>{r.choice ? `${CHOICE_LABEL[r.choice]} ${pct0(r.p_choice)}` : "–"}</td>
                    <td className={`num ${tone(r.outcome?.ret_pct) ?? ""}`}>{r.outcome ? fmtPct(r.outcome.ret_pct / 100) : "–"}</td>
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
