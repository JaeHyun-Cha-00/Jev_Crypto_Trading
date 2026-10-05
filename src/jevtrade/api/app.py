"""Read-only HTTP API over the SQLite store and report files.

Every route is a GET. The database is opened with SQLite's `mode=ro`, so
the API cannot write even by mistake, and it never touches an exchange or
the model. The forward-log routes only read the collector's JSONL, locally
or from GitHub (jevtrade.api.forward), and /api/forward/paper replays Jev's
logged answers through the simulator (jevtrade.api.jev_paper). It serves the dashboard (stage 10) and anything else that wants
to watch the paper account.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Iterator

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from ..sim import SimState
from ..data.timeframes import timeframe_ms
from .forward import ForwardLog
from .jev_paper import replay
from .settings import ApiConfig  # noqa: F401  (re-exported)


_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RUN = re.compile(r"^[A-Za-z0-9_.:-]+$")


def _rows(cur: sqlite3.Cursor) -> list[dict]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def create_app(app_cfg, forward_log: ForwardLog | None = None) -> FastAPI:
    """`app_cfg` is a jevtrade.config.AppConfig; `forward_log` overrides the
    loader built from `app_cfg.forward_log` (tests inject one)."""
    fwd = forward_log or ForwardLog(app_cfg.forward_log)
    db_path = Path(app_cfg.storage.sqlite_path)
    app = FastAPI(title="jevtrade (read-only)", version="1",
                  description="Paper-trading state. GET only; the database is opened read-only.")
    app.add_middleware(CORSMiddleware, allow_origins=app_cfg.api.cors_origins,
                       allow_methods=["GET"], allow_headers=["*"])

    def connect_ro() -> sqlite3.Connection:
        return sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True, check_same_thread=False)

    def db() -> Iterator[sqlite3.Connection]:
        if not db_path.exists():
            raise HTTPException(503, f"no database at {db_path} yet; start the paper loop or sync data")
        conn = connect_ro()
        try:
            yield conn
        finally:
            conn.close()

    def paper_state(conn: sqlite3.Connection, run_id: str):
        if not _has_table(conn, "paper_state"):
            return None
        return conn.execute(
            "SELECT state, model, model_version, started_at, updated_at, heartbeat_at FROM paper_state"
            " WHERE run_id=?", (run_id,)).fetchone()

    run_q = Query(None, description="paper run id (default: paper.run_id)")

    @app.get("/api/health")
    def health():
        ok = db_path.exists()
        out = {"status": "ok" if ok else "no_database", "database": str(db_path), "time": time.time()}
        if ok:
            conn = connect_ro()
            try:
                row = paper_state(conn, app_cfg.paper.run_id)
            finally:
                conn.close()
            if row:
                out["paper_heartbeat_at"] = row[5]
                out["paper_last_bar_at"] = row[4]
        return out

    @app.get("/api/config")
    def config():
        """Non-secret settings: symbols, model, sizing and risk limits."""
        p = app_cfg.policy.for_model(app_cfg.decision.model)
        return {
            "exchange": app_cfg.data.exchange,
            "symbols": app_cfg.data.symbols,
            "timeframe": app_cfg.data.timeframe,
            "model": app_cfg.decision.model,
            "jev_model": app_cfg.decision.jev.model,
            "horizon_bars": app_cfg.decision.horizon_bars,
            "flat_band_pct": app_cfg.decision.flat_band_pct,
            "paper_run_id": app_cfg.paper.run_id,
            "sizing": p.describe_sizing(app_cfg.paper.initial_equity),
            "policy": p.model_dump(),
        }

    @app.get("/api/paper/status")
    def status(run_id: str | None = run_q, conn: sqlite3.Connection = Depends(db)):
        run_id = run_id or app_cfg.paper.run_id
        row = paper_state(conn, run_id)
        if row is None:
            raise HTTPException(404, f"paper run {run_id!r} has not started")
        st = SimState.from_json(row[0])
        eq = st.equity()
        trades = conn.execute("SELECT COUNT(*), COALESCE(SUM(pnl), 0), COALESCE(SUM(pnl > 0), 0)"
                              " FROM paper_trades WHERE run_id=?", (run_id,)).fetchone()
        peak = conn.execute("SELECT MAX(equity) FROM paper_bars WHERE run_id=?", (run_id,)).fetchone()[0]
        initial = app_cfg.paper.initial_equity
        return {
            "run_id": run_id, "model": row[1], "model_version": row[2],
            "started_at": row[3], "updated_at": row[4], "heartbeat_at": row[5],
            "last_bar_ts": st.last_bar_ts,
            "equity": eq, "cash": st.cash, "initial_equity": initial,
            "total_return": eq / initial - 1,
            "drawdown": eq / peak - 1 if peak else 0.0,
            "trades": trades[0], "realized_pnl": trades[1],
            "win_rate": trades[2] / trades[0] if trades[0] else None,
            "risk": {"day_start_equity": st.risk.day_start_equity,
                     "day_return": eq / st.risk.day_start_equity - 1 if st.risk.day_start_equity else 0.0,
                     "consecutive_losses": st.risk.consecutive_losses,
                     "cooldown_until_ts": st.risk.cooldown_until_ts},
            "positions": [
                {"symbol": s, "qty": o.pos.qty, "entry_price": o.pos.entry_price, "entry_ts": o.pos.entry_ts,
                 "stop_price": o.pos.stop_price, "mark": st.marks.get(s, o.pos.entry_price),
                 "notional": o.pos.qty * st.marks.get(s, o.pos.entry_price),
                 "unrealized_pnl": o.pos.qty * (st.marks.get(s, o.pos.entry_price) - o.pos.entry_price),
                 "entry_reason": o.entry_reason}
                for s, o in sorted(st.open.items())],
            "pending": [{"symbol": s, "kind": p.action.kind, "reason": p.action.reason,
                         "size_frac": p.action.size_frac} for s, p in sorted(st.pending.items())],
            "marks": st.marks,
        }

    @app.get("/api/paper/equity")
    def equity(run_id: str | None = run_q, since: int | None = Query(None, description="bar ts, UTC ms"),
               limit: int = Query(5000, ge=1, le=100_000), conn: sqlite3.Connection = Depends(db)):
        if not _has_table(conn, "paper_bars"):
            return []
        q = "SELECT bar_ts, equity, cash, exposure, mode FROM paper_bars WHERE run_id=?"
        args: list = [run_id or app_cfg.paper.run_id]
        if since is not None:
            q += " AND bar_ts >= ?"
            args.append(since)
        rows = _rows(conn.execute(q + " ORDER BY bar_ts DESC LIMIT ?", (*args, limit)))
        return rows[::-1]

    @app.get("/api/paper/trades")
    def trades(run_id: str | None = run_q, symbol: str | None = None,
               limit: int = Query(200, ge=1, le=10_000), conn: sqlite3.Connection = Depends(db)):
        if not _has_table(conn, "paper_trades"):
            return []
        q = "SELECT * FROM paper_trades WHERE run_id=?"
        args: list = [run_id or app_cfg.paper.run_id]
        if symbol:
            q += " AND symbol=?"
            args.append(symbol)
        return _rows(conn.execute(q + " ORDER BY exit_ts DESC LIMIT ?", (*args, limit)))

    @app.get("/api/decisions")
    def decisions(run_id: str | None = run_q, symbol: str | None = None,
                  before: int | None = Query(None, description="only bars before this ts, for paging"),
                  limit: int = Query(100, ge=1, le=2_000), conn: sqlite3.Connection = Depends(db)):
        if not _has_table(conn, "decisions"):
            return []
        q = ("SELECT id, run_id, symbol, bar_ts, model, model_version, probs, abstain, abstain_reason,"
             " latency_ms, input_tokens, cost_usd, passed_threshold, policy_action, policy_reason, created_at"
             " FROM decisions WHERE run_id=?")
        args: list = [run_id or app_cfg.paper.run_id]
        if symbol:
            q += " AND symbol=?"
            args.append(symbol)
        if before is not None:
            q += " AND bar_ts < ?"
            args.append(before)
        rows = _rows(conn.execute(q + " ORDER BY bar_ts DESC, symbol LIMIT ?", (*args, limit)))
        for r in rows:
            r["probs"] = json.loads(r["probs"])
        return rows

    @app.get("/api/decisions/{decision_id}")
    def decision(decision_id: int, conn: sqlite3.Connection = Depends(db)):
        if not _has_table(conn, "decisions"):
            raise HTTPException(404, "no decisions")
        rows = _rows(conn.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)))
        if not rows:
            raise HTTPException(404, f"decision {decision_id} not found")
        d = rows[0]
        d["probs"] = json.loads(d["probs"])
        answers = _rows(conn.execute("SELECT * FROM decision_answers WHERE decision_id=?", (decision_id,)))
        for a in answers:
            a["probabilities"] = json.loads(a["probabilities"])
        d["answers"] = answers
        return d

    @app.get("/api/candles")
    def candles(symbol: str, limit: int = Query(500, ge=1, le=20_000),
                conn: sqlite3.Connection = Depends(db)):
        if not _has_table(conn, "candles"):
            return []
        d = app_cfg.data
        rows = _rows(conn.execute(
            "SELECT ts, open, high, low, close, volume FROM candles WHERE exchange=? AND symbol=?"
            " AND timeframe=? ORDER BY ts DESC LIMIT ?", (d.exchange, symbol, d.timeframe, limit)))
        return rows[::-1]

    @app.get("/api/reports")
    def reports(run_id: str | None = run_q):
        run_id = run_id or app_cfg.paper.run_id
        if not _RUN.match(run_id):
            raise HTTPException(400, "bad run id")
        d = Path(app_cfg.report.output_dir) / run_id
        days = sorted((p.stem for p in d.glob("*.md") if _DAY.match(p.stem)), reverse=True) if d.is_dir() else []
        return {"run_id": run_id, "days": days}

    @app.get("/api/reports/{day}")
    def report(day: str, run_id: str | None = run_q):
        run_id = run_id or app_cfg.paper.run_id
        if not _DAY.match(day) or not _RUN.match(run_id):
            raise HTTPException(400, "day must be YYYY-MM-DD")
        base = Path(app_cfg.report.output_dir) / run_id / day
        md, js = base.with_suffix(".md"), base.with_suffix(".json")
        if not md.exists():
            raise HTTPException(404, f"no report for {day}")
        return {"run_id": run_id, "day": day, "markdown": md.read_text(),
                "data": json.loads(js.read_text()) if js.exists() else None}

    @app.get("/api/backtests")
    def backtests():
        d = Path(app_cfg.backtest.output_dir)
        out = []
        for f in sorted(d.glob("*/summary.json")) if d.is_dir() else []:
            s = json.loads(f.read_text())
            out.append({k: s.get(k) for k in ("run_id", "model", "model_version", "start", "end",
                                              "total_return", "max_drawdown", "sharpe", "trades")})
        return out

    @app.get("/api/backtests/{run_id}")
    def backtest(run_id: str, points: int = Query(1000, ge=10, le=20_000)):
        if not _RUN.match(run_id):
            raise HTTPException(400, "bad run id")
        d = Path(app_cfg.backtest.output_dir) / run_id
        if not (d / "summary.json").exists():
            raise HTTPException(404, f"no backtest {run_id}")
        eq = pd.read_csv(d / "equity.csv")
        step = max(1, len(eq) // points)
        eq = eq.iloc[::step]
        return {"summary": json.loads((d / "summary.json").read_text()),
                "equity": [{"ts": str(t), "equity": float(e)} for t, e in eq.itertuples(index=False)]}

    @app.get("/api/forward/summary")
    def forward_summary():
        """Jev's hourly forward log (data-log branch) scored against realized outcomes."""
        return fwd.summary()

    @app.get("/api/forward/rows")
    def forward_rows(symbol: str | None = None, limit: int = Query(100, ge=1, le=2_000)):
        """Recent decisions, newest first, each with its outcome (null while pending)."""
        return fwd.rows(symbol, limit)

    paper_cache: dict = {}

    @app.get("/api/forward/paper")
    def forward_paper(actions: int = Query(200, ge=0, le=5_000)):
        """Jev's simulated account: the policy and simulator replayed over the forward log's
        answers and closes from forward_log.paper_start on. No model or exchange calls;
        fills are simulated."""
        snap = fwd.snapshot()
        if paper_cache.get("snap") is not snap:   # replay once per forward-log refresh
            start = app_cfg.forward_log.paper_start_ms()
            rows = snap.rows if start is None else [r for r in snap.rows if r["candle_ts"] >= start]
            out = replay(rows, app_cfg, timeframe_ms(app_cfg.data.timeframe))
            out["tracking_since"] = start if start is not None else (out["curve"][0]["bar_ts"] if out["curve"] else None)
            paper_cache.update(snap=snap, out=out)
        out = dict(paper_cache["out"])
        out["actions"] = out["actions"][:actions]
        return out

    return app
