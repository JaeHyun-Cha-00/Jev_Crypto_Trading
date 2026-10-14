# Jev_Crypto_Trading

A personal research system where **Jev** (TypeSafe AI) paper-trades crypto
on its own: every hour it decides, coin by coin, whether to buy (and how
much), wait, hold or sell, and a simulated account executes exactly that.

> **Paper trading only.** This system never places real orders. It uses no
> exchange API keys and calls no authenticated endpoints. Market data comes from
> public endpoints only. `tests/test_safety.py` enforces this.

## Design principle

Jev trades with one **fixed prompt** (`docs/jev_prompt.md`, version-stamped
and locked by a test): a mandate (long-only spot, costs, the goal is profit
after costs), the coin's anonymized market state, the account, and the coin's
position. Code keeps the loop and the memory, the account and positions
replayed from the log, and executes Jev's choices within the cash it has.
There is no stop-loss, time limit, position cap or probability threshold.

## Layout

Python package `src/jevtrade/`, in the order data flows through it:

| Module | Purpose |
|---|---|
| `data/` | Public OHLCV via ccxt (`fetcher.py`) and the dashboard's live Coinbase market data (`market.py`) |
| `features/` | Closed-candle features, no look-ahead |
| `state/` | Anonymized JSON market state for the model |
| `decision/` | The Jev client (via OpenRouter) and the fixed prompt (`agent.py`); `python -m jevtrade.decision` asks Jev once from the terminal |
| `collect/` | The hourly run: Jev trades the account one closed candle at a time (GitHub Actions → `data-log` branch), and `kick.py`, which starts the run when GitHub skips it |
| `forward/` | Reading the forward log back and scoring Jev's choices (`log.py`), and the paper account both the collector and the dashboard step through (`agent.py`) |
| `api/` | Read-only FastAPI behind the dashboard, and the hosted-site extras (`hosting.py`) |
| `net.py` | The plain HTTPS GET the readers above share |
| `config.py` | Typed settings loaded from `config/default.yaml` |

Elsewhere: `web/` is the React dashboard (`src/pages/`, `src/components/`,
`src/lib/` for the API client and formatting), `tests/` mirrors the modules,
`config/` holds the YAML, `docs/` the longer guides, and `Dockerfile`
with `render.yaml` at the root deploy it.

## How it runs

- **GitHub Actions** (`.github/workflows/collect.yml`) has Jev trade every
  tracked coin each hour and commits its choices to the `data-log` branch.
- **Render** (`render.yaml`) hosts the dashboard and restarts a skipped hourly
  run: see [Live website](#live-website).

Nothing needs to run on your own computer.

## Dashboard

| Page | What it shows |
|---|---|
| **Portfolio** (home, `#/`) | The simulated account at live Coinbase prices, its equity in 5-minute steps, what it holds and what it sold |
| **Activity** | Jev's buys hour by hour, what the account did, and every call |
| **Accuracy** | Jev's choices scored against what the price did over the next 24 hours |
| **Prompt** | The fixed prompt Jev trades with, and how the account executes it |
| **Market** | Every tracked coin live from Coinbase, with a page per coin |

Times are when Jev decided: just after each hourly candle closes.

## How the Jev portfolio trades

Simulated with fake money; nothing here places orders.

- Every hour, for a coin the account doesn't hold, Jev chooses **buy small,
  buy normal, buy large or wait** (5%, 10% or 20% of equity); for a coin it
  holds, **hold or sell**. It sees how long it has held the coin, the return
  since the buy, the best and worst since then, and the account's cash.
- The account sells first, then buys the most confident choices first, each
  cut to the cash left. Nothing else sells: no stop-loss and no time limit.
- **Costs** are Coinbase Advanced's: a 0.60% taker fee per side plus an
  estimated spread, about 1.2–1.8% a round trip. Jev's mandate quotes ~1.5%.
- It starts at $10,000 from `forward_log.start` (the first decision is an hour
  later, as that candle closes); the settings are under `jev_paper` in
  `config/default.yaml`.
- Until 2026-10-14 a rule-based policy traded on Jev's direction
  probabilities instead: see [Version 1](#version-1-rule-based-archived).

## Live website

`render.yaml` deploys the dashboard as one free Render web service. To set it up:

1. Sign in at https://dashboard.render.com with GitHub and let Render see this repo.
2. New → Blueprint → pick this repo.
3. Fill in `DASHBOARD_PASSWORD` (the browser asks for it; any user name works)
   and `GITHUB_TOKEN` (Contents: read, Actions: read and write), then Apply.

Every merge to `main` that touches the app redeploys. The same service starts
the hourly collect run when GitHub skips it (from :03 past the hour) and pings
itself every 10 minutes, because free services otherwise sleep after 15 idle
minutes.

## Setup

Requires Python 3.11 or newer.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'   # or '.[api]' for just the runtime plus the API server
cp .env.example .env   # only needed to call Jev yourself
pytest
```

To run the dashboard locally: `python -m jevtrade.api` in one terminal and
`cd web && npm ci && npm run dev` in another, then open http://localhost:5173.

## Configuration

Settings live in `config/default.yaml`, which is validated by pydantic models
in `src/jevtrade/config.py`. Pass `--config path.yaml` to any CLI to use another
file. A misspelled key is an error, not silently ignored. Secrets are read
**only** from environment variables (`.env`, which git ignores), never from YAML.

```yaml
data:
  exchange: coinbaseexchange # api.exchange.coinbase.com, the only exchange supported
  symbols: [BTC/USD, ETH/USD, ...]  # the 82 coins Jev is asked about hourly; all on Coinbase
  timeframe: 1h
  page_limit: 300            # candles per request; Coinbase serves at most 300
  requests_trust_env: true   # honour HTTPS_PROXY / REQUESTS_CA_BUNDLE
```

## Docs

| File | What it covers |
|---|---|
| [docs/forward-log.md](docs/forward-log.md) | The hourly forward log of Jev's answers: the collect workflow, the backup kick, the `data-log` branch, the Accuracy page |
| [docs/jev_prompt.md](docs/jev_prompt.md) | The fixed prompt Jev trades with, and how the account executes it |

## Version 1: rule-based (archived)

From 2026-10-06 to 2026-10-14 Jev answered four fixed questions per coin
(24-hour direction at a 3% band, regime, an 8% dip, a clear signal) without
seeing the account, and a rule-based policy traded on the direction
probabilities: buy at p(up) ≥ 0.60 with an edge of 0.40, sell at
p(down) ≥ 0.40, an 8% stop.

| | Return, 10-06 18:00Z to 10-14 21:00Z |
|---|---|
| Jev account | −14.3% (62 trades, 9 winners) |
| Equal-weight buy and hold, 82 coins | −9.5% |
| BTC | −4.5% |

Its direction calls were right 40% of the time against 51% for always
"flat", and its buy signals returned the same as the average coin. The
answers followed the last 24 hours (p(up) vs. the prior 24-hour return:
+0.62), so it bought after rises and sold on the first pullback, most within
hours. The code, the full results and the prompt are on the
[`v1-rule-based`](https://github.com/JaeHyun-Cha-00/Jev_Crypto_Trading/tree/v1-rule-based)
branch; its log stays on `data-log` (schema 1 lines).

## Evaluation validity

Jev may have seen historical market data during training, so results on any
period before its release are **potentially contaminated**. That is why Jev is
judged only on the forward log: answers given hour by hour, scored once the
future they asked about has happened.
