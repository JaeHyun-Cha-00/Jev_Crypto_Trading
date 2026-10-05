# Running each stage

## 1. Data

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
  to `start` (more than a year per coin by default). Windows
  with no candles, such as an outage, are stepped over and then reported as
  gaps. If `start` is moved earlier, the missing head is backfilled on the
  next sync.
- Kraken remains available: set `exchange: kraken` (BTC/USD and ETH/USD
  exist there; not every coin in the default list does). **Limitation:** Kraken's public OHLC endpoint returns only the
  most recent 720 candles (about 30 days at 1h), whatever `since` is set to,
  so a Kraken store starts about 30 days back and a sync stopped for longer
  leaves an unresolved gap. Stores are keyed by exchange, so Kraken and
  Coinbase candles never mix.
- All timestamps are candle open times in UTC epoch milliseconds. DataFrames
  use a tz-aware UTC index.

## 2. Features

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

## 3. State, decision models, policy

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
[docs/jev_prompt.md](jev_prompt.md). The policy uses `direction`: up, flat
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
  python -m jevtrade.decision --symbol BTC/USD
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
  its stop is hit, so notional = equity × 1% / stop distance (12.5% of equity
  with the 8% stop). It is capped at `max_position_frac` (50%) and by the
  room left under `max_gross_exposure` (50%). Every backtest summary prints
  the sizing in force.
- **Exits** are checked in order: stop-loss, `max_holding_bars`,
  then `p(down) ≥ exit_threshold` with `p(down) − p(up) ≥ exit_min_edge`.
  The first two don't depend on the model.
  `max_holding_bars` can differ per model through
  `max_holding_bars_by_model`; `null` turns the time exit off. The baseline
  has it off, so it exits when the SMAs cross back (or on the stop). Jev has
  it off too, so it holds until its answer turns down or the stop.
- **Re-entry:** after an exit, the same symbol can't enter again for
  `min_trade_interval_bars` bars (0 by default: it may re-enter right away).
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

## 4. Backtest

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
- Within a bar, coins act in priority order: open positions first (a stop
  frees room before anything new is sized), then the rest by edge
  (p(up) − p(down)), highest first. When the exposure caps leave room for
  only a few entries, the strongest signals get them, not the coins listed
  first in `data.symbols`. The paper loop and the dashboard's Jev replay use
  the same order.
- Entries and model or holding exits fill at the **next bar's open**, moved
  `slippage_bps` against the trade. Stop exits fill **inside the bar** at the
  action's `fill_price`. An entry's stop is re-anchored to its fill price.
- Every fill pays `fee_bps` on notional. Equity is cash plus positions marked
  at each close; positions still open at the end close at the last close.
- `max_holding_bars` defaults to `decision.horizon_bars` (24 in
  `config/default.yaml`), so a position is held for the hours the direction
  question asks about. The baseline and Jev override it to no time limit and
  exit on their own signal.
- Every decision is logged to `decisions` / `decision_answers` under the run
  id. `summary.json`, `summary.md`, `trades.csv` and `equity.csv` go to
  `backtest.output_dir/<run id>/`.
- `--model jev` costs one API call per symbol per bar and needs
  `--allow-live-model`. Over all 81 default coins since 2025 that is about
  1.2M calls (roughly $100), so pass `--symbols` to narrow it. Runs that end before the pinned snapshot's date are
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

## 7. Paper loop

```bash
python -m jevtrade.paper run          # loop forever: process each closed candle, sleep to the next
python -m jevtrade.paper run --once   # process whatever is ready, then exit
python -m jevtrade.paper status       # equity, open positions, pending orders, last bar
```

The paper loop is meant to run unattended on your own machine or a small VM
(1 vCPU and 1 GB RAM is plenty), not in a hosted notebook or chat session.
It needs outbound HTTPS to the exchange's public API, plus OpenRouter if the
model is Jev.

- **Paper only.** Fills are simulated from candles by the same `Simulator`
  the backtest uses (`src/jevtrade/sim.py`), so a bar fills, sizes and exits
  exactly as it would in a backtest; a test replays history hour by hour and
  checks that paper trades and equity match the backtest. The exchange
  client is unauthenticated and the code has no order calls
  (`tests/test_safety.py`, `tests/test_paper.py`).
- **One pass per closed candle.** Each step syncs public candles, then
  processes every closed bar after the last processed one. A bar's fills,
  decisions, trades, equity row and account state commit in one SQLite
  transaction, and `paper_bars` records it. Running a step again for the
  same candle does nothing.
