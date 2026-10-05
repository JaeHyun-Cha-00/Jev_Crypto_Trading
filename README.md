# data-log

Written by the hourly `collect` workflow on main (`python -m jevtrade.collect`).
Nothing here trades or simulates positions.

- `decisions/YYYY-MM-DD.jsonl`: one line per Jev call, per symbol and closed 1h
  candle (day = the candle's open time, UTC). `status` is `answered`, `abstain`
  (Jev responded but the answer was unusable) or `error` (no response; that
  candle is asked again on a later run). `answers` holds every answer as Jev
  returned it; a Noul's `noul` is P(yes).
- `outcomes/YYYY-MM-DD.jsonl`: the realized outcome of each decision once its
  horizon has closed, keyed by `symbol` and `candle_ts`.
