# Forward data collection (GitHub Actions)

`.github/workflows/collect.yml` runs at 1 minute past every hour (and on
demand via **Run workflow**). Each run:

- reads every coin's current Coinbase price in one call and logs it on the
  newest candle's line (`cb_price`, `cb_price_at`), so the dashboard's
  portfolio fills a decision right away at the price it was made at;
- fetches recent public 1h candles from Coinbase for every coin in `data.symbols`;
- asks Jev (the pinned snapshot) the configured questions once for each closed
  candle that has no answer logged yet, looking back at most 24 candles, so a
  skipped or delayed run is backfilled and no candle is asked twice;
- logs the realized outcome of each decision once its horizon has closed;
- commits the JSONL files to the orphan `data-log` branch, never to main.

`forward_log.start` is a fresh-start cutoff: the collector never asks about a
candle before it (the backfill stops there), and the API ignores earlier lines,
so the portfolio starts at its full balance and the accuracy stats start empty.
To restart, move it to a later hour; earlier lines stay on `data-log`.

No trading and no simulated positions. Calls that got no response at all are
logged with `status: "error"` and asked again on the next run. The key comes
from the `OPENROUTER_API_KEY` repository secret (Settings → Secrets and
variables → Actions); the run stops before any call if it is missing. Each
call costs money: about $0.00008 at the recorded ~2,000 input tokens, so the
default 82 coins hourly is roughly $0.16 a day (about $4.70 a month). Trim
`data.symbols` to spend less. A coin whose candles can't be fetched is skipped
for that run and retried the next hour; the other coins still run.

Locally, against any directory:

```bash
python -m jevtrade.collect --out data-log --max-backfill 24
```

GitHub can delay or skip scheduled runs. The Render service runs a backstop
(`JEVTRADE_KICK=1`, see the README's "Live website"): from 3 minutes past each hour it asks GitHub
whether a collect run started this hour and, if none did, starts one (the run
still happens in GitHub Actions). It needs `GITHUB_TOKEN` with **Actions: read
and write** on the repo; without a token it exits and stays stopped. Check it in the Render service's logs, or run
one check by hand with `python -m jevtrade.collect.kick --once`.

## Accuracy page

The API reads the `data-log` branch, and the dashboard's **Accuracy** page
scores each call once the `horizon_bars` it asked about have passed (24 hours,
so a call made at 10:00 is scored at 10:00 the next day):

- tiles for scored calls (with pending, abstain and error counts), Jev's
  direction hit rate marked ▲/▼ against the best naive baseline, direction
  Brier next to the Brier of the realized base rates, and the cost so far;
- Jev's hit rate next to "always flat" and "always the most common realized
  class" (a hindsight baseline);
- a confusion matrix (Jev's call vs. what happened) and a calibration table
  (Jev's direction confidence in bins vs. hit rate), plus log loss and the
  adverse-move Brier (the `adverse_move` Noul is P(yes));
- the recent calls: when Jev decided (just after the candle closed), symbol,
  call and confidence, regime, P(adverse), realized direction and return, and
  hit, miss or when the result is due.

Until the first call is scored, the page says when that will be instead of
showing empty tables.

It follows the page's symbol filter and refresh. Configure it under
`forward_log` in `config/default.yaml`:

| Key | Default | Meaning |
|---|---|---|
| `source` | `github` | `github` (HTTPS), `local` (a directory) or `off` |
| `repo`, `branch` | this repo, `data-log` | where `source: github` reads |
| `local_dir` | `data-log` | for `source: local`, e.g. `git worktree add data-log origin/data-log` |
| `refresh_seconds` | `300` | re-read at most this often; a failed refresh keeps the last good data and shows the error |
| `max_days` | `30` | newest day files to load |
| `token_env` | `GITHUB_TOKEN` | env var with an optional GitHub token |

The GitHub source lists files with the contents API and downloads only new
or changed day files. The repo is public, so no token is needed; a
`GITHUB_TOKEN` with read-only Contents access lifts GitHub's rate limit (and
would be required if the repo were made private again, in which case the page
says whether the token is missing or lacks access). It only reads: no writes, model calls or exchange calls.
