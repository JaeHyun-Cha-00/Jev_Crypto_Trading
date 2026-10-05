# Jev_Crypto_Trading

A personal research system that paper-trades crypto using **Jev** (TypeSafe AI)
as a probability model, with a deterministic Python policy layer that owns
every trading decision.

> **Paper trading only.** This system never places real orders. It uses no
> exchange API keys and calls no authenticated endpoints. Market data comes from
> public endpoints only. `tests/test_safety.py` enforces this.

## Design principle

Jev interprets market state. It does not own trading. Jev outputs
probabilities, and a rule-based layer decides whether to act, how much to
trade, and when to exit. All risk limits live in code, never in prompts.

## Layout

| Module | Purpose | Status |
|---|---|---|
| `src/jevtrade/data/` | Public OHLCV via ccxt → SQLite, incremental, gap detection, UTC | ✅ stage 1 |
| `src/jevtrade/features/` | Closed-candle features, no look-ahead | ✅ stage 2 |
| `state/` | Anonymized text market state for Jev | planned |
| `decision/` | `DecisionModel`: Jev / Mock / Baseline | planned |
| `policy/` | Probabilities → actions, risk limits | planned |
| `backtest/` | Event-driven walk-forward backtester | planned |
| `paper/` | Live paper loop | planned |
| `report/` | Metrics and daily Markdown summary | planned |
| `api/` | Read-only FastAPI | planned |
| `web/` | React dashboard | planned |

## Setup

Requires Python 3.11 or newer.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env   # only needed once the JevModel stage lands
pytest
```

## Configuration

Settings live in `config/default.yaml`, which is validated by pydantic models
in `src/jevtrade/config.py`. Pass `--config path.yaml` to any CLI to use another
file. Secrets are read **only** from environment variables (`.env`, which git
ignores), never from YAML.

```yaml
data:
  exchange: coinbase         # any ccxt exchange id with public OHLCV
  symbols: [BTC/USDT, ETH/USDT]
  timeframe: 1h
  start: "2024-01-01T00:00:00Z"   # first candle fetched into an empty store
  page_limit: 300            # candles per request (coinbase max)
storage:
  sqlite_path: data/jevtrade.sqlite
```

## Running each stage

### 1. Data

```bash
python -m jevtrade.data            # sync all configured symbols up to the last closed candle
```

- Only **closed** candles are stored, so the candle still forming is never
  written.
- Syncs are incremental: each one resumes from the last stored candle.
- Gaps between stored candles are detected and backfilled once. Gaps that
  remain are real exchange gaps (for example, maintenance). They are logged
  and reported, never interpolated.
- The default exchange is Coinbase (public Advanced Trade candles, max 300
  per request). Kraken was not chosen because its public OHLC endpoint returns
  only the most recent 720 candles, so it can't backfill history.
- All timestamps are candle open times in UTC epoch milliseconds. DataFrames
  use a tz-aware UTC index.

### 2. Features

`jevtrade.features.compute.compute_features(candles_df)` returns, for each
closed candle:

| Feature | Definition |
|---|---|
| `ret_{1,4,24,72}` | log return over *w* bars |
| `rvol_{24,72}` | std of 1-bar log returns over *w* bars |
| `volume_z_24` | z-score of log volume against the trailing 24 bars |
| `rsi_14` | Wilder RSI (recursive, causal) |
| `dist_ma_{20,50,200}` | `close / SMA(w) - 1` |

Rows without enough history (`max_lookback()` = 200 bars by default) contain
NaN and are treated as "not ready", never filled. Look-ahead is tested two
ways. First, features must not change when future candles are appended.
Second, they must not change when future candles are perturbed.

## Evaluation validity

Jev may have seen historical market data during training. Any backtest on a
period before Jev's release is therefore **potentially contaminated** and
will be labelled as such. Forward paper trading after the release is the
primary evaluation.
