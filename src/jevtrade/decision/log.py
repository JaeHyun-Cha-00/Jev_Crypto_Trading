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

-- One row per answered question, for calibration against realized outcomes
-- (join on decisions.symbol / bar_ts and the candles that follow).
CREATE TABLE IF NOT EXISTS decision_answers (
    decision_id     INTEGER NOT NULL REFERENCES decisions(id),
    question        TEXT    NOT NULL,
    qtype           TEXT    NOT NULL,   -- choice | noul
    choice          TEXT,               -- selected option ('true'/'false' for noul at 0.5)
    probabilities   TEXT    NOT NULL,   -- JSON {option: p}; noul -> {true: p, false: 1-p}
    noul            REAL,               -- P(yes) as returned, noul questions only
    confidence      REAL,               -- model-reported confidence (choice only)
    gate_confidence REAL,               -- choice: confidence; noul: |2p - 1|
    PRIMARY KEY (decision_id, question)
);
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
            decision_id = int(cur.lastrowid)
            self.conn.executemany(
                "INSERT INTO decision_answers (decision_id, question, qtype, choice, probabilities,"
                " noul, confidence, gate_confidence) VALUES (?,?,?,?,?,?,?,?)",
                [(decision_id, q, a["type"], a.get("choice"), json.dumps(a["probabilities"]),
                  a.get("noul"), a.get("confidence"), a.get("gate_confidence"))
                 for q, a in (d.extra.get("answers") or {}).items()],
            )
        return decision_id

    def annotate(self, decision_id: int, passed: bool, action: str, reason: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE decisions SET passed_threshold=?, policy_action=?, policy_reason=? WHERE id=?",
                (int(passed), action, reason, decision_id),
            )
