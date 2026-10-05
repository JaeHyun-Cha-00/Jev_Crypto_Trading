"""SQLite storage for OHLCV candles. All timestamps are UTC epoch milliseconds."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd

Candle = Sequence[float]  # [ts_ms, open, high, low, close, volume]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    exchange  TEXT    NOT NULL,
    symbol    TEXT    NOT NULL,
    timeframe TEXT    NOT NULL,
    ts        INTEGER NOT NULL,  -- candle open time, UTC epoch ms
    open      REAL    NOT NULL,
    high      REAL    NOT NULL,
    low       REAL    NOT NULL,
    close     REAL    NOT NULL,
    volume    REAL    NOT NULL,
    PRIMARY KEY (exchange, symbol, timeframe, ts)
);

-- Gaps a sync already tried to backfill. Coinbase leaves out hours with no
-- trades, so thin coins have gaps that never fill; each is fetched once.
CREATE TABLE IF NOT EXISTS candle_gaps_tried (
    exchange  TEXT    NOT NULL,
    symbol    TEXT    NOT NULL,
    timeframe TEXT    NOT NULL,
    after_ts  INTEGER NOT NULL,  -- last candle before the gap
    before_ts INTEGER NOT NULL,  -- first candle after the gap
    PRIMARY KEY (exchange, symbol, timeframe, after_ts, before_ts)
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    p = Path(path)
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


class CandleStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.executescript(_SCHEMA)

    def upsert(self, exchange: str, symbol: str, timeframe: str, candles: Iterable[Candle]) -> int:
        rows = [
            (exchange, symbol, timeframe, int(c[0]), float(c[1]), float(c[2]),
             float(c[3]), float(c[4]), float(c[5]))
            for c in candles
        ]
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?)", rows
            )
        return len(rows)

    def last_ts(self, exchange: str, symbol: str, timeframe: str) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(ts) FROM candles WHERE exchange=? AND symbol=? AND timeframe=?",
            (exchange, symbol, timeframe),
        ).fetchone()
        return row[0]

    def first_ts(self, exchange: str, symbol: str, timeframe: str) -> int | None:
        row = self.conn.execute(
            "SELECT MIN(ts) FROM candles WHERE exchange=? AND symbol=? AND timeframe=?",
            (exchange, symbol, timeframe),
        ).fetchone()
        return row[0]

    def timestamps(self, exchange: str, symbol: str, timeframe: str) -> list[int]:
        cur = self.conn.execute(
            "SELECT ts FROM candles WHERE exchange=? AND symbol=? AND timeframe=? ORDER BY ts",
            (exchange, symbol, timeframe),
        )
        return [r[0] for r in cur]

    def tried_gaps(self, exchange: str, symbol: str, timeframe: str) -> set[tuple[int, int]]:
        cur = self.conn.execute(
            "SELECT after_ts, before_ts FROM candle_gaps_tried WHERE exchange=? AND symbol=? AND timeframe=?",
            (exchange, symbol, timeframe),
        )
        return {(int(a), int(b)) for a, b in cur}

    def mark_gaps_tried(self, exchange: str, symbol: str, timeframe: str,
                        gaps: Iterable[tuple[int, int]]) -> None:
        with self.conn:
            self.conn.executemany(
                "INSERT OR IGNORE INTO candle_gaps_tried VALUES (?,?,?,?,?)",
                [(exchange, symbol, timeframe, a, b) for a, b in gaps],
            )

    def load(
        self,
        exchange: str,
        symbol: str,
        timeframe: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> pd.DataFrame:
        """Return candles as a DataFrame indexed by UTC open time, ascending."""
        q = ("SELECT ts, open, high, low, close, volume FROM candles "
             "WHERE exchange=? AND symbol=? AND timeframe=?")
        args: list = [exchange, symbol, timeframe]
        if start_ms is not None:
            q += " AND ts >= ?"
            args.append(start_ms)
        if end_ms is not None:
            q += " AND ts <= ?"
            args.append(end_ms)
        q += " ORDER BY ts"
        df = pd.read_sql_query(q, self.conn, params=args)
        df.index = pd.to_datetime(df.pop("ts"), unit="ms", utc=True)
        df.index.name = "ts"
        return df
