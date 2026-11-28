"""The dashboard's view of the forward log: computed from the log, or read from a digest.

Every forward route of the API answers from a *view*:

- `LiveView` loads the whole log (jevtrade.forward.log) and replays the account
  in process. Tests and local runs use it.
- `DigestView` reads one small file, `dashboard/digest.json.gz` on data-log,
  that the hourly collect workflow builds after each run with
  `python -m jevtrade.forward.digest --log <data-log dir>`. The hosted
  dashboard uses it (forward_log.digest: true): a free Render instance (512 MB,
  0.1 CPU) can no longer load 100k+ log lines and replay them itself.

The digest holds exactly what the routes return: the summary, the replayed
account (its newest MAX_ACTIONS calls and MAX_HOURS hours) and each coin's
newest ROWS_PER_SYMBOL rows.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from .. import net
from ..data.timeframes import timeframe_ms
from ..net import HttpGet, NotFound, redact
from .agent import replay
from .log import ForwardLog
from .settings import ForwardLogConfig

log = logging.getLogger(__name__)

DIGEST_PATH = "dashboard/digest.json.gz"   # on the data-log branch
DIGEST_VERSION = 1
# The coin page asks for a coin's newest 200 rows, and the activity feed's 300
# newest rows (four hours of 82 coins) are among them.
ROWS_PER_SYMBOL = 200
MAX_ACTIONS = 5_000   # /api/forward/paper?actions= caps here too
MAX_HOURS = 720       # 30 days of the Activity page's hours; it shows the newest 168
_API = "https://api.github.com"
_RAW = "https://raw.githubusercontent.com"


def replay_account(rows: list[dict], app_cfg) -> dict:
    """The paper account replayed through `rows` from forward_log.start on, with `tracking_since`."""
    start = app_cfg.forward_log.start_ms()
    if start is not None:
        rows = [r for r in rows if r["candle_ts"] >= start]
    out = replay(rows, app_cfg, timeframe_ms(app_cfg.data.timeframe))
    out["tracking_since"] = start if start is not None else (out["curve"][0]["bar_ts"] if out["curve"] else None)
    return out


class LiveView:
    """The routes' answers computed from the forward log itself."""

    def __init__(self, fwd: ForwardLog, app_cfg):
        self.fwd, self.app_cfg = fwd, app_cfg
        self._paper: tuple[object, dict] | None = None
        self._lock = threading.Lock()

    def summary(self) -> dict:
        return self.fwd.summary()

    def rows(self, symbol: str | None, limit: int) -> list[dict]:
        return self.fwd.rows(symbol, limit)

    def paper(self) -> dict:
        snap = self.fwd.snapshot()
        # One replay at a time (the dashboard asks for /paper and /paper/detail at once),
        # and once per forward-log refresh. The old one is dropped first.
        with self._lock:
            if self._paper is None or self._paper[0] is not snap:
                self._paper = None
                self._paper = (snap, replay_account(snap.rows, self.app_cfg))
            return self._paper[1]


def build(view: LiveView) -> dict:
    """The digest: what every forward route returns, from one load and one replay."""
    summary = view.summary()
    paper = dict(view.paper())
    paper["actions"] = paper["actions"][:MAX_ACTIONS]
    paper["hours"] = paper["hours"][:MAX_HOURS]
    kept: Counter = Counter()
    rows = []
    for r in view.fwd.snapshot().rows:   # newest first
        if kept[r["symbol"]] < ROWS_PER_SYMBOL:
            kept[r["symbol"]] += 1
            rows.append(r)
    return {"version": DIGEST_VERSION, "built_at": time.time(), "summary": summary, "paper": paper, "rows": rows}


