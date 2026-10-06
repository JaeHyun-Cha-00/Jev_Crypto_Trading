# data-log

Written by the hourly `collect` workflow on main (`python -m jevtrade.collect`).
Nothing here trades or simulates positions.

- `decisions/YYYY-MM-DD.jsonl`: one line per Jev call, per symbol and closed 1h
  candle (day = the candle's open time, UTC), with that candle's `open`, `high`,
  `low`, `close` and `volume` (base units; older lines have only `close`) and `called_at`, when the call was made.
  Lines for the newest candle of a run also carry the coin's Coinbase price at
  the start of that run (`cb_price`, `cb_price_at`; absent when Coinbase couldn't
  be read). Lines from 2026-10-05 and 2026-10-06 may carry a Robinhood quote
  (`rh_bid`, `rh_ask`, `rh_quote_at`); it is no longer logged. `status` is `answered`, `abstain`
  (Jev responded but the answer was unusable) or `error` (no response; that
  candle is asked again on a later run). `answers` holds every answer as Jev
  returned it; a Noul's `noul` is P(yes).
- `state/YYYY-MM-DD.jsonl.gz`: the state text Jev was shown for each call,
  keyed by `symbol`, `candle_ts` and `input_hash` (gzip; `zcat` reads it).
  Decision lines from before 2026-10-05 still carry it inline as `state`.
- `outcomes/YYYY-MM-DD.jsonl`: the realized outcome of each decision once its
  horizon has closed, keyed by `symbol` and `candle_ts`.
