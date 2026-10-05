"""CLI: daily Markdown report for a paper run.

    python -m jevtrade.report [--day YYYY-MM-DD] [--run-id paper] [--config PATH]

Defaults to yesterday (UTC), the last complete day. Writes
`report.output_dir/<run id>/<day>.md` and `.json` and prints the Markdown.
The paper loop also writes each day's report when the day ends
(`paper.daily_report`).
"""

from __future__ import annotations

import argparse
import time
from datetime import date

from ..config import load_config
from ..data.store import connect
from .daily import previous_utc_day, write_report


def main() -> None:
    ap = argparse.ArgumentParser(description="Write the daily paper-trading report")
    ap.add_argument("--config", default=None)
    ap.add_argument("--day", default=None, help="UTC day, YYYY-MM-DD (default: yesterday)")
    ap.add_argument("--run-id", default=None, help="default: paper.run_id")
    args = ap.parse_args()
    cfg = load_config(args.config)
    day = date.fromisoformat(args.day) if args.day else previous_utc_day(int(time.time() * 1000))
    path = write_report(connect(cfg.storage.sqlite_path), cfg, args.run_id or cfg.paper.run_id, day, cfg.report)
    print(path.read_text())
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