def write(digest: dict, path: Path) -> int:
    """Write `digest` as gzip JSON (mtime 0, so equal content gives equal bytes); returns the size."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = gzip.compress(json.dumps(digest, separators=(",", ":")).encode(), mtime=0)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(body)
    tmp.replace(path)
    return len(body)


class DigestView:
    """The routes' answers read from the digest the collect workflow builds (refreshed every
    forward_log.refresh_seconds; on a failed refresh the last good digest is kept)."""

    def __init__(self, app_cfg, http_get: HttpGet | None = None, clock: Callable[[], float] = time.time):
        self.app_cfg, self.cfg = app_cfg, app_cfg.forward_log
        self._get, self._clock = http_get, clock
        self._digest: dict | None = None
        self._fetched_at: float | None = None
        self._attempt_at: float | None = None
        self._error: str | None = None
        self._empty_paper: dict | None = None
        self._lock = threading.Lock()

    def _load(self) -> dict | None:
        with self._lock:
            now = self._clock()
            wait = self.cfg.refresh_seconds if self._error is None else min(self.cfg.refresh_seconds, 60)
            if self._attempt_at is None or now - self._attempt_at >= wait:
                self._attempt_at = now
                try:
                    t0 = time.monotonic()
                    d = json.loads(gzip.decompress(self._fetch()))
                    if d.get("version") != DIGEST_VERSION:
                        raise ValueError(f"digest version {d.get('version')!r}, expected {DIGEST_VERSION}")
                    self._digest, self._fetched_at, self._error = d, now, None
                    log.info("dashboard digest loaded: %d rows, built %.0f min ago, in %.1fs", len(d["rows"]),
                             (now - d["built_at"]) / 60, time.monotonic() - t0)
                except Exception as e:   # keep the last good digest
                    log.warning("dashboard digest refresh failed: %s", e)
                    self._error = redact(f"{type(e).__name__}: {e}")[:300]
            return self._digest

    def _fetch(self) -> bytes:
        c = self.cfg
        if c.source == "local":
            p = Path(c.local_dir) / DIGEST_PATH
            if not p.is_file():
                raise FileNotFoundError(f"no {p}: build it with python -m jevtrade.forward.digest --log {c.local_dir}")
            return p.read_bytes()
        get = self._get or net.get
        token = os.environ.get(c.token_env) if c.token_env else None
        headers = {"User-Agent": "jevtrade-api"}
        if token:   # works for private repos too
            url = f"{_API}/repos/{c.repo}/contents/{DIGEST_PATH}?ref={quote(c.branch, safe='')}"
            headers |= {"Accept": "application/vnd.github.raw", "Authorization": f"Bearer {token}"}
        else:
            url = f"{_RAW}/{c.repo}/{quote(c.branch)}/{DIGEST_PATH}"
            headers["Accept"] = "*/*"
        try:
            return get(url, headers, c.timeout_s)
        except NotFound:
            raise NotFound(f"no {DIGEST_PATH} on {c.repo}@{c.branch} yet: "
                           "the collect workflow builds it after each run") from None

    def summary(self) -> dict:
        d = self._load()
        if d is None:   # nothing loaded yet: the empty summary, with the reason
            s = ForwardLog(self.cfg.model_copy(update={"source": "off"})).summary()
        else:
            s = dict(d["summary"])
        location = f"{self.cfg.repo}@{self.cfg.branch}" if self.cfg.source == "github" else self.cfg.local_dir
        s.update(source=self.cfg.source, location=location, error=self._error, fetched_at=self._fetched_at,
                 built_at=d["built_at"] if d else None)
        return s

    def rows(self, symbol: str | None, limit: int) -> list[dict]:
        d = self._load()
        rows = d["rows"] if d else []
        if symbol:
            rows = [r for r in rows if r["symbol"] == symbol]
        return rows[:limit]

    def paper(self) -> dict:
        d = self._load()
        if d is not None:
            return d["paper"]
        if self._empty_paper is None:   # a fresh account until the first digest arrives
            self._empty_paper = replay_account([], self.app_cfg)
        return self._empty_paper


def main(argv: list[str] | None = None) -> None:
    from ..config import load_config

    p = argparse.ArgumentParser(description="Build the dashboard digest from a data-log checkout.")
    p.add_argument("--log", required=True, help="the data-log directory (decisions/, outcomes/)")
    p.add_argument("--config", help="config YAML (default: config/default.yaml)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = load_config(args.config)
    fwd_cfg = cfg.forward_log.model_copy(update={"source": "local", "local_dir": args.log})
    view = LiveView(ForwardLog(fwd_cfg, round_trip=2 * cfg.jev_paper.typical_side_cost()), cfg)
    t0 = time.monotonic()
    digest = build(view)
    if digest["summary"].get("error"):
        raise SystemExit(f"forward log unreadable: {digest['summary']['error']}")
    size = write(digest, Path(args.log) / DIGEST_PATH)
    print(f"wrote {DIGEST_PATH}: {len(digest['rows']):,} rows, {len(digest['paper']['hours'])} hours, "
          f"{size / 1e6:.1f} MB in {time.monotonic() - t0:.1f}s")


if __name__ == "__main__":
    main()
