# Jev_Crypto_Trading

A personal research system that paper-trades crypto using **Jev** (TypeSafe AI)
as a probability model, with a deterministic Python policy layer that owns
every trading decision.

> **Paper trading only.** This system never places real orders. It uses no
> exchange API keys and calls no authenticated endpoints. Market data comes from
> public endpoints only. `tests/test_safety.py` enforces this.

> **Archived: version 1 (rule-based).** This branch keeps the system as it ran
> from 2026-10-06 to 2026-10-14. `main` has since moved to version 2, where Jev
> chooses every buy and sell itself with a fixed prompt. The forward log of
> this version stays on the `data-log` branch (schema 1 lines, up to the
> 2026-10-14T22:00Z candle).

## Version 1 results (2026-10-06 18:00Z to 2026-10-14 21:00Z)

82 coins, 196 hours, 16,070 Jev calls (no errors or abstains), 14,068 of them
scored 24 hours later. Model cost $1.33.

**Portfolio** (the replay in `src/jevtrade/forward/portfolio.py`, Coinbase Advanced costs)

| | Return |
|---|---|
| Jev account | **−14.3%** ($10,000 → $8,568; max drawdown −15.3%) |
| Equal-weight buy and hold, all 82 coins | −9.5% |
| BTC buy and hold | −4.5% |

- 62 closed trades, 9 winners (15%), −3.85% per trade on average.
- Of the $1,430 lost, $365 was fees and spread: the entries themselves lost
  about $1,065 before costs.
- 55 of 62 exits were model exits (p(down) ≥ 0.40), 7 were the 8% stop. Many
  positions were sold 1–4 hours after the buy at −6% to −9%.

**Calls**

- Direction hit rate **40.0%**, against **50.5%** for always answering
  "flat". Direction Brier 0.82 vs 0.61 for the realized base rates.
- The 1,258 calls that met the buy rule returned −0.50% over 24 hours, the
  same as the average coin (−0.52%): no edge.
- The surer Jev was, the worse it did: p(up) ≥ 0.8 returned −1.68% (34% up).
  Calls at 70%+ confidence were right only 47–53% of the time.
- Cross-sectional rank IC of p(up) − p(down) vs the 24-hour return: −0.07.

**Why**

Jev's direction answers tracked the last 24 hours: p(up) correlated +0.62
with the prior 24-hour return, p(down) −0.58, while the prior 24-hour return
said almost nothing about the next 24 hours (−0.03). So the account bought
after rises, and the first pullback lifted p(down) past the 0.40 exit: buy
high, sell low, a round trip of costs each time. Jev never saw the account,
so "sell" was only ever the same stateless 24-hour direction question read
by a rule with a lower bar (0.40) than the buy (0.60). Hit rates were 47–58%
on the rising days (10-09, 10-10) and 27–37% on the falling ones.

## Version 1 prompt

One System One request per coin and closed 1h candle, with the anonymized
market state (returns, volatility, volume z-score, RSI, distance from moving
averages, percentiles, the last 24 bars) and four independent questions. Jev
never saw prices, tickers, dates or the portfolio. Full text:
[docs/jev_prompt.md](docs/jev_prompt.md).

| Question | Type | Options | Used by the policy |
|---|---|---|---|
| `direction`: the close 24 hours later vs. the last close | Choice | up (> +3%), flat, down (< −3%) | buy and sell |
| `regime` | Choice | trend_up, trend_down, range, volatile | no |
| `adverse_move`: a dip of more than 8% within 24 hours | Noul | true / false | no |
| `clear_signal`: indicators agree on one direction | Noul | true / false | no |

Policy: buy when p(up) ≥ 0.60 and p(up) − p(down) ≥ 0.40, 12.5% of equity
(1% risk at the 8% stop), at most 1% of the coin's hourly dollar volume,
strongest signals first; sell when p(down) ≥ 0.40 and p(down) − p(up) ≥ 0.30,
or at the 8% stop. No time limit; the skill gate was off.

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
