"""SQLite persistence for the paper loop.

Each processed bar is one transaction: the simulator state, the trades it
closed, its equity row and its decisions commit together or not at all.
`paper_bars` doubles as the idempotency ledger: a bar already in it is never
processed again.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone

from ..sim import SimState, Trade

_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_state (
    run_id        TEXT PRIMARY KEY,
    state         TEXT NOT NULL,      -- SimState JSON
    model         TEXT NOT NULL,
    model_version TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,      -- last committed bar
    heartbeat_at  TEXT                -- last loop iteration, bar or not
);

CREATE TABLE IF NOT EXISTS paper_bars (
    run_id       TEXT    NOT NULL,
    bar_ts       INTEGER NOT NULL,    -- bar open time, UTC ms
    equity       REAL    NOT NULL,    -- at this bar's close
    cash         REAL    NOT NULL,
    exposure     REAL    NOT NULL,    -- position notional / equity
    mode         TEXT    NOT NULL,    -- full | risk_only (catch-up beyond max_catchup_bars)
    processed_at TEXT    NOT NULL,
    PRIMARY KEY (run_id, bar_ts)
);

CREATE TABLE IF NOT EXISTS paper_trades (
    run_id       TEXT    NOT NULL,
    symbol       TEXT    NOT NULL,
    entry_ts     INTEGER NOT NULL,
    entry_price  REAL    NOT NULL,
    exit_ts      INTEGER NOT NULL,
    exit_price   REAL    NOT NULL,
    qty          REAL    NOT NULL,
    fees         REAL    NOT NULL,
    pnl          REAL    NOT NULL,
    ret          REAL    NOT NULL,
    bars_held    INTEGER NOT NULL,
    exit_reason  TEXT    NOT NULL,
    entry_reason TEXT    NOT NULL,
    PRIMARY KEY (run_id, symbol, entry_ts)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PaperStore:
    def __init__(self, conn: sqlite3.Connection, run_id: str):
        self.conn = conn
        self.run_id = run_id
        conn.executescript(_SCHEMA)

    def load(self) -> tuple[SimState, str, str] | None:
        row = self.conn.execute(
            "SELECT state, model, model_version FROM paper_state WHERE run_id=?", (self.run_id,)
        ).fetchone()
        return (SimState.from_json(row[0]), row[1], row[2]) if row else None

    def processed(self, bar_ts: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM paper_bars WHERE run_id=? AND bar_ts=?", (self.run_id, bar_ts)
        ).fetchone() is not None

    def commit_bar(self, st: SimState, model: str, model_version: str, bar_ts: int, mode: str,
                   trades: list[Trade]) -> None:
        """Write one bar's results. Call inside the bar's open transaction."""
        now = _now()
        eq = st.equity()
        notional = sum(o.pos.qty * st.marks.get(s, o.pos.entry_price) for s, o in st.open.items())
        self.conn.execute(
            "INSERT INTO paper_bars VALUES (?,?,?,?,?,?,?)",
            (self.run_id, bar_ts, eq, st.cash, notional / eq if eq > 0 else 0.0, mode, now),
        )
        self.conn.executemany(
            "INSERT INTO paper_trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(self.run_id, *asdict(t).values()) for t in trades],
        )
        self.conn.execute(
            "INSERT INTO paper_state (run_id, state, model, model_version, started_at, updated_at,"
            " heartbeat_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET"
            " state=excluded.state, updated_at=excluded.updated_at, heartbeat_at=excluded.heartbeat_at",
            (self.run_id, st.to_json(), model, model_version, now, now, now),
        )

    def heartbeat(self) -> None:
        with self.conn:
            self.conn.execute("UPDATE paper_state SET heartbeat_at=? WHERE run_id=?", (_now(), self.run_id))

    def trades(self) -> list[Trade]:
        cur = self.conn.execute(
            "SELECT symbol, entry_ts, entry_price, exit_ts, exit_price, qty, fees, pnl, ret, bars_held,"
            " exit_reason, entry_reason FROM paper_trades WHERE run_id=? ORDER BY exit_ts, symbol",
            (self.run_id,),
        )
        return [Trade(*r) for r in cur]

    def equity(self) -> list[tuple[int, float]]:
        cur = self.conn.execute(
            "SELECT bar_ts, equity FROM paper_bars WHERE run_id=? ORDER BY bar_ts", (self.run_id,))
        return [(int(t), float(e)) for t, e in cur]
