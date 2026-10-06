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
| `data/` | Public OHLCV via ccxt (`fetcher.py`); the live coin list and Robinhood quotes (`live.py`); the dashboard's live Coinbase market data (`market.py`) |
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
`config/` holds the YAML, `docs/` the longer guides, and `Dockerfile.hosted`
with `render.yaml` at the root deploy it.

## How it runs

- **GitHub Actions** (`.github/workflows/collect.yml`) asks Jev about every
  tracked coin each hour and commits the answers to the `data-log` branch.
- **Render** (`render.yaml`) hosts the dashboard and restarts a skipped hourly
  run: see [docs/deploy.md](docs/deploy.md).

Nothing needs to run on your own computer.

## Setup

Requires Python 3.11 or newer.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'   # or '.[api]' for just the runtime plus the API server
cp .env.example .env   # only needed to call Jev yourself, or for a private repo's forward log
pytest
```

To run the dashboard locally: `python -m jevtrade.api` in one terminal and
`cd web && npm ci && npm run dev` in another, then open http://localhost:5173.

## Configuration

Settings live in `config/default.yaml`, which is validated by pydantic models
in `src/jevtrade/config.py`. Pass `--config path.yaml` to any CLI to use another
file. Secrets are read **only** from environment variables (`.env`, which git
ignores), never from YAML.

```yaml
data:
  exchange: coinbaseexchange # api.exchange.coinbase.com; or kraken
  symbols: [BTC/USD, ETH/USD, ...]  # 81 coins: Robinhood-tradable with a Coinbase USD market
  symbols_live: true         # re-read that list from Robinhood and Coinbase each run; symbols is the fallback
  exclude: [PAXG/USD]        # never tracked, even when listed
  timeframe: 1h
  page_limit: 300            # candles per request, clamped per exchange
  requests_trust_env: true   # honour HTTPS_PROXY / REQUESTS_CA_BUNDLE
```

## Docs

| File | What it covers |
|---|---|
| [docs/forward-log.md](docs/forward-log.md) | The hourly forward log of Jev's answers: the collect workflow, collect-kick, the `data-log` branch |
| [docs/deploy.md](docs/deploy.md) | The live website on Render |
| [docs/jev_prompt.md](docs/jev_prompt.md) | The question Jev is asked and how its answer is read |

## Evaluation validity

Jev may have seen historical market data during training, so results on any
period before its release are **potentially contaminated**. That is why Jev is
judged only on the forward log: answers given hour by hour, scored once the
future they asked about has happened.
