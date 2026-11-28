"""The dashboard digest: built from the log, it answers every forward route the same way."""

import gzip
import json
import shutil

from fastapi.testclient import TestClient

from jevtrade.api.app import create_app
from jevtrade.config import AppConfig
from jevtrade.forward import digest as dg
from jevtrade.forward.log import ForwardLog
from test_forward import FIXTURE, FakeGitHub

ROUTES = ["/api/forward/summary", "/api/forward/paper", "/api/forward/rows",
          "/api/forward/rows?symbol=ETH/USD&limit=2"]
_PER_FETCH = {"fetched_at", "built_at", "error", "source", "location"}


def app_cfg(**forward) -> AppConfig:
    return AppConfig.model_validate({"data": {"symbols": ["BTC/USD", "ETH/USD"]}, "market": {"enabled": False},
                                     "forward_log": {"source": "local", **forward}})


def build_into(tmp_path):
    log_dir = tmp_path / "data-log"
    shutil.copytree(FIXTURE, log_dir)
    dg.main(["--log", str(log_dir)])
    return log_dir


def answers(client) -> dict:
    out = {}
    for route in ROUTES:
        body = client.get(route).json()
        out[route] = {k: v for k, v in body.items() if k not in _PER_FETCH} if isinstance(body, dict) else body
    return out


def test_digest_answers_like_the_live_log(tmp_path, monkeypatch):
    monkeypatch.setattr("jevtrade.config.load_config", lambda path=None: app_cfg())   # main()'s config
    log_dir = build_into(tmp_path)
    assert (log_dir / dg.DIGEST_PATH).is_file()
    live = TestClient(create_app(app_cfg(local_dir=str(log_dir))))
    served = TestClient(create_app(app_cfg(local_dir=str(log_dir), digest=True)))
    assert answers(served) == answers(live)
    s = served.get("/api/forward/summary").json()
    assert s["error"] is None and s["built_at"] is not None and s["source"] == "local"


def test_digest_keeps_each_coins_newest_rows(monkeypatch):
    monkeypatch.setattr(dg, "ROWS_PER_SYMBOL", 1)
    cfg = app_cfg()
    fwd = ForwardLog(cfg.forward_log.model_copy(update={"source": "github", "repo": "me/repo"}),
                     http_get=FakeGitHub())
    d = dg.build(dg.LiveView(fwd, cfg))
    assert sorted(r["symbol"] for r in d["rows"]) == ["BTC/USD", "ETH/USD"]
    newest = {r["symbol"]: r["candle_ts"] for r in reversed(fwd.snapshot().rows)}
    assert all(r["candle_ts"] == newest[r["symbol"]] for r in d["rows"])


def test_digest_from_github_and_before_it_exists(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    cfg = app_cfg(source="github", repo="me/repo", digest=True)
    root = tmp_path / "branch"
    root.mkdir()
    gh = FakeGitHub(root=root)
    view = dg.DigestView(cfg, http_get=gh)
    # No digest yet: an empty dashboard that says why, not a 500.
    s = view.summary()
    assert "the collect workflow builds it" in s["error"] and s["overall"]["decisions"] == 0
    assert view.rows(None, 10) == [] and view.paper()["curve"] == []
    assert gh.calls[-1][0] == f"https://raw.githubusercontent.com/me/repo/data-log/{dg.DIGEST_PATH}"

    (root / "dashboard").mkdir()
    built = {"version": dg.DIGEST_VERSION, "built_at": 1.0, "summary": {"overall": {"decisions": 3}},
             "paper": {"curve": [1]}, "rows": [{"symbol": "BTC/USD"}, {"symbol": "ETH/USD"}]}
    (root / dg.DIGEST_PATH).write_bytes(gzip.compress(json.dumps(built).encode()))
    view = dg.DigestView(cfg, http_get=gh)
    assert view.summary()["overall"]["decisions"] == 3 and view.summary()["error"] is None
    assert view.rows("ETH/USD", 10) == [{"symbol": "ETH/USD"}] and view.paper() == {"curve": [1]}

    # A failed refresh keeps the last good digest and reports the failure.
    clock = [0.0]
    view = dg.DigestView(cfg, http_get=gh, clock=lambda: clock[0])
    assert view.summary()["error"] is None
    gh.fail, clock[0] = True, 10_000.0
    s = view.summary()
    assert "network down" in s["error"] and s["overall"]["decisions"] == 3


def test_digest_with_a_token_reads_the_contents_api(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    seen = []

    def get(url, headers, timeout_s):
        seen.append((url, headers))
        return gzip.compress(json.dumps({"version": dg.DIGEST_VERSION, "built_at": 0, "summary": {},
                                         "paper": {}, "rows": []}).encode())

    dg.DigestView(app_cfg(source="github", repo="me/repo", digest=True), http_get=get).summary()
    url, headers = seen[0]
    assert url == f"https://api.github.com/repos/me/repo/contents/{dg.DIGEST_PATH}?ref=data-log"
    assert headers["Authorization"] == "Bearer tok" and headers["Accept"] == "application/vnd.github.raw"


def test_write_is_deterministic(tmp_path):
    d = {"version": 1, "built_at": 5, "summary": {}, "paper": {}, "rows": []}
    a, b = tmp_path / "a.json.gz", tmp_path / "b.json.gz"
    dg.write(d, a), dg.write(d, b)
    assert a.read_bytes() == b.read_bytes() and json.loads(gzip.decompress(a.read_bytes())) == d
