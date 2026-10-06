import pytest
from fastapi.testclient import TestClient

from jevtrade.api.app import create_app
from jevtrade.config import AppConfig


def app_cfg() -> AppConfig:
    return AppConfig.model_validate({"data": {"symbols": ["BTC/USD", "ETH/USD"]}})


@pytest.fixture(scope="module")
def env():
    cfg = app_cfg()
    return cfg, TestClient(create_app(cfg))


def test_every_route_is_get_only(env):
    cfg, c = env
    for r in create_app(cfg).routes:
        methods = getattr(r, "methods", None)
        if methods and r.path.startswith("/api"):
            assert methods <= {"GET", "HEAD"}, (r.path, methods)
    assert c.post("/api/config").status_code == 405


def test_health_and_config(env):
    _, c = env
    assert c.get("/api/health").json()["status"] == "ok"
    cfg = c.get("/api/config").json()
    assert cfg["symbols"] == ["BTC/USD", "ETH/USD"]
    assert cfg["sizing"]["risk_per_trade"] == 0.01
    assert "api_key" not in str(cfg).lower()


def test_config_describes_the_jev_portfolio():
    cfg = app_cfg()
    cfg.policy = cfg.policy.model_copy(update={"max_holding_bars": 4, "max_holding_bars_by_model": {"jev": None}})
    cfg.jev_paper.initial_equity = 2_500
    sizing = TestClient(create_app(cfg)).get("/api/config").json()["sizing"]
    assert sizing["max_holding_bars"] is None and sizing["starting_balance"] == 2_500


def test_old_paper_routes_are_gone(env):
    _, c = env
    for path in ("/api/paper/status", "/api/decisions", "/api/candles", "/api/reports", "/api/backtests"):
        assert c.get(path).status_code == 404


def test_forward_routes_with_github_mocked(env, monkeypatch):
    from jevtrade.forward.log import ForwardLog
    from jevtrade.forward.settings import ForwardLogConfig
    from test_forward import FakeGitHub

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    cfg, _ = env
    gh = FakeGitHub()
    fwd = ForwardLog(ForwardLogConfig(source="github", repo="me/repo"), http_get=gh)
    c = TestClient(create_app(cfg, forward_log=fwd))
    s = c.get("/api/forward/summary").json()
    assert s["error"] is None and s["source"] == "github"
    assert s["overall"]["scored"] == 4 and s["overall"]["hit_rate"] == 0.5
    assert s["per_symbol"]["BTC/USD"]["pending"] == 1
    rows = c.get("/api/forward/rows").json()
    assert len(rows) == 7 and rows[0]["outcome"] is None
    assert not any("state" in r or "raw_response" in r for r in rows)
    eth = c.get("/api/forward/rows", params={"symbol": "ETH/USD", "limit": 2}).json()
    assert len(eth) == 2 and {r["symbol"] for r in eth} == {"ETH/USD"}
    assert c.get("/api/forward/rows", params={"limit": 0}).status_code == 422
    assert c.post("/api/forward/summary").status_code == 405


def test_forward_unreachable_is_empty_not_500(env):
    cfg, c = env   # default config: source github, network refused by conftest
    s = c.get("/api/forward/summary")
    assert s.status_code == 200
    body = s.json()
    assert body["overall"]["decisions"] == 0 and body["overall"]["hit_rate"] is None
    assert "network disabled" in body["error"]
    assert c.get("/api/forward/rows").json() == []


def test_forward_off(env):
    cfg, _ = env
    off = cfg.model_copy(deep=True)
    off.forward_log.source = "off"
    s = TestClient(create_app(off)).get("/api/forward/summary").json()
    assert s["source"] == "off" and s["error"] is None and s["overall"]["scored"] == 0


def test_forward_paper_replays_logged_answers(env):
    from jevtrade.forward.log import ForwardLog
    from jevtrade.forward.settings import ForwardLogConfig
    from test_forward import FakeGitHub

    cfg, _ = env
    fwd = ForwardLog(ForwardLogConfig(source="github", repo="me/repo"), http_get=FakeGitHub())
    c = TestClient(create_app(cfg, forward_log=fwd))
    r = c.get("/api/forward/paper").json()
    assert r["initial_equity"] == cfg.jev_paper.initial_equity and r["last_bar_ts"] is not None
    assert {a["symbol"] for a in r["actions"]} <= {"BTC/USD", "ETH/USD"}
    assert len(c.get("/api/forward/paper", params={"actions": 1}).json()["actions"]) == 1
    assert len(c.get("/api/forward/paper", params={"hours": 1}).json()["hours"]) == 1
    assert c.post("/api/forward/paper").status_code == 405
    # Coinbase is unreachable in tests, so the detail falls back to the hourly equity at each close.
    d = c.get("/api/forward/paper/detail").json()
    assert d["step_ms"] in (300_000, 900_000, 3_600_000)
    assert all(set(p) == {"ts", "equity"} for p in d["points"])
    assert c.get("/api/forward/paper/detail", params={"hours": 0}).status_code == 422


def test_forward_starts_at_start(env):
    from datetime import datetime, timezone

    from jevtrade.forward.log import ForwardLog
    from jevtrade.forward.settings import ForwardLogConfig
    from test_forward import FakeGitHub

    cfg, _ = env
    first = 1791086400000   # first fixture candle, 2026-10-04T04:00Z
    for start_ms, expect_rows in ((first + 3_600_000, True), (first + 30 * 86_400_000, False)):
        c2 = cfg.model_copy(deep=True)
        c2.forward_log.start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
        fwd = ForwardLog(ForwardLogConfig(source="github", repo="me/repo"), http_get=FakeGitHub())
        r = TestClient(create_app(c2, forward_log=fwd)).get("/api/forward/paper").json()
        assert r["tracking_since"] == start_ms
        assert all(a["bar_ts"] >= start_ms for a in r["actions"])
        assert bool(r["actions"]) is expect_rows
        if not expect_rows:   # nothing logged since the start yet: a fresh account
            assert r["equity"] == cfg.jev_paper.initial_equity and r["trades"] == [] and r["curve"] == []
        # The loader drops lines before the start too, so the stats match.
        fwd2 = ForwardLog(ForwardLogConfig(source="github", repo="me/repo", start=c2.forward_log.start),
                          http_get=FakeGitHub())
        s = TestClient(create_app(c2, forward_log=fwd2)).get("/api/forward/summary").json()
        assert s["overall"]["decisions"] == (5 if expect_rows else 0)   # two of the seven are in the first hour


def test_start_reads_naive_and_zoned_times():
    from jevtrade.forward.settings import ForwardLogConfig

    assert ForwardLogConfig().start_ms() is None
    z = ForwardLogConfig(start="2026-10-05T07:00:00Z").start_ms()
    assert z == ForwardLogConfig(start="2026-10-05T07:00:00").start_ms() == 1791183600000
