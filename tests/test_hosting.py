"""Hosted extras: password, dashboard files and background threads, all off unless asked for."""

import base64

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jevtrade.api import hosting


def _app(tmp_path, env):
    (tmp_path / "index.html").write_text("<html>dash</html>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app-1.js").write_text("js")
    (tmp_path.parent / "secret.txt").write_text("nope")
    app = FastAPI()

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/config")
    def config():
        return {"symbols": []}

    return TestClient(hosting.apply(app, None, {"JEVTRADE_WEB_DIR": str(tmp_path), **env}))


def _basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_password_guards_everything_but_health(tmp_path):
    c = _app(tmp_path, {"DASHBOARD_PASSWORD": "s3cret"})
    assert c.get("/api/health").status_code == 200
    for path in ("/", "/api/config", "/assets/app-1.js"):
        r = c.get(path)
        assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
    assert c.get("/api/config", headers=_basic("x", "wrong")).status_code == 401
    assert c.get("/api/config", headers={"Authorization": "Basic !!!"}).status_code == 401
    assert c.get("/api/config", headers=_basic("anyone", "s3cret")).json() == {"symbols": []}


def test_dashboard_files_and_spa_fallback(tmp_path):
    c = _app(tmp_path, {})
    assert c.get("/").text == "<html>dash</html>"
    assert c.get("/coin/BTC-USD").text == "<html>dash</html>"
    r = c.get("/assets/app-1.js")
    assert r.text == "js" and "immutable" in r.headers["cache-control"]
    assert c.get("/api/nope").status_code == 404
    assert "nope" not in c.get("/../secret.txt").text


def test_missing_build_is_a_clear_error(tmp_path):
    with pytest.raises(SystemExit, match="index.html"):
        hosting.add_dashboard(FastAPI(), tmp_path)


def test_no_threads_unless_asked(monkeypatch):
    class Cfg:
        class forward_log:
            token_env, repo = "GITHUB_TOKEN", "o/r"
    started = []
    monkeypatch.setattr(hosting.threading.Thread, "start", lambda self: started.append(self.name))
    assert hosting.start_background(Cfg, {}) == []
    assert hosting.start_background(Cfg, {"JEVTRADE_KICK": "1"}) == []          # no token: skip
    ts = hosting.start_background(Cfg, {"JEVTRADE_KICK": "1", "GITHUB_TOKEN": "t",
                                        "JEVTRADE_KEEPALIVE_MIN": "10",
                                        "RENDER_EXTERNAL_URL": "https://x.onrender.com/"})
    assert [t.name for t in ts] == started == ["collect-kick", "keepalive"]
    assert ts[1]._args == ("https://x.onrender.com/api/health", 10.0)
