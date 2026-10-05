"""Hourly backstop for the collect workflow's GitHub schedule.

GitHub may delay or skip scheduled runs. This loop, run next to the API on an
always-on box, wakes at minute `--minute` of every hour, asks GitHub whether a
collect run already started this hour, and starts one with workflow_dispatch
only if none did. It calls nothing but the GitHub API; the run itself (and
every Jev call) happens in GitHub Actions, as with the schedule.

    python -m jevtrade.collect.kick [--minute 20] [--once]

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
    ap.add_argument("--minute", type=int, default=20,
                    help="minute of the hour to check, after the :11 schedule (default 20)")
    ap.add_argument("--ref", default="main")
    ap.add_argument("--once", action="store_true", help="check once now and exit")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    cfg = load_config(args.config)
    token = os.environ.get(cfg.forward_log.token_env)
    if not token:
        raise SystemExit(f"set ${cfg.forward_log.token_env} (Actions: read and write) to start collect runs")
    run(Kicker(cfg.forward_log.repo, token, args.ref), args.minute, once=args.once)


def run(k: Kicker, minute: int = 20, once: bool = False) -> None:
    """Check at `minute` past every hour, forever (or once, now). Errors are logged and
    retried next hour; with `once` they exit non-zero."""
    while True:
        if not once:
            wake = next_wake(datetime.now(timezone.utc), minute)
            time.sleep(max(0.0, (wake - datetime.now(timezone.utc)).total_seconds()))
        try:
            log.info("%s", k.tick(datetime.now(timezone.utc)))
        except Exception as e:  # noqa: BLE001 - keep the loop alive; next hour tries again
            log.warning("kick failed: %s", e)
            if once:
                raise SystemExit(1) from e
        if once:
            return

if __name__ == "__main__":
    main()
