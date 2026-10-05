"""Hourly collect backstop: decides from the GitHub API; no network here."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from jevtrade.collect.kick import Kicker, next_wake, run

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


class Stop(Exception):
    pass


class Clock:
    """Wall clock that `sleep` advances; stops the loop after `until`."""

    def __init__(self, start, until, jumps=()):
        self.t, self.until, self.jumps, self.sleeps = start, until, dict(jumps), []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += timedelta(seconds=seconds)
        self.t = self.jumps.pop(len(self.sleeps), self.t)  # a suspended box wakes up late
        if self.t >= self.until:
            raise Stop


class FakeKicker:
    def __init__(self, fail=()):
        self.ticks, self.fail = [], set(fail)

    def tick(self, now):
        self.ticks.append(now)
        if len(self.ticks) in self.fail:
            raise RuntimeError("HTTP 502")
        return "dispatched"


def _run(clock, k, minute=20):
    with pytest.raises(Stop):
        run(k, minute, clock=clock.now, sleep=clock.sleep)


def T(h, m):
    return datetime(2026, 10, 5, h, m, tzinfo=timezone.utc)


def test_loop_checks_once_per_hour_at_the_minute():
    clock, k = Clock(T(9, 5), T(11, 59)), FakeKicker()
    _run(clock, k)
    assert [t.strftime("%H:%M") for t in k.ticks] == ["09:20", "10:20", "11:20"]
    assert max(clock.sleeps) <= 5 * 60


def test_a_restart_after_the_minute_still_covers_the_hour():
    clock, k = Clock(T(16, 40), T(17, 1)), FakeKicker()
    _run(clock, k)
    assert [t.strftime("%H:%M") for t in k.ticks] == ["16:40"]


def test_a_box_that_slept_through_the_minute_checks_when_it_wakes():
    # asleep from 16:15 to 16:50: the old loop would wait for 17:20
    clock, k = Clock(T(16, 10), T(16, 59), jumps={1: T(16, 50)}), FakeKicker()
    _run(clock, k)
    assert [t.strftime("%H:%M") for t in k.ticks] == ["16:50"]


def test_a_failed_check_is_retried_at_the_next_wake_not_next_hour():
    clock, k = Clock(T(16, 19), T(16, 59)), FakeKicker(fail={1})
    _run(clock, k)
    assert [t.strftime("%H:%M") for t in k.ticks] == ["16:20", "16:25"]
