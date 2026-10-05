"""Hourly backstop for the collect workflow's GitHub schedule.

GitHub may delay or skip scheduled runs. This loop, run next to the API on an
always-on box, asks GitHub from minute `--minute` of every hour on whether a
collect run already started this hour, and starts one with workflow_dispatch
only if none did. It re-checks the clock every few minutes, so an hour is
still covered when the box slept through `--minute` or the container restarted
after it. It calls nothing but the GitHub API; the run itself (and
every Jev call) happens in GitHub Actions, as with the schedule.

    python -m jevtrade.collect.kick [--minute 1] [--once]

Needs $GITHUB_TOKEN with "Actions: read and write" on the repo (a fine-grained
token; add "Contents: read" so the API's forward log keeps working).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..api.forward import _redact, _ssl_context
from ..config import load_config

log = logging.getLogger(__name__)

_API = "https://api.github.com"
WORKFLOW = "collect.yml"

# (method, url, headers, body) -> (status, body)
Http = Callable[[str, str, dict, bytes | None], tuple[int, bytes]]


def _http(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20, context=_ssl_context()) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class Kicker:
    def __init__(self, repo: str, token: str, ref: str = "main", http: Http = _http):
        self.repo = repo
        self.ref = ref
        self.http = http
        self.headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
                        "User-Agent": "jevtrade-kick", "X-GitHub-Api-Version": "2022-11-28"}

    def _runs_since(self, since: datetime) -> list[dict]:
        url = (f"{_API}/repos/{self.repo}/actions/workflows/{WORKFLOW}/runs"
               f"?created=%3E%3D{since.strftime('%Y-%m-%dT%H:%M:%SZ')}&per_page=5")
        status, body = self.http("GET", url, self.headers, None)
        if status != 200:
            raise RuntimeError(f"listing runs: HTTP {status}: {_redact(body.decode(errors='replace'))[:200]}")
        return json.loads(body).get("workflow_runs", [])

    def tick(self, now: datetime) -> str:
        """Start a collect run unless one already started this hour. Returns what happened."""
        hour = now.replace(minute=0, second=0, microsecond=0)
        runs = self._runs_since(hour)
        if runs:
            r = runs[0]
            return f"skip: run {r.get('id')} ({r.get('event')}, {r.get('status')}) started this hour"
        url = f"{_API}/repos/{self.repo}/actions/workflows/{WORKFLOW}/dispatches"
        status, body = self.http("POST", url, self.headers, json.dumps({"ref": self.ref}).encode())
        if status != 204:
            raise RuntimeError(f"dispatch: HTTP {status}: {_redact(body.decode(errors='replace'))[:200]}")
        return "dispatched: no collect run had started this hour"


def next_wake(now: datetime, minute: int) -> datetime:
    t = now.replace(minute=minute, second=0, microsecond=0)
    return t if t > now else t + timedelta(hours=1)


def main() -> None:
    ap = argparse.ArgumentParser(description="Start the collect workflow when GitHub's schedule skipped an hour")
    ap.add_argument("--config", default=None)
    ap.add_argument("--minute", type=int, default=1,
                    help="minute of the hour to start checking; Coinbase has the closed hour's "
                         "candle within seconds (default 1)")
    ap.add_argument("--ref", default="main")
    ap.add_argument("--once", action="store_true", help="check once now and exit")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    cfg = load_config(args.config)
    token = os.environ.get(cfg.forward_log.token_env)
    if not token:
        raise SystemExit(f"set ${cfg.forward_log.token_env} (Actions: read and write) to start collect runs")
    run(Kicker(cfg.forward_log.repo, token, args.ref), args.minute, once=args.once)


def run(k: Kicker, minute: int = 1, once: bool = False, poll_min: float = 5,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], None] = time.sleep) -> None:
    """From `minute` past every hour on, make sure a collect run started that hour
    (or check once, now, with `once`).

    Wakes every `poll_min` minutes and reads the wall clock each time, so a box
    that was asleep, or a container restarted after `minute`, still covers the
    hour instead of waiting for the next one. An hour is done after one
    successful check; errors are logged and retried at the next wake."""
    if once:
        try:
            log.info("%s", k.tick(clock()))
        except Exception as e:  # noqa: BLE001
            log.warning("kick failed: %s", e)
            raise SystemExit(1) from e
        return
    done: datetime | None = None
    while True:
        now = clock()
        hour = now.replace(minute=0, second=0, microsecond=0)
        if hour != done and now.minute >= minute:
            try:
                log.info("%s", k.tick(now))
                done = hour
            except Exception as e:  # noqa: BLE001 - keep the loop alive; the next wake tries again
                log.warning("kick failed: %s", e)
        now = clock()
        wake = min(next_wake(now, minute), now + timedelta(minutes=poll_min))
        sleep(max(1.0, (wake - now).total_seconds()))


if __name__ == "__main__":
    main()