- **Restart-safe.** Stop it any time (Ctrl-C, `SIGTERM`, a reboot, a crash).
  A bar interrupted mid-way is rolled back and redone on the next start;
  bars missed while it was down are caught up in order. Past
  `max_catchup_bars` (48), older missed bars run risk-only: stops and
  holding limits still apply, but the model isn't called and nothing new
  opens. A file lock (`<sqlite_path>.paper.lock`) stops a second loop from
  writing the same store.
- A fresh run starts at the latest closed bar with `paper.initial_equity`;
  it doesn't replay history. Changing the decision model of an existing run
  is refused; set `paper.run_id` to start a new paper account.
- If a coin the account holds (or has an order queued for) has no candle
  yet, the loop waits for it, retrying every `retry_s`, for at most
  `stall_grace_bars` (1) bars, then processes the bar without it. A flat
  coin with no candle is skipped for that bar without waiting: Coinbase
  publishes no candle for an hour without trades, and a coin it doesn't
  hold has nothing to protect.
- `decision.model: jev` needs `--allow-live-model` or
  `JEVTRADE_ALLOW_LIVE_MODEL=1`, because it calls the paid API once per
  symbol per bar.
- Tables: `paper_state` (account JSON, model, heartbeat), `paper_bars`
  (equity per bar), `paper_trades`, and `decisions` under `run_id = paper`.

To keep it running on a Linux box without Docker, a systemd unit works:

```ini
# /etc/systemd/system/jevtrade-paper.service
[Unit]
Description=jevtrade paper loop
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/jev_crypto_trading
ExecStart=/opt/jev_crypto_trading/.venv/bin/python -m jevtrade.paper run
Restart=always
RestartSec=30
User=jevtrade

[Install]
WantedBy=multi-user.target
```

## 8. Daily report

```bash
python -m jevtrade.report                    # yesterday (UTC) for paper.run_id
python -m jevtrade.report --day 2026-10-04   # a given day
python -m jevtrade.report --run-id <backtest run id> --day 2026-10-04   # calibration of a backtest
```

Writes `reports/out/<run id>/<day>.md` and a `.json` twin, built only from
the SQLite store, so rewriting a day gives the same file. The paper loop
writes each day's report right after that day's last bar
(`paper.daily_report`). A report has:
- **Account:** equity at the day's open and close, day PnL, return since
  start, max drawdown, bars processed (and any risk-only catch-up bars).
- **Trades** closed that day, and **open positions** with stop, mark and
  unrealized PnL.
- **Decisions:** counts, abstains, actions, skip reasons, Jev cost, tokens
  and latency.
- **Calibration** of the `direction` probabilities against what happened
  `horizon_bars` later, over every resolved decision of the run so far:
  Brier score next to the Brier of always forecasting the realized base
  rates (the bar to beat), log loss, top-choice hit rate, and a
  reliability table for p(up).

On Coinbase BTC/ETH since 2025, about 75% of 4-hour windows ended "flat"
(within ±1%), and up and down were about 12.5% each. A model that rarely
says flat is badly calibrated even if its up/down calls are informative.

## 9. API (read-only)

```bash
python -m jevtrade.api                  # http://127.0.0.1:8000, docs at /docs
```

Every route is a `GET`, and the database is opened with SQLite's
`mode=ro`, so the API can't write, place orders, or call the model. It is
safe to run next to the paper loop. Keep it on localhost or behind a
firewall or VPN; it has no authentication.

| Route | Returns |
|---|---|
| `/api/health` | database present, paper heartbeat |
| `/api/config` | symbols, model, sizing and risk limits (no secrets) |
| `/api/paper/status` | equity, cash, return, drawdown, open positions with unrealized PnL, pending orders, risk state |
| `/api/paper/equity?since=&limit=` | equity, cash and exposure per bar |
| `/api/paper/trades?symbol=&limit=` | closed paper trades, newest first |
| `/api/decisions?symbol=&before=&limit=` | model probabilities and the policy's verdict per bar |
| `/api/decisions/{id}` | one decision with its state text and per-question answers |
| `/api/candles?symbol=BTC/USD&limit=` | stored candles |
| `/api/reports`, `/api/reports/{day}` | daily reports (Markdown and JSON) |
| `/api/backtests`, `/api/backtests/{run_id}` | backtest summaries and downsampled equity |
| `/api/forward/summary` | the forward log scored: counts, hit rate vs. baselines, confusion matrix, calibration, Brier/log loss, cost, source and any fetch error |
| `/api/forward/rows?symbol=&limit=` | recent forward calls joined with their outcomes (null while pending), newest first, without `state` |
| `/api/forward/paper?actions=` | Jev's simulated account replayed from the forward log: equity, curve, positions, pending orders, trades, and each hour's action (newest first) |

