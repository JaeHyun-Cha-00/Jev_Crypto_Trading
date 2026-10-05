"""CLI: the live paper loop. Paper only; it never places real orders.

    python -m jevtrade.paper run [--once] [--allow-live-model] [--config PATH]
    python -m jevtrade.paper status [--config PATH]

`run` syncs public candles and processes every newly closed bar, then sleeps
until the next candle closes. It is meant to run unattended on your own
machine or a small VM (see docs/stages.md, "7. Paper loop"). It is safe to
stop at any time (Ctrl-C, SIGTERM, a reboot): state lives in SQLite and each
bar commits atomically, so the next start resumes where it left off.

`decision.model: jev` makes one paid API call per symbol per bar, so it also
needs `--allow-live-model` or JEVTRADE_ALLOW_LIVE_MODEL=1.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
import time
from datetime import date, datetime, timezone

from ..config import load_config
from ..data.fetcher import public_exchange
from ..data.store import connect
from ..data.timeframes import ms_to_iso, timeframe_ms
from ..decision.factory import build_model
from ..report.daily import write_report
from .engine import PaperLock, PaperLockError, PaperTrader, seconds_until_next_close
from .store import PaperStore

log = logging.getLogger("jevtrade.paper")


def _run(args) -> None:
    cfg = load_config(args.config)
    live_ok = args.allow_live_model or os.environ.get("JEVTRADE_ALLOW_LIVE_MODEL") == "1"
    if cfg.decision.model == "jev" and not live_ok:
        raise SystemExit("decision.model is jev, which calls the paid Jev API once per symbol per bar; "
                         "pass --allow-live-model (or set JEVTRADE_ALLOW_LIVE_MODEL=1) to run it")
    lock_path = cfg.paper.lock_path or f"{cfg.storage.sqlite_path}.paper.lock"
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    try:
        with PaperLock(lock_path):
            conn = connect(cfg.storage.sqlite_path)
            trader = PaperTrader(cfg, conn, build_model(cfg.decision),
                                 public_exchange(cfg.data.exchange, cfg.data.requests_trust_env))
            tf_ms = timeframe_ms(cfg.data.timeframe)
            log.info("paper loop: run %s, model %s (%s), symbols %s, store %s",
                     cfg.paper.run_id, trader.model.name, trader.model.version,
                     ", ".join(cfg.data.symbols), cfg.storage.sqlite_path)
            while not stop.is_set():
                try:
                    r = trader.step()
                    for t in r.trades:
                        log.info("closed %s: pnl %.2f (%s)", t.symbol, t.pnl, t.exit_reason)
                    log.info("processed %d bar(s)%s%s; equity %s", len(r.processed),
                             f", {len(r.risk_only)} risk-only" if r.risk_only else "",
                             f"; waiting on {', '.join(r.waiting_on)}" if r.waiting_on else "",
                             f"{r.equity:,.2f}" if r.equity is not None else "n/a")
                    if cfg.paper.daily_report:
                        for day in completed_days(r.processed, tf_ms):
                            log.info("wrote %s", write_report(conn, cfg, cfg.paper.run_id, day, cfg.report))
                    wait = seconds_until_next_close(int(time.time() * 1000), tf_ms, cfg.paper.poll_delay_s)
                    if r.waiting_on:
                        wait = min(wait, cfg.paper.retry_s)
                except SystemExit:
                    raise
                except Exception:  # network errors, exchange hiccups: log and retry
                    log.exception("step failed; retrying in %ss", cfg.paper.retry_s)
                    wait = cfg.paper.retry_s
                if args.once:
                    break
                stop.wait(wait)
    except PaperLockError as e:
        raise SystemExit(str(e)) from None
    log.info("paper loop stopped")


def completed_days(processed: list[int], tf_ms: int) -> list[date]:
    """UTC days whose last bar is among `processed`."""
    return sorted({datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()
                   for ts in processed if (ts + tf_ms) % 86_400_000 == 0})


def _status(args) -> None:
    cfg = load_config(args.config)
    conn = connect(cfg.storage.sqlite_path)
    store = PaperStore(conn, cfg.paper.run_id)
    loaded = store.load()
    if loaded is None:
        print(f"paper run {cfg.paper.run_id!r}: not started")
        return
    st, model, version = loaded
    hb = conn.execute("SELECT started_at, heartbeat_at FROM paper_state WHERE run_id=?",
                      (cfg.paper.run_id,)).fetchone()
    trades = store.trades()
    print(f"paper run {cfg.paper.run_id!r}: model {model} ({version}), started {hb[0]}, heartbeat {hb[1]}")
    print(f"last bar {ms_to_iso(st.last_bar_ts)}; equity {st.equity():,.2f}; cash {st.cash:,.2f}; "
          f"{len(trades)} closed trades, pnl {sum(t.pnl for t in trades):,.2f}")
    for s, o in st.open.items():
        print(f"  open {s}: qty {o.pos.qty:.6g} @ {o.pos.entry_price:.6g}, stop {o.pos.stop_price:.6g}, "
              f"mark {st.marks.get(s, o.pos.entry_price):.6g}")
    for s, p in st.pending.items():
        print(f"  pending {p.action.kind} {s} at next open: {p.action.reason}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Paper-trading loop (simulated fills only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="process closed candles, then sleep until the next close")
    r.add_argument("--config", default=None)
    r.add_argument("--once", action="store_true", help="process what is ready, then exit")
    r.add_argument("--allow-live-model", action="store_true",
                   help="required when decision.model is jev (paid API call per symbol per bar)")
    s = sub.add_parser("status", help="print the paper account")
    s.add_argument("--config", default=None)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    _run(args) if args.cmd == "run" else _status(args)


if __name__ == "__main__":
    main()
