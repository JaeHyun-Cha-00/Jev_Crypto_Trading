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
| `src/jevtrade/backtest/` | Event-driven walk-forward backtester | ✅ stage 4 |
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
probability per option. The four default questions are documented in
[docs/jev_prompt.md](docs/jev_prompt.md). The policy uses `direction`: up, flat
or down over `horizon_bars`, where "flat" means within ±`flat_band_pct`.
- `MockModel` is deterministic: its output is a hash of the state, with an
  optional abstain rate or fixed outputs.
- `BaselineModel` is an SMA crossover expressed through the same interface,
  so it goes through the same policy.
- `JevModel` calls TypeSafe's Jev through OpenRouter's System One endpoint
  (`POST https://openrouter.ai/api/v1/systemone`), asking all questions in one
  request. The version is pinned to a dated snapshot in
  `decision.jev.model` (aliases such as `-latest` are rejected), and a
  response served by any other version abstains. Each call logs the pinned and
  served version, latency, input tokens and estimated cost
  (`input_tokens × price_per_input_token_usd`; OpenRouter's reported cost is
  used when present). The key comes from `$OPENROUTER_API_KEY`. Transient
  errors retry, then abstain; auth, billing and bad-request errors raise.
  Unit tests replay recorded fixtures in `tests/fixtures/jev/`.

  One live decision on the latest closed bar (prints the raw response,
  latency, tokens and cost; `--record DIR` saves a new fixture):

  ```bash
  python -m jevtrade.decision --symbol BTC/USDT
  ```

The `decisions` table logs every call with the input hash, input text, model
version, raw output, latency, input tokens, estimated cost, and the policy's
verdict. `decision_answers` stores every answer (probabilities, raw `noul`,
confidence) one row per question, for calibration checks.

**Policy** (`policy/engine.py`) is long-only spot. It runs on bar close, and
orders fill at the next bar's open.
- **Entry** requires `p(up) ≥ entry_threshold` and
  `p(up) − p(down) ≥ min_edge`. An abstain or a missing decision means no
  trade.
- **Size** is risk-based: a trade risks `risk_per_trade` (1%) of equity if
  its stop is hit, so notional = equity × 1% / stop distance (33% of equity
  with the 3% stop). It is capped at `max_position_frac` (50%) and by the
  room left under `max_gross_exposure` (50%). Every backtest summary prints
  the sizing in force.
- **Exits** are checked in order: stop-loss, `max_holding_bars`,
  then `p(down) ≥ exit_threshold`. The first two don't depend on the model.
  `max_holding_bars` can differ per model through
  `max_holding_bars_by_model`; `null` turns the time exit off. The baseline
  has it off, so it exits when the SMAs cross back (or on the stop).
- **Re-entry:** after an exit, the same symbol can't enter again for
  `min_trade_interval_bars` (4) bars.
- **Stops** trigger on the bar's low, not its close. A stopped long fills
  inside that bar at the stop price, or at the bar's open if it gapped
  below the stop, less `stop_slippage_bps`. The backtest and paper loops
  must pass `bar_open` and `bar_low` to `Policy.evaluate` and use the
  action's `fill_price` instead of the next open.
- **Blocks:** `max_daily_loss_pct` (measured from equity at the start of the
  UTC day) blocks new entries for the rest of that day. After
  `cooldown_after_losses` consecutive losses, entries pause for
  `cooldown_bars`.
- Every action records a reason string, including skips.

### 4. Backtest

```bash
python -m jevtrade.data                      # make sure the store is synced
python -m jevtrade.backtest --model baseline # or mock; --start/--end ISO-8601 UTC, --symbols ...
```

`backtest/engine.py` walks every closed bar in time order across the
configured symbols and runs the same pipeline the paper loop will run:
features, `build_state()`, `DecisionModel.decide()`, then `Policy.evaluate()`
with `bar_open` and `bar_low`.
- Each decision only sees candles up to its bar. Features are computed once
  and sliced, which is safe because they are causal; a test checks that
  appending future bars leaves every earlier decision unchanged.
- Entries and model or holding exits fill at the **next bar's open**, moved
  `slippage_bps` against the trade. Stop exits fill **inside the bar** at the
  action's `fill_price`. An entry's stop is re-anchored to its fill price.
- Every fill pays `fee_bps` on notional. Equity is cash plus positions marked
  at each close; positions still open at the end close at the last close.
- `max_holding_bars` defaults to `decision.horizon_bars` (4), so a position is
  held for the 4 hours the direction question asks about. The baseline
  overrides it to no time limit and exits on its own signal.
- Every decision is logged to `decisions` / `decision_answers` under the run
  id. `summary.json`, `summary.md`, `trades.csv` and `equity.csv` go to
  `backtest.output_dir/<run id>/`.
- `--model jev` costs one API call per symbol per bar and needs
  `--allow-live-model`. Runs that end before the pinned snapshot's date are
  labelled potentially contaminated (see below). Tests never call Jev.

Results on Coinbase BTC/USD and ETH/USD, 1h, 2025-01-01 to 2026-10-05, with the
default config (10 bps fee and 2 bps slippage per side, 1% risk per trade):

| Model | Return | Max DD | Sharpe | Trades | Win rate | Avg bars held | Fees | Buy & hold BTC / ETH |
|---|---|---|---|---|---|---|---|---|
| Mock (hash noise) | −56.1% | −56.7% | −5.4 | 1,181 | 33.2% | 3.5 | 5,046 | −8.0% / −18.7% |
| Baseline (SMA 20/50) | −35.4% | −40.9% | −1.5 | 387 | 30.0% | 37.9 | 1,457 | −8.0% / −18.7% |

Neither offline model has an edge over this period. Letting the baseline
ride its own signal cut its trade count from 1,628 to 387 and its loss
roughly in half; the mock model still pays round-trip costs (about 24 bps)
on a 4-bar hold. These runs check the plumbing and set the bar Jev has to
clear.

## Evaluation validity

Jev may have seen historical market data during training. Any backtest on a
period before Jev's release is therefore **potentially contaminated** and
will be labelled as such. Forward paper trading after the release is the
primary evaluation.
