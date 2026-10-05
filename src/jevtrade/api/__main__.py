"""CLI: serve the read-only API.

    python -m jevtrade.api [--host 127.0.0.1] [--port 8000] [--config PATH]

Docs at http://<host>:<port>/docs. Every route is a GET and the database is
opened read-only, so it is safe to run next to the paper loop. Environment
variables turn on the hosted extras (dashboard, password, collect backstop):
see jevtrade.api.hosting. $PORT, when set, is the default port.
"""

from __future__ import annotations

import argparse
import logging
import os

import uvicorn

from ..config import load_config
from .app import create_app
from .hosting import apply


def main() -> None:
    ap = argparse.ArgumentParser(description="Serve the read-only jevtrade API")
    ap.add_argument("--config", default=None)
    ap.add_argument("--host", default=None, help="default: api.host")
    ap.add_argument("--port", type=int, default=None, help="default: $PORT, else api.port")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = load_config(args.config)
    port = args.port or int(os.environ.get("PORT") or cfg.api.port)
    uvicorn.run(apply(create_app(cfg), cfg), host=args.host or cfg.api.host, port=port,
                log_level="info")


if __name__ == "__main__":
    main()