Every paper route takes `?run_id=` (default `paper.run_id`).

## 10. Web dashboard

```bash
python -m jevtrade.api          # terminal 1: the read-only API on :8000
cd web && npm ci && npm run dev # terminal 2: http://localhost:5173 (proxies /api to :8000)
npm run build                   # static files in web/dist, for any web server that proxies /api
```

React + TypeScript (Vite; lightweight-charts for candles) with two views.

**Market** (`#/`, the default) works like a broker app's coin list, for the
coins in `data.symbols`:
- Prices stream live from Coinbase's public websocket
  (`wss://ws-feed.exchange.coinbase.com`, opened by your browser), with the
  API's `/api/market/tickers` as the baseline every 15 seconds and the
  fallback every 5 seconds if the socket is blocked ("Delayed prices").
  Prices flash green or red as they tick.
- BTC, ETH, SOL and XRP cards, market breadth (coins up vs down), top
  gainers, top losers and most traded.
- The full list: price, 24h change, 24h range, dollar volume, a 24-hour
  sparkline and Jev's last call (p(up)/p(down)) or its holding. Search,
  sort, and tabs for All, a ★ watchlist (kept in your browser) and the coins
  Jev holds.
- A page per coin (`#/coin/BTC-USD`): live price, bid/ask and 24h stats;
  candles from 1 minute to 1 day with volume and Jev's paper buys and sells
  marked, the last candle built live from trades; the order book with depth
  and bid/ask balance; the live trade tape; and Jev's position (P&L at the
  live price), last call and hourly calls on that coin with their results.

The API's market routes (`/api/market/tickers|candles|book|trades`) read
public Coinbase REST endpoints only, cache them for a few seconds and space
calls under Coinbase's public limit (`market:` in `config/default.yaml`).
No keys and no orders.

**Jev portfolio** (`#/jev`) reads the API and refreshes every minute. It
tracks only Jev: the mock/baseline paper loop and backtests are no longer
shown. It shows:
- Health from the forward log's last Jev call.
- **Jev paper portfolio**: the policy and simulator replayed over the answers
  Jev already gave in the forward log (`/api/forward/paper`), with the equity
  curve (24 hours, 7 days, 30 days, all), cash, buys, drawdown, closed
  trades and realized PnL. No extra Jev calls and no real orders.
- **What Jev bought**, at the top: every coin it is buying, holding or sold,
  with times, prices, size and P&L. Coins Jev has bought get a filter chip;
  the rest of `data.symbols` sit in a picker, so the page scales to many coins.
- Buys, trades and PnL per coin, for coins Jev has bought.
- The trading rules in force, including whether the skill gate lets Jev buy.
- Jev's activity: **By hour** (the default) lists every hour since tracking
  started, bought or not: the coins Jev picked (answers that met the buy
  thresholds) with their p(up), what the account did with them (bought, held
  back by the skill gate, no room, already held), and once the question's
  horizon has closed the picks' average return before and after a round trip
  of costs next to the average coin's. An hour the collector skipped shows as
  not asked. The other tabs: its buys, its closed trades, and every hourly
  call with p(up), p(down), the policy's action and reason. All filter by coin.
- The Jev forward log (see below): how the hourly forward calls compare
  with what happened.

**Skill gate** (`jev_paper.gate_lookback_hours`, `gate_min_signals`): the
replay buys only while Jev's recent buy signals would have paid. A signal is
every logged answer that met the buy thresholds, bought or not; it resolves
`decision.horizon_bars` later. New buys are allowed while the signals of the
last 7 days that have resolved averaged a positive return after a round trip
of costs **and** beat the average logged coin over the same hours (so a
market-wide rally alone doesn't count), with at least 500 resolved. Held
coins still sell on Jev's answer and the stop. Blocked buys show a
`skill_gate:` reason.

The replay prices each hour from the logged closes (an hour opens at the
previous hour's close), so stops see closes only, not intrabar dips. It covers
the symbols the collector asks about (`data.symbols`). The replay runs once
per forward-log refresh and is cached. It counts only candles from
`forward_log.start` on (the dashboard shows "Tracking since").

It follows the OS light or dark setting and works down to phone width. Set
`JEVTRADE_API` to point the dev server at an API elsewhere.
