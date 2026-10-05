"""Decision log: every model call is recorded, including abstains and errors."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .base import Decision

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT    NOT NULL,      -- 'paper' or a backtest run id
    symbol        TEXT    NOT NULL,
    bar_ts        INTEGER NOT NULL,      -- decision bar open time, UTC ms
    model         TEXT    NOT NULL,
    model_version TEXT    NOT NULL,
    input_hash    TEXT    NOT NULL,
    input_text    TEXT,
    probs         TEXT    NOT NULL,      -- JSON {question: {option: p}}
    abstain       INTEGER NOT NULL,
    abstain_reason TEXT,
    raw_output    TEXT,
    latency_ms    REAL,
    input_tokens  INTEGER,
    cost_usd      REAL,
    passed_threshold INTEGER,            -- filled by the policy layer
    policy_action TEXT,
    policy_reason TEXT,
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decisions_run ON decisions(run_id, symbol, bar_ts);
"""


class DecisionLog:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.executescript(_SCHEMA)

    def record(
        self, run_id: str, symbol: str, bar_ts: int, d: Decision, input_text: str | None
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO decisions (run_id, symbol, bar_ts, model, model_version, input_hash,"
                " input_text, probs, abstain, abstain_reason, raw_output, latency_ms, input_tokens,"
                " cost_usd, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, symbol, bar_ts, d.model, d.model_version, d.input_hash, input_text,
                 json.dumps(d.probs), int(d.abstain), d.abstain_reason, d.raw_output,
                 d.latency_ms, d.input_tokens, d.cost_usd,
                 datetime.now(timezone.utc).isoformat()),
            )
        return int(cur.lastrowid)

    def annotate(self, decision_id: int, passed: bool, action: str, reason: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE decisions SET passed_threshold=?, policy_action=?, policy_reason=? WHERE id=?",
                (int(passed), action, reason, decision_id),
            )
