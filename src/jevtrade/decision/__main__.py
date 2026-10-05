"""CLI: one live JevModel decision on the latest closed bar.

    python -m jevtrade.decision [--symbol BTC/USD] [--config PATH] [--record DIR]

Fetches recent public candles (the same lookback the hourly collector uses,
so the state text matches what Jev is shown there), builds the market
state, asks Jev the configured questions once, logs the decision and every
answer to the configured SQLite store, and prints the raw response, latency,
input tokens and cost. `--record DIR` saves the state and response
body as a test fixture (no headers, so no credentials).
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

from ..config import load_config
from ..collect.collector import candles_frame, lookback_bars
from ..data.fetcher import OHLCVSource, fetch_range, has_deep_history, page_limit_for, public_exchange
from ..data.store import connect
from ..data.timeframes import last_closed_open_ms, timeframe_ms
from ..features.compute import compute_features
from ..state.builder import MarketState, build_state
from .jev import JevModel
from .log import DecisionLog


def latest_state(cfg, source: OHLCVSource, symbol: str, now_ms: int):
    """Candles up to the last closed bar and the state built on it (None if not enough history)."""
    d = cfg.data
    tf_ms = timeframe_ms(d.timeframe)
    latest = last_closed_open_ms(now_ms, tf_ms)
    rows = fetch_range(source, symbol, d.timeframe, latest - lookback_bars(cfg, 1) * tf_ms, latest, tf_ms,
                       page_limit_for(d.exchange, d.page_limit), skip_empty=has_deep_history(d.exchange))
    candles = candles_frame(rows)
    if candles.empty:
        return candles, None
    state: MarketState | None = build_state(candles, compute_features(candles, cfg.features),
                                            cfg.decision.horizon_bars, d.timeframe, cfg.state, cfg.features)
    return candles, state


def _pretty(raw: str) -> str:
    """The response body, indented when it is JSON (an error page may not be)."""
    try:
        return json.dumps(json.loads(raw), indent=2)
    except ValueError:
        return raw


def main() -> None:
    ap = argparse.ArgumentParser(description="Make one live Jev decision call")
    ap.add_argument("--config", default=None)
    ap.add_argument("--symbol", default=None, help="defaults to the first configured symbol")
    ap.add_argument("--record", default=None, help="directory to save a fixture into")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    cfg = load_config(args.config)
    symbol = args.symbol or cfg.data.symbols[0]
    source = public_exchange(cfg.data.exchange, cfg.data.requests_trust_env)
    candles, state = latest_state(cfg, source, symbol, int(time.time() * 1000))
    if state is None:
        raise SystemExit(f"not enough history for {symbol}: {len(candles)} candles")

    questions = cfg.decision.resolved_questions(cfg.data.timeframe)
    d = JevModel(cfg.decision.jev).decide(state, questions)
    bar_ts = int(candles.index[-1].timestamp() * 1000)
    Path(cfg.storage.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
    decision_id = DecisionLog(connect(cfg.storage.sqlite_path)).record(
        "live-cli", symbol, bar_ts, d, state.text)

    print(f"symbol={symbol} last_closed_bar={candles.index[-1].isoformat()} "
          f"candles={len(candles)} state_est_tokens={state.est_tokens}")
    print(f"pinned_model={cfg.decision.jev.model} served_model={d.extra.get('served_model')}")
    print(f"latency_ms={d.latency_ms:.0f} input_tokens={d.input_tokens} "
          f"est_cost_usd={d.extra.get('estimated_cost_usd')} "
          f"reported_cost_usd={d.extra.get('reported_cost_usd')}")
    print(f"abstain={d.abstain} reason={d.abstain_reason}")
    print(f"logged decision id={decision_id} to {cfg.storage.sqlite_path}")
    print("raw_response:")
    print(_pretty(d.raw_output) if d.raw_output else "(none)")

    if args.record:
        out = Path(args.record)
        out.mkdir(parents=True, exist_ok=True)
        (out / "state.json").write_text(state.text + "\n")
        (out / "response.json").write_text(d.raw_output + "\n")
        print(f"recorded fixture in {out}")


if __name__ == "__main__":
    main()
