# data-log

Written by the hourly `collect` workflow on main (`python -m jevtrade.collect`).
Jev trades a paper account here: each line is its choice for one coin and
hour. Nothing places real orders.

- `decisions/YYYY-MM-DD.jsonl`: one line per Jev call, per symbol and closed 1h
  candle (day = the candle's open time, UTC), with that candle's `open`, `high`,
  `low`, `close` and `volume` (base units) and `called_at`, when the call was made.
  From schema 2 on, `question` is the question asked (`entry` when the account
  didn't hold the coin, `manage` when it did), `shown` is the account and
  position Jev was shown, and `prompt_version` names the prompt's wording.
  Lines for the newest candle of a run also carry the coin's Coinbase price at
  the start of that run (`cb_price`, `cb_price_at`; absent when Coinbase couldn't
  be read): that hour's trades fill at it. `status` is `answered`, `abstain`
  (Jev responded but the answer was unusable) or `error` (no response); either
  of the last two does nothing. `answers` holds every answer as Jev returned it.
  Schema 1 lines (before the agent prompt) answered the old direction questions.
- `state/YYYY-MM-DD.jsonl.gz`: the state text Jev was shown for each call,
  keyed by `symbol`, `candle_ts` and `input_hash` (gzip; `zcat` reads it).
- `outcomes/YYYY-MM-DD.jsonl`: the realized outcome of each decision once its
  horizon has closed, keyed by `symbol` and `candle_ts`.
