"""Running the API and dashboard as one public web service (render.yaml).

Locally, docker compose puts nginx in front of the API and keeps both on
localhost. A hosted copy is one container on the internet instead, so this
module adds, only when the matching environment variable is set:

- JEVTRADE_WEB_DIR: serve the built dashboard (web/dist) from the API itself,
  with unknown paths falling back to index.html like nginx's try_files.
- DASHBOARD_PASSWORD: HTTP basic auth on every route except /api/health (the
  host's health check and the keep-alive ping). Any user name works. Without
  it a hosted dashboard would show the private forward log to anyone.
- JEVTRADE_KICK=1: run the collect backstop (jevtrade.collect.kick) in a
  background thread, at minute JEVTRADE_KICK_MINUTE (default 3), so the
  hourly run starts even when the local docker box is off.
- JEVTRADE_KEEPALIVE_MIN: every N minutes GET <public URL>/api/health, so a
  free instance that sleeps after idle time stays up for the kick thread.
  The URL is JEVTRADE_PUBLIC_URL, else Render's RENDER_EXTERNAL_URL.
"""

from __future__ import annotations

import base64
import hmac
import logging
import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import Mapping

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response

log = logging.getLogger(__name__)

OPEN_PATHS = frozenset({"/api/health"})


def _authorized(header: str | None, password: str) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode()
    except (ValueError, UnicodeDecodeError):
        return False
    _, _, given = decoded.partition(":")
    return hmac.compare_digest(given.encode(), password.encode())


def add_password(app: FastAPI, password: str) -> None:
    @app.middleware("http")
    async def basic_auth(request: Request, call_next):
        if request.url.path in OPEN_PATHS or _authorized(request.headers.get("authorization"), password):
            return await call_next(request)
        return Response("Password required", status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="Jev dashboard", charset="UTF-8"'})


def add_dashboard(app: FastAPI, web_dir: str | Path) -> None:
    """Serve the built dashboard after every /api route (register this last)."""
    root = Path(web_dir).resolve()
    index = root / "index.html"
    if not index.is_file():
        raise SystemExit(f"JEVTRADE_WEB_DIR={web_dir}: no index.html (run `npm run build` in web/)")

    @app.get("/{path:path}", include_in_schema=False)
    def dashboard(path: str):
        if path.startswith("api/"):
            return Response('{"detail":"Not Found"}', status_code=404, media_type="application/json")
        f = (root / path).resolve()
        if path and f.is_file() and f.is_relative_to(root):
            # Vite fingerprints everything under assets/, so it can be cached for good.
            cache = "public, max-age=31536000, immutable" if path.startswith("assets/") else "no-cache"
            return FileResponse(f, headers={"Cache-Control": cache})
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


def _keepalive(url: str, minutes: float) -> None:
    while True:
        time.sleep(minutes * 60)
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                r.read()
        except Exception as e:  # noqa: BLE001 - try again next round
            log.warning("keep-alive ping failed: %s", e)


def start_background(cfg, env: Mapping[str, str]) -> list[threading.Thread]:
    """Start the kick and keep-alive threads the environment asks for."""
    threads = []
    if env.get("JEVTRADE_KICK") == "1":
        from ..collect.kick import Kicker, run
        token = env.get(cfg.forward_log.token_env)
        if not token:
            log.warning("JEVTRADE_KICK=1 but $%s is not set; not starting collect runs",
                        cfg.forward_log.token_env)
        else:
            minute = int(env.get("JEVTRADE_KICK_MINUTE", "3"))
            k = Kicker(cfg.forward_log.repo, token, env.get("JEVTRADE_KICK_REF", "main"))
            threads.append(threading.Thread(target=run, args=(k, minute), name="collect-kick", daemon=True))
    every = float(env.get("JEVTRADE_KEEPALIVE_MIN", "0") or 0)
    base = env.get("JEVTRADE_PUBLIC_URL") or env.get("RENDER_EXTERNAL_URL")
    if every > 0 and base:
        url = base.rstrip("/") + "/api/health"
        threads.append(threading.Thread(target=_keepalive, args=(url, every), name="keepalive", daemon=True))
    for t in threads:
        t.start()
        log.info("started %s", t.name)
    return threads


def apply(app: FastAPI, cfg, env: Mapping[str, str] = os.environ) -> FastAPI:
    """Add whatever hosting pieces the environment turns on. Call after create_app."""
    if env.get("DASHBOARD_PASSWORD"):
        add_password(app, env["DASHBOARD_PASSWORD"])
    if env.get("JEVTRADE_WEB_DIR"):
        add_dashboard(app, env["JEVTRADE_WEB_DIR"])
    start_background(cfg, env)
    return app
