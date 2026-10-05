import json
from datetime import date

import pytest

from jevtrade.data.store import CandleStore, connect
from jevtrade.decision.base import Decision
from jevtrade.decision.log import DecisionLog
from jevtrade.paper.__main__ import completed_days
from jevtrade.report import ReportConfig, build_report, calibration, format_report, write_report
from jevtrade.report.daily import day_bounds, realized_direction

from conftest import H, T0
from test_paper import FIRST, MultiSource, app_cfg, run_hourly, series


def test_realized_direction():
    assert realized_direction(100, 101.5, 1.0) == "up"
    assert realized_direction(100, 98.9, 1.0) == "down"
    assert realized_direction(100, 101.0, 1.0) == "flat"


def _decision(up, flat, down):
    return Decision("m", "m-1", "h", {"direction": {"up": up, "flat": flat, "down": down}})


def test_calibration_scores_against_realized_moves():
    conn = connect(":memory:")
    cfg = app_cfg()
    # Closes: +2% after 4 bars from bar 0, -2% from bar 1.
    closes = [100, 100, 101, 101, 102, 98, 98, 98, 98, 98]
    CandleStore(conn).upsert(cfg.data.exchange, "BTC/USD", "1h",
                             [[T0 + i * H, c, c, c, c, 1] for i, c in enumerate(closes)])
    log = DecisionLog(conn)
    log.record("r", "BTC/USD", T0, _decision(1.0, 0.0, 0.0), None)        # right: up
    log.record("r", "BTC/USD", T0 + H, _decision(1.0, 0.0, 0.0), None)    # wrong: 100 -> 98 down
    log.record("r", "BTC/USD", T0 + 9 * H, _decision(1.0, 0.0, 0.0), None)  # unresolved
    c = calibration(conn, cfg, "r", T0 + 10 * H)
    assert c["n"] == 2 and c["hit_rate"] == 0.5
    assert c["brier"] == pytest.approx((0 + 2) / 2)
    assert c["base_rates"] == {"up": 0.5, "flat": 0.0, "down": 0.5}
    assert calibration(conn, cfg, "r", T0 + 5 * H)["n"] == 1   # bar 1's outcome not closed yet


def test_daily_report_from_paper_run(tmp_path):
    src = MultiSource(series())
    t = run_hourly(tmp_path / "p.sqlite", src, FIRST, FIRST + 80)
    cfg = app_cfg()
    lo, _ = day_bounds(date(2024, 1, 18))
    r = build_report(t.conn, cfg, "paper", date(2024, 1, 18))
    assert r["bars"] == 24 and r["model"] == "mock"
    assert r["decisions"]["count"] == 48
    bars = dict(t.conn.execute("SELECT bar_ts, equity FROM paper_bars").fetchall())
    assert r["equity_close"] == pytest.approx(bars[lo + 23 * H], abs=0.01)
    assert r["equity_open"] == pytest.approx(bars[lo - H], abs=0.01)
    assert r["realized_pnl"] == pytest.approx(sum(x["pnl"] for x in r["trades"]), abs=0.05)
    assert r["calibration"]["n"] > 0
    md = format_report(r)
    assert "# Paper report 2024-01-18" in md and "Calibration" in md and "no real orders" in md

    path = write_report(t.conn, cfg, "paper", date(2024, 1, 18), ReportConfig(output_dir=str(tmp_path / "out")))
    assert path.read_text() == md
    assert json.loads(path.with_suffix(".json").read_text())["day"] == "2024-01-18"
    # Rewriting is deterministic.
    write_report(t.conn, cfg, "paper", date(2024, 1, 18), ReportConfig(output_dir=str(tmp_path / "out")))
    assert path.read_text() == md


def test_report_for_empty_day(tmp_path):
    conn = connect(tmp_path / "e.sqlite")
    r = build_report(conn, app_cfg(), "paper", date(2024, 1, 1))
    assert r["bars"] == 0 and r["equity_close"] is None
    assert "No resolved decisions yet." in format_report(r)


def test_completed_days_fire_on_last_bar_of_day():
    day = 1_705_536_000_000  # 2024-01-18T00:00Z
    assert completed_days([day + 22 * H], H) == []
    assert completed_days([day + 22 * H, day + 23 * H, day + 24 * H], H) == [date(2024, 1, 18)]
