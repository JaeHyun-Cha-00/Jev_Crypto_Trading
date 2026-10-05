"""Hourly collect backstop: decides from the GitHub API; no network here."""

import json
from datetime import datetime, timezone

import pytest

from jevtrade.collect.kick import Kicker, next_wake

NOW = datetime(2026, 10, 5, 9, 20, 5, tzinfo=timezone.utc)


class FakeGitHub:
    def __init__(self, runs, dispatch_status=204):
        self.runs, self.dispatch_status, self.calls = runs, dispatch_status, []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if method == "GET":
            return 200, json.dumps({"workflow_runs": self.runs}).encode()
        return self.dispatch_status, b""


def test_dispatches_when_no_run_started_this_hour():
    gh = FakeGitHub([])
    assert Kicker("o/r", "tok", http=gh).tick(NOW).startswith("dispatched")
    (m1, list_url, h, _), (m2, post_url, _, body) = gh.calls
    assert m1 == "GET" and "created=%3E%3D2026-10-05T09:00:00Z" in list_url
    assert m2 == "POST" and post_url.endswith("/repos/o/r/actions/workflows/collect.yml/dispatches")
    assert json.loads(body) == {"ref": "main"} and h["Authorization"] == "Bearer tok"


def test_skips_when_the_schedule_already_ran():
    gh = FakeGitHub([{"id": 7, "event": "schedule", "status": "completed"}])
    assert Kicker("o/r", "tok", http=gh).tick(NOW).startswith("skip")
    assert [c[0] for c in gh.calls] == ["GET"]


def test_a_refused_dispatch_raises():
    with pytest.raises(RuntimeError, match="HTTP 403"):
        Kicker("o/r", "tok", http=FakeGitHub([], dispatch_status=403)).tick(NOW)


def test_next_wake_is_the_coming_minute_mark():
    assert next_wake(NOW, 20) == datetime(2026, 10, 5, 10, 20, tzinfo=timezone.utc)
    assert next_wake(NOW.replace(minute=5), 20) == datetime(2026, 10, 5, 9, 20, tzinfo=timezone.utc)
