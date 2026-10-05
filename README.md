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
| `data/` | Public OHLCV via ccxt → SQLite (incremental, gap backfill, UTC); the live coin list and Robinhood quotes (`live.py`); the dashboard's live Coinbase market data (`market.py`) |
| `features/` | Closed-candle features, no look-ahead |
| `state/` | Anonymized JSON market state for the model |
| `decision/` | `DecisionModel`: Mock, Baseline, Jev (via OpenRouter) |
| `policy/` | Probabilities → actions, risk limits |
| `sim.py` | Simulated account shared by the backtest, the paper loop and the Jev portfolio |
| `backtest/` | Event-driven walk-forward backtester |
| `paper/` | Live paper loop, restart-safe, simulated fills only |
| `report/` | Daily Markdown report: account, trades, decisions, calibration |
| `collect/` | Hourly forward log of Jev's answers and outcomes (GitHub Actions → `data-log` branch), and `kick.py`, which starts the run when GitHub skips it |
| `forward/` | Reading the forward log back (`log.py`), scoring it, and replaying Jev's paper account from it (`portfolio.py`) |
| `api/` | Read-only FastAPI over all of the above, and the hosted-site extras (`hosting.py`) |
| `net.py` | The plain HTTPS GET the readers above share |
| `config.py` | Typed settings loaded from `config/default.yaml` |

Elsewhere: `web/` is the React dashboard (`src/pages/`, `src/components/`,
`src/lib/` for the API client and formatting), `tests/` mirrors the modules,
`config/` holds the YAML, `docs/` the longer guides, and the Dockerfiles,
`docker-compose.yml` and `render.yaml` at the root run it.

## Quick start (Docker)

```bash
cp .env.example .env              # optional; only needed for a private repo's forward log
docker compose up -d --build      # read-only API + dashboard
```

Then open http://localhost:8080 (or put it on a free public URL: see
[Live website](docs/deploy.md#live-website-free-on-render)). See
[Running on a fresh Linux VM](docs/deploy.md#running-on-a-fresh-linux-vm) for a server.

## Setup

Requires Python 3.11 or newer.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'   # or '.[api]' for just the runtime plus the API server
cp .env.example .env   # only needed for decision.model: jev, or a private repo's forward log
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
  symbols: [BTC/USD, ETH/USD, ...]  # 81 coins: Robinhood-tradable with a Coinbase USD market
  symbols_live: true         # re-read that list from Robinhood and Coinbase each run; symbols is the fallback
  exclude: [PAXG/USD]        # never tracked, even when listed
  timeframe: 1h
  start: "2025-01-01T00:00:00Z"   # earliest candle kept; backfilled on coinbase
  page_limit: 300            # candles per request, clamped per exchange
  requests_trust_env: true   # honour HTTPS_PROXY / REQUESTS_CA_BUNDLE
storage:
  sqlite_path: data/jevtrade.sqlite
```

## Docs

| File | What it covers |
|---|---|
| [docs/stages.md](docs/stages.md) | Running each stage: data, features, state and models, backtest, paper loop, report, API, dashboard |
| [docs/forward-log.md](docs/forward-log.md) | The hourly forward log of Jev's answers: the collect workflow, collect-kick, the `data-log` branch |
| [docs/deploy.md](docs/deploy.md) | The live website on Render and running on a fresh Linux VM |
| [docs/jev_prompt.md](docs/jev_prompt.md) | The question Jev is asked and how its answer is read |

## Evaluation validity

Jev may have seen historical market data during training. Any backtest on a
period before Jev's release is therefore **potentially contaminated** and
will be labelled as such. Forward paper trading after the release is the
primary evaluation.
