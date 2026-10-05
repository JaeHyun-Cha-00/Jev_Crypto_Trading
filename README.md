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
| `src/jevtrade/state/` | Anonymized JSON market state for the model | ✅ stage 3 |
| `src/jevtrade/decision/` | `DecisionModel`: Mock ✅, Baseline ✅, Jev (pending docs) | ✅ stage 3 (partial) |
| `src/jevtrade/policy/` | Probabilities → actions, risk limits | ✅ stage 3 |
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
  exchange: coinbaseexchange # api.exchange.coinbase.com; or kraken
  symbols: [BTC/USD, ETH/USD]
  timeframe: 1h
  start: "2025-01-01T00:00:00Z"   # earliest candle kept; backfilled on coinbase
  page_limit: 300            # candles per request, clamped per exchange
  requests_trust_env: true   # honour HTTPS_PROXY / REQUESTS_CA_BUNDLE
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
- The default exchange is Coinbase Exchange (ccxt id `coinbaseexchange`,
  `api.exchange.coinbase.com`). It returns at most 300 candles per request
  but pages back through full hourly history, so a fresh store is backfilled
  to `start` (more than a year of BTC/USD and ETH/USD by default). Windows
  with no candles, such as an outage, are stepped over and then reported as
  gaps. If `start` is moved earlier, the missing head is backfilled on the
  next sync.
- Kraken remains available: set `exchange: kraken` (the USD symbols exist
  there too). **Limitation:** Kraken's public OHLC endpoint returns only the
  most recent 720 candles (about 30 days at 1h), whatever `since` is set to,
  so a Kraken store starts about 30 days back and a sync stopped for longer
  leaves an unresolved gap. Stores are keyed by exchange, so Kraken and
  Coinbase candles never mix.
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

### 3. State, decision models, policy

Each closed bar flows through the pipeline like this:

```
candles ─▶ features ─▶ build_state() ─▶ DecisionModel.decide() ─▶ Policy.evaluate() ─▶ Action
                         (JSON text)       (probabilities only)     (all risk limits)
```

**State** (`state/builder.py`) is a compact JSON object: current features,
volatility and return percentiles, and the last 24 bars of return, range and
volume z-score. It contains no dates, prices, volumes or tickers. A test checks
this: rescaling prices or volume, or shifting the series in time, must produce
byte-identical text. The state targets fewer than 2,000 estimated tokens (it
drops the oldest bars to fit) and is rejected above 32K.

**Decision models** (`decision/`) answer configured questions with a
probability per option. The default question is `direction`: up, flat or down
over `horizon_bars`, where "flat" means within ±`flat_band_pct`.
- `MockModel` is deterministic: its output is a hash of the state, with an
  optional abstain rate or fixed outputs.
- `BaselineModel` is an SMA crossover expressed through the same interface,
  so it goes through the same policy.
- `JevModel` is **not implemented yet**. `decision.model: jev` raises an
  error until the TypeSafe API docs can be read.

The `decisions` table logs every call with the input hash, input text, model
version, raw output, latency, input tokens, estimated cost, and the policy's
verdict.

**Policy** (`policy/engine.py`) is long-only spot. It runs on bar close, and
orders fill at the next bar's open.
- **Entry** requires `p(up) ≥ entry_threshold` and
  `p(up) − p(down) ≥ min_edge`. An abstain or a missing decision means no
  trade.
- **Size** is `risk_per_trade / stop_loss_pct`, capped by `max_position_frac`
  and by the room left under `max_gross_exposure`.
- **Exits** are checked in order: stop-loss on close, `max_holding_bars`,
  then `p(down) ≥ exit_threshold`. The first two don't depend on the model.
- **Blocks:** `max_daily_loss_pct` (measured from equity at the start of the
  UTC day) blocks new entries for the rest of that day. After
  `cooldown_after_losses` consecutive losses, entries pause for
  `cooldown_bars`.
- Every action records a reason string, including skips.

## Evaluation validity

Jev may have seen historical market data during training. Any backtest on a
period before Jev's release is therefore **potentially contaminated** and
will be labelled as such. Forward paper trading after the release is the
primary evaluation.
