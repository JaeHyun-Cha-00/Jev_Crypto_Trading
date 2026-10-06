# Jev decision questions

JevModel asks Jev four questions about the same market state in **one**
System One request. The questions are independent, so they run in parallel and
cannot see each other's answers (see TypeSafe's
[speculative fan-out](https://docs.typesafe.ai/patterns/fan-out.md)).
`default_questions()` in `src/jevtrade/decision/base.py` generates the text
below from config; `tests/test_jev.py` checks that this file matches it.

Jev only interprets the state. It never sees portfolio state, prices, dates or
tickers, and it never returns an action. The policy layer turns these
probabilities into trades, and all risk limits live in code.

## State

`state` is the anonymized JSON object built by `state/builder.py`: current
features (percent returns, realized volatility, volume z-score, RSI, distance
from moving averages), volatility and return percentiles, and the last bars'
return, range and volume z-score. It is sent as a JSON object, not a string,
because TypeSafe recommends named fields when state has several parts.

## Config

| Setting | `config/default.yaml` | Used in |
| :- | :- | :- |
| `decision.horizon_bars` | `24` (24 hours on 1h candles) | `direction`, `adverse_move` |
| `decision.flat_band_pct` | `3.0` | `direction`: up > +3%, down < -3%, otherwise flat |
| `decision.adverse_move_pct` | `8.0` (matches `policy.stop_loss_pct`) | `adverse_move` |
| `data.timeframe` | `1h` | horizon wording |

The band is wider than a round trip's costs (~1.2-1.8% on Coinbase Advanced),
so an `up`
answer is a move that still pays after costs. Until 2026-10-05 the questions
asked about 4 hours and a 1% band (adverse move 3%); a 4-hour move past 1%
averaged only about 2.5% on these coins, barely more than the costs.

## The four questions (as sent with config/default.yaml)

### 1. `direction` (Choice)

**Instructions:** Given the market state, where will this asset's close price be 24 hours (24 bars of 1h) after the most recent closed bar, relative to that bar's close?

**Criteria:**

- `up`: The close is more than 3% above the last close (change above +3%).
- `flat`: The change is between -3% and +3% inclusive.
- `down`: The close is more than 3% below the last close (change below -3%).

### 2. `regime` (Choice)

**Instructions:** Which description best fits this asset's market behaviour over the bars described in the state?

**Criteria:**

- `trend_up`: Persistent gains: price above its moving averages, pullbacks shallow.
- `trend_down`: Persistent losses: price below its moving averages, bounces shallow.
- `range`: No persistent direction: price near its moving averages, ordinary volatility.
- `volatile`: Large swings in both directions: realized volatility high relative to its own history.

### 3. `adverse_move` (Noul)

**Instructions:** Within the next 24 hours (24 bars of 1h) after the most recent closed bar, will this asset's price at any point trade more than 8% below that bar's close?

**Criteria:**

- `true`: Price dips more than 8% below the last close at some point in the window.
- `false`: Price never falls more than 8% below the last close in the window.

### 4. `clear_signal` (Noul)

**Instructions:** Does the state show a clear, coherent directional setup, with returns, moving-average distances, RSI and volume pointing the same way?

**Criteria:**

- `true`: The indicators agree on one direction and the move is distinct from noise.
- `false`: The indicators are mixed, contradictory, or too small to distinguish from noise.

## Reading the answers

- **Choice** (`direction`, `regime`): `probabilities` gives one probability
  per option, and `confidence` (0 to 1) says how concentrated that
  distribution is. The policy reads `p(up)` and `p(down)` from `direction`.
- **Noul** (`adverse_move`, `clear_signal`): `noul` **is** the probability
  that the answer is yes. TypeSafe returns no separate confidence for a Noul;
  a value near 0.5 is the model saying it is unsure
  ([Noul](https://docs.typesafe.ai/primitives/noul.md),
  [Confidence](https://docs.typesafe.ai/confidence.md#noul)). JevModel stores
  it as `{"true": p, "false": 1 - p}` and derives the documented
  confidence-style gate `|2p - 1|` as `gate_confidence`.
- The policy currently uses only `direction`. `regime`, `adverse_move` and
  `clear_signal` are stored for calibration and later policy rules.

## Storage

Every call is written to the `decisions` table (input hash, state text, model
version, raw response, latency, input tokens, cost) and every answer to
`decision_answers`: question, type, selected option, probabilities, raw
`noul`, model-reported `confidence`, and `gate_confidence`. Join on
`decisions.symbol` and `bar_ts` with later candles to check calibration.

## Failure handling

- If any question is missing from the answer, if the served model differs
  from the pinned `decision.jev.model`, or if the call fails after retries,
  the decision **abstains** and the policy does not trade.
- Auth, billing and bad-request errors (400, 401, 402, 403, 404) raise instead
  of abstaining, because they are configuration problems, not market signals.
