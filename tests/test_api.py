import os
import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

from jevtrade.api.app import create_app
from jevtrade.report import ReportConfig, write_report

from test_paper import FIRST, MultiSource, app_cfg, run_hourly, series


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("api")
    db = tmp / "p.sqlite"
    src = MultiSource(series())
    t = run_hourly(db, src, FIRST, FIRST + 80)
    cfg = app_cfg()
    cfg.storage.sqlite_path = str(db)
    cfg.report = ReportConfig(output_dir=str(tmp / "reports"))
    cfg.backtest.output_dir = str(tmp / "backtests")
    write_report(t.conn, cfg, "paper", date(2024, 1, 18), cfg.report)
    t.conn.close()
    return cfg, TestClient(create_app(cfg))


def test_every_route_is_get_only(env):
    cfg, _ = env
    app = create_app(cfg)
    for r in app.routes:
        methods = getattr(r, "methods", None)
        if methods and r.path.startswith("/api"):
            assert methods <= {"GET", "HEAD"}, (r.path, methods)
    _, c = env
    assert c.post("/api/paper/status").status_code == 405
    assert c.delete("/api/decisions/1").status_code == 405


def test_health_and_config(env):
    _, c = env
    h = c.get("/api/health").json()
    assert h["status"] == "ok" and h["paper_heartbeat_at"]
    cfg = c.get("/api/config").json()
    assert cfg["symbols"] == ["BTC/USD", "ETH/USD"]
    assert cfg["sizing"]["risk_per_trade"] == 0.01
    assert "api_key" not in str(cfg).lower()


def test_paper_status_matches_store(env):
    cfg, c = env
    s = c.get("/api/paper/status").json()
    conn = sqlite3.connect(cfg.storage.sqlite_path)
    last_eq = conn.execute("SELECT equity FROM paper_bars ORDER BY bar_ts DESC LIMIT 1").fetchone()[0]
    n = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    assert s["model"] == "mock" and s["equity"] == pytest.approx(last_eq)
    assert s["trades"] == n > 0
    for p in s["positions"]:
        assert p["unrealized_pnl"] == pytest.approx(p["qty"] * (p["mark"] - p["entry_price"]))
    assert c.get("/api/paper/status", params={"run_id": "nope"}).status_code == 404


def test_equity_trades_decisions(env):
    _, c = env
    eq = c.get("/api/paper/equity").json()
    assert len(eq) == 80 and eq[0]["bar_ts"] < eq[-1]["bar_ts"]
    assert len(c.get("/api/paper/equity", params={"limit": 10}).json()) == 10
    tr = c.get("/api/paper/trades", params={"symbol": "BTC/USD"}).json()
    assert tr and all(t["symbol"] == "BTC/USD" for t in tr)
    ds = c.get("/api/decisions", params={"limit": 6}).json()
    assert len(ds) == 6 and "input_text" not in ds[0]
    assert all("direction" in d["probs"] for d in ds if not d["abstain"])
    older = c.get("/api/decisions", params={"before": ds[-1]["bar_ts"], "limit": 2}).json()
    assert all(d["bar_ts"] < ds[-1]["bar_ts"] for d in older)
    one = c.get(f"/api/decisions/{ds[0]['id']}").json()
    assert one["input_text"] and one["policy_action"]
    assert c.get("/api/decisions/999999").status_code == 404


def test_candles_reports_backtests(env):
    _, c = env
    cs = c.get("/api/candles", params={"symbol": "BTC/USD", "limit": 50}).json()
    assert len(cs) == 50 and cs[0]["ts"] < cs[-1]["ts"]
    assert c.get("/api/reports").json()["days"] == ["2024-01-18"]
    r = c.get("/api/reports/2024-01-18").json()
    assert r["markdown"].startswith("# Paper report") and r["data"]["day"] == "2024-01-18"
    assert c.get("/api/reports/2024-01-19").status_code == 404
    assert c.get("/api/reports/..%2Fetc").status_code in (400, 404)
    assert c.get("/api/reports", params={"run_id": "../x"}).status_code == 400
    assert c.get("/api/backtests").json() == []


def test_database_is_opened_read_only(env):
    cfg, _ = env
    before = os.path.getmtime(cfg.storage.sqlite_path)
    app = create_app(cfg)
    _, c = env
    for path in ("/api/paper/status", "/api/paper/equity", "/api/decisions", "/api/health"):
        assert c.get(path).status_code == 200
    assert os.path.getmtime(cfg.storage.sqlite_path) == before
    assert app  # constructed without touching the file


def test_missing_database_is_503(tmp_path):
    cfg = app_cfg()
    cfg.storage.sqlite_path = str(tmp_path / "none.sqlite")
    c = TestClient(create_app(cfg))
    assert c.get("/api/health").json()["status"] == "no_database"
    assert c.get("/api/paper/status").status_code == 503
    assert not (tmp_path / "none.sqlite").exists()


def test_forward_routes_with_github_mocked(env, monkeypatch):
    from jevtrade.api.forward import ForwardLog
    from jevtrade.api.settings import ForwardLogConfig
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
    from jevtrade.api.forward import ForwardLog
    from jevtrade.api.settings import ForwardLogConfig
    from test_forward import FakeGitHub

    cfg, _ = env
    fwd = ForwardLog(ForwardLogConfig(source="github", repo="me/repo"), http_get=FakeGitHub())
    c = TestClient(create_app(cfg, forward_log=fwd))
    r = c.get("/api/forward/paper").json()
    assert r["initial_equity"] == cfg.paper.initial_equity and r["last_bar_ts"] is not None
    assert {a["symbol"] for a in r["actions"]} <= {"BTC/USD", "ETH/USD"}
    assert len(c.get("/api/forward/paper", params={"actions": 1}).json()["actions"]) == 1
    assert c.post("/api/forward/paper").status_code == 405
