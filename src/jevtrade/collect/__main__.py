"""CLI: one forward-collection pass (run hourly by .github/workflows/collect.yml).

    python -m jevtrade.collect --out DIR [--config PATH] [--max-backfill 24] [--require-key]

Asks Jev about every closed candle of the tracked symbols (`data.symbols`), within the last
`--max-backfill` candles, that DIR has no answer for yet, then logs realized
outcomes for decisions whose horizon has closed. Each Jev call costs money.
`--require-key` stops before any call unless $OPENROUTER_API_KEY is set; the
key's value is never read here.
"""

from __future__ import annotations

import argparse
import logging
import os

from ..config import load_config
from ..data.fetcher import public_exchange
from ..decision.jev import JevModel
from .collector import Collector


def main() -> None:
    ap = argparse.ArgumentParser(description="Log Jev answers for recent closed candles")
    ap.add_argument("--out", required=True, help="log directory (the data-log checkout)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--max-backfill", type=int, default=24,
                    help="closed candles per symbol to check, newest first (default 24)")
    ap.add_argument("--require-key", action="store_true",
                    help="fail unless the API key environment variable is set")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if args.max_backfill < 1:
        ap.error("--max-backfill must be at least 1")

    cfg = load_config(args.config)
    key_env = cfg.decision.jev.api_key_env
    if args.require_key and not os.environ.get(key_env):
        raise SystemExit(f"${key_env} is not set; add it as a repository secret")

    source = public_exchange(cfg.data.exchange, cfg.data.requests_trust_env)
    res = Collector(cfg, JevModel(cfg.decision.jev), source, args.out,
                    max_backfill=args.max_backfill).run()
    print(f"jev_calls={len(res.called)} errors={len(res.errors)} "
          f"outcomes={len(res.outcomes)} failed_symbols={len(res.failed_symbols)} "
          f"cost_usd={res.cost_usd:.6f}")
    if res.errors and len(res.errors) == len(res.called):
        raise SystemExit("every Jev call failed; see the log above")


if __name__ == "__main__":
    main()
