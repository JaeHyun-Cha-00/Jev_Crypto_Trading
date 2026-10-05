# Jev decision questions

JevModel asks Jev four questions about the same market state in **one**
System One request. The questions are independent, so they run in parallel and
cannot see each other's answers (see TypeSafe's
[speculative fan-out](https://docs.typesafe.ai/patterns/fan-out.md)). The
source of truth for the wording is `default_questions()` in
`src/jevtrade/decision/base.py`; this page explains the intent.

Jev only interprets the state. It never sees portfolio state, prices, dates or
tickers, and it never returns an action. The policy layer turns these
probabilities into trades, and all risk limits live in code.

## State

`state` is the anonymized JSON object built by `state/builder.py`: current
features (percent returns, realized volatility, volume z-score, RSI, distance
from moving averages), volatility and return percentiles, and the last bars'
return, range and volume z-score. It is sent as a JSON object, not a string,
because TypeSafe recommends named fields when state has several parts.

`H` below is `decision.horizon_bars`, `F` is `decision.flat_band_pct` and `A`
is `decision.adverse_move_pct`.

## The four questions

| id | type | answers | used by |
| :- | :- | :- | :- |
| `direction` | Choice | `up` / `flat` / `down` | policy entry and exit |
| `regime` | Choice | `trend_up` / `trend_down` / `range` / `volatile` | logged for analysis |
| `adverse_move` | Noul | `true` / `false` | logged for analysis |
| `clear_signal` | Noul | `true` / `false` | logged for analysis |

### 1. `direction` (Choice)

Where will the close be `H` bars after the most recent closed bar, relative to
that bar's close?

- `up`: higher by more than `F`%.
- `flat`: within ±`F`%.
- `down`: lower by more than `F`%.

The policy consumes `p(up)` and `p(down)` exactly as it does for MockModel and
BaselineModel.

### 2. `regime` (Choice)

Which description best fits the market's behaviour over the bars in the state?

- `trend_up`: persistent gains, price above its moving averages, pullbacks shallow.
- `trend_down`: persistent losses, price below its moving averages, bounces shallow.
- `range`: no persistent direction, price near its moving averages, ordinary volatility.
- `volatile`: large swings in both directions, realized volatility high relative to its own history.

### 3. `adverse_move` (Noul)

Within the next `H` bars, will price at any point trade more than `A`% below
the most recent close? This is the probability that a long opened now would be
stopped out at an `A`% stop. `A` defaults to 3.0 to match `policy.stop_loss_pct`.

### 4. `clear_signal` (Noul)

Does the state show a clear, coherent directional setup (returns, trend
distances, RSI and volume pointing the same way), as opposed to mixed or
noisy evidence? This is a separate presence judgment, so the policy can later
require it on top of `direction` without re-asking the direction question.

## Response handling

- A Choice answer becomes `{option: probability}`; a Noul answer becomes
  `{"true": p, "false": 1 - p}`.
- If any question is missing from the answer, if the served model differs
  from the pinned `decision.jev.model`, or if the call fails after retries,
  the decision **abstains** and the policy does not trade.
- Auth, billing and bad-request errors (400, 401, 402, 403, 404) raise instead
  of abstaining, because they are configuration problems, not market signals.

## Assumption

The original task referred to this file, but it was not in the repository, so
these four questions were chosen as a reasonable default: the existing
`direction` question that the policy already uses, plus three that help the
policy's risk logic later (regime, stop risk, signal clarity). Change the
wording here and in `default_questions()` together.
