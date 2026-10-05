"""CLI: serve the read-only API.

    python -m jevtrade.api [--host 127.0.0.1] [--port 8000] [--config PATH]

Docs at http://<host>:<port>/docs. Every route is a GET and the database is
opened read-only, so it is safe to run next to the paper loop.
"""

from __future__ import annotations

import argparse

import uvicorn

from ..config import load_config
from .app import create_app


def main() -> None:
    ap = argparse.ArgumentParser(description="Serve the read-only jevtrade API")
    ap.add_argument("--config", default=None)
    ap.add_argument("--host", default=None, help="default: api.host")
    ap.add_argument("--port", type=int, default=None, help="default: api.port")
    args = ap.parse_args()
    cfg = load_config(args.config)
    uvicorn.run(create_app(cfg), host=args.host or cfg.api.host, port=args.port or cfg.api.port,
                log_level="info")


if __name__ == "__main__":
    main()
