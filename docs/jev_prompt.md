# Jev's trading prompt

Jev trades its paper account itself. Every hour, just after each 1h candle
closes, the collector asks Jev about each coin once, with one fixed question:
`entry` for a coin the account doesn't hold, `manage` for one it does. Jev's
choice is the trade. Code only executes it (`src/jevtrade/forward/agent.py`),
within the cash the account has: there is no stop-loss, time limit, position
cap or probability threshold.

The prompt is version **`agent-1`**. `src/jevtrade/decision/agent.py`
generates the text below from config (`jev_paper.sizes` and the costs), and
`tests/test_agent.py` checks that this file matches it. Every decision line in
the log records the version it was asked with (`prompt_version`). Change the
version whenever the wording changes.

## State

`state` is a JSON object with four parts:

| Field | What it holds |
| :- | :- |
| `mandate` | The standing instructions below, the same for every call |
| `market` | The coin's anonymized market state from `state/builder.py`: percent returns, realized volatility, volume z-score, RSI, distance from moving averages, percentiles within the trailing 720 bars, and the last 24 bars' return, range and volume z-score. No dates, prices or tickers. |
| `account` | `cash_pct_of_equity` (cash as a share of the account) and `assets_held` (coins held) |
| `position` | `null` when the account doesn't hold the coin; otherwise `hours_held`, `return_since_buy_pct` (the price since the buy, before costs), `best_since_buy_pct` and `worst_since_buy_pct` (the highest and lowest the price has been since the buy), and `share_of_equity_pct` |

Each decision line keeps the account and position Jev was shown as `shown`.

## Mandate

> You run a long-only spot crypto account and decide for one asset at a time, once an hour, just after each 1h bar closes. You can only buy the asset with cash and hold it, or sell it back to cash: no shorting, no leverage. Every buy and every sell costs about 0.8% of the amount traded (taker fee plus spread), so a round trip costs about 1.5%. Your goal is the account's profit after costs; trading has no value in itself. Buy only when you expect the move to more than repay the costs, and sell only when holding no longer pays. Nothing else will sell for you: there is no stop-loss and no time limit, so a position you hold stays open until you sell it.

The costs it quotes are the taker fee plus a mid-depth coin's spread
(`JevPaperConfig.typical_side_cost`). The account charges each coin its own
estimated spread, so a deep coin costs a little less and a thin one more.

## The two questions

### 1. `entry` (Choice): a coin the account doesn't hold

**Instructions:** You do not hold this asset (`position` is null). Under the mandate in `mandate`, and given the asset's `market` state and your `account`, what do you do now?

**Criteria:**

- `buy_small`: Buy about 5% of the account's equity: an edge that should repay the 1.5% round trip, but with real doubt.
- `buy_normal`: Buy about 10% of the account's equity: a clear edge that should repay the costs with room to spare.
- `buy_large`: Buy about 20% of the account's equity: a strong, unusually clear edge.
- `wait`: Do not buy: no edge large enough to repay the costs, or the setup is unclear.

### 2. `manage` (Choice): a coin the account holds

**Instructions:** You hold this asset (see `position`). Under the mandate in `mandate`, and given the asset's `market` state, your `position` and your `account`, do you keep it or sell it now?

**Criteria:**

- `hold`: Keep it: the reason to own it still stands, and holding should pay more than selling now. A pullback or a quiet stretch that doesn't break that reason is not a reason to sell.
- `sell`: Sell all of it now: the reason to own it has broken, or the gain to expect from here no longer justifies the risk of holding. Selling costs about 0.8% of the position.

## How the account executes an hour

1. Every `sell` fills first, at the Coinbase price logged when Jev was asked
   (`cb_price`), or the candle's close for a backfilled hour.
2. Then every buy, the most confident first (the probability of the chosen
   size), at that size's share of the equity after the sells. A buy is cut to
   the cash left and skipped when the cash is gone (`no_cash`).
3. `wait`, `hold`, an abstain or a failed call does nothing.

Each fill pays the taker fee (`jev_paper.fee_bps`) and the coin's estimated
spread. An hour's answers are final: a failed call is not asked again, and the
collector never asks about an hour older than the newest one logged, because
later positions were shown to Jev on top of it.

## Scoring

The **Accuracy** page scores each call 24 hours later (`decision.horizon_bars`):
a buy was right when the price then rose by more than a round trip of costs,
a wait when it didn't; a hold was right when the price then rose, a sell when
it didn't.

## Reading the answers

`probabilities` gives one probability per option and `confidence` (0 to 1)
says how concentrated that distribution is
([Choice](https://docs.typesafe.ai/primitives/choice.md),
[Confidence](https://docs.typesafe.ai/confidence.md)). The account acts on
`choice`, the most probable option, and orders buys by its probability.

## Failure handling

- If the answer is missing, if the served model differs from the pinned
  `decision.jev.model`, or if the call fails after retries, the call
  **abstains** and nothing is traded for that coin that hour.
- Auth, billing and bad-request errors (400, 401, 402, 403, 404) raise instead
  of abstaining, because they are configuration problems, not market signals.

## Before `agent-1`

Until 2026-10-14 Jev answered four questions (24-hour direction at a 3% band,
regime, an 8% adverse move, a clear signal) and a rule-based policy traded on
them: buy at p(up) ≥ 0.60 and an edge of 0.40, sell at p(down) ≥ 0.40, an 8%
stop. Over 2026-10-06 to 10-14 that account lost 14.3% while the average coin
lost 9.5%: Jev never saw its positions, and it sold most of them within hours
of buying. Those lines (schema 1) stay on `data-log`, and the code is on the
`v1-rule-based` branch, whose README summarizes the results.
