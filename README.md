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

Python package `src/jevtrade/`, in the order data flows through it:

| Module | Purpose |
|---|---|
| `data/` | Public OHLCV via ccxt (`fetcher.py`) and the dashboard's live Coinbase market data (`market.py`) |
| `features/` | Closed-candle features, no look-ahead |
| `state/` | Anonymized JSON market state for the model |
| `decision/` | The questions and the Jev model (via OpenRouter); `python -m jevtrade.decision` asks Jev once from the terminal |
| `policy/` | Probabilities → actions, risk limits |
| `sim.py` | Simulated account behind the Jev portfolio |
| `collect/` | Hourly forward log of Jev's answers and outcomes (GitHub Actions → `data-log` branch), and `kick.py`, which starts the run when GitHub skips it |
| `forward/` | Reading the forward log back (`log.py`), scoring it, and replaying Jev's paper account from it (`portfolio.py`) |
| `api/` | Read-only FastAPI behind the dashboard, and the hosted-site extras (`hosting.py`) |
| `net.py` | The plain HTTPS GET the readers above share |
| `config.py` | Typed settings loaded from `config/default.yaml` |

Elsewhere: `web/` is the React dashboard (`src/pages/`, `src/components/`,
`src/lib/` for the API client and formatting), `tests/` mirrors the modules,
`config/` holds the YAML, `docs/` the longer guides, and `Dockerfile`
with `render.yaml` at the root deploy it.

## How it runs

- **GitHub Actions** (`.github/workflows/collect.yml`) asks Jev about every
  tracked coin each hour and commits the answers to the `data-log` branch.
- **Render** (`render.yaml`) hosts the dashboard and restarts a skipped hourly
  run: see [Live website](#live-website).

Nothing needs to run on your own computer.

## Dashboard

| Page | What it shows |
|---|---|
| **Portfolio** (home, `#/`) | The simulated account at live Coinbase prices, its equity in 5-minute steps, what it holds and what it sold |
| **Activity** | Jev's picks hour by hour, what the account did, and every call |
| **Accuracy** | Jev's calls scored against what happened 24 hours later, next to naive baselines |
| **Rules** | The trading rules and costs below |
| **Market** | Every tracked coin live from Coinbase, with a page per coin |

Times are when Jev decided: just after each hourly candle closes.

## How the Jev portfolio trades

Simulated with fake money; nothing here places orders.

- **Buy** when Jev gives p(up) ≥ 0.60 and p(up) − p(down) ≥ 0.40 for the next
  24 hours. Each buy risks 1% of the account at the 8% stop, so it is 12.5% of
  the account, smaller for thin coins (at most 1% of the coin's hourly dollar
  volume). The account can be fully invested; strongest signals go first.
- **Sell** when Jev turns bearish (p(down) ≥ 0.40 and p(down) − p(up) ≥ 0.30)
  or the price falls 8% below the buy. There is no time limit.
- **Costs** are Coinbase Advanced's: a 0.60% taker fee per side plus an
  estimated spread, about 1.2–1.8% a round trip.
- It starts at $10,000 from `forward_log.start` (the first decision is an hour
  later, as that candle closes); the settings are under `policy` and
  `jev_paper` in `config/default.yaml`.

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
| [docs/jev_prompt.md](docs/jev_prompt.md) | The question Jev is asked and how its answer is read |

## Evaluation validity

Jev may have seen historical market data during training, so results on any
period before its release are **potentially contaminated**. That is why Jev is
judged only on the forward log: answers given hour by hour, scored once the
future they asked about has happened.
