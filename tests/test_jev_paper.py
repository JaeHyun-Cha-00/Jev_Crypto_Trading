"""Jev paper account replayed from forward-log rows: no model or network calls."""

import pytest

from jevtrade.api.forward import join
from jevtrade.api.jev_paper import replay
from jevtrade.config import AppConfig

H = 3_600_000
T0 = 1791086400000


def row(sym, i, close, up=0.1, down=0.1, status="answered"):
    probs = {"up": up, "flat": round(1 - up - down, 4), "down": down}
    return {"symbol": sym, "candle_ts": T0 + i * H, "close": close, "status": status,
            "called_at": f"2026-10-04T{i:02d}:07:00Z", "served_model": "typesafe/jev-test",
            "answers": {"direction": {"type": "choice", "choice": max(probs, key=probs.get),
                                      "probabilities": probs}} if status == "answered" else {}}


def run(rows):
    return replay(join(rows, []), AppConfig(), H)


def test_empty_log_is_a_flat_account():
    r = run([])
    assert r["equity"] == r["initial_equity"] and r["trades"] == [] and r["curve"] == []


def test_buy_fills_next_open_and_exits_after_horizon():
    rows = [row("BTC/USD", 0, 100.0, up=0.6, down=0.1)]                    # passes 0.55 and edge 0.10
    rows += [row("BTC/USD", i, 100.0 + i) for i in range(1, 7)]
    rows += [row("ETH/USD", i, 50.0, up=0.5, down=0.1) for i in range(7)]  # below threshold: never bought
    r = run(rows)
    buys = [a for a in r["actions"] if a["action"] == "enter"]
    assert [(a["symbol"], a["bar_ts"]) for a in buys] == [("BTC/USD", T0)]
    assert buys[0]["size_frac"] == pytest.approx(1 / 3)   # 1% risk / 3% stop
    [t] = r["trades"]
    # Entry at the next hour's open (= hour 0's close) plus 2bps slippage.
    assert t["entry_ts"] == T0 + H and t["entry_price"] == pytest.approx(100.0 * 1.0002)
    assert t["exit_reason"].startswith("max_holding") and t["bars_held"] == 4
    assert t["pnl"] > 0 and r["equity"] == pytest.approx(r["initial_equity"] + t["pnl"])
    assert r["per_symbol"]["ETH/USD"] == {"trades": 0, "pnl": 0, "buys": 0}
    assert r["actions"][0]["bar_ts"] == T0 + 6 * H   # newest first
    assert len(r["curve"]) == 7


def test_buy_on_last_hour_is_pending_and_errors_never_trade():
    rows = [row("BTC/USD", 0, 100.0, status="error"), row("BTC/USD", 1, 100.0, status="abstain"),
            row("BTC/USD", 2, 100.0, up=0.7, down=0.0)]
    r = run(rows)
    reasons = {a["bar_ts"]: a["reason"] for a in r["actions"]}
    assert reasons[T0].startswith("no_decision") and reasons[T0 + H].startswith("abstain")
    assert r["trades"] == [] and r["positions"] == []
    assert [(p["symbol"], p["kind"]) for p in r["pending"]] == [("BTC/USD", "enter")]


def test_open_position_is_marked_to_the_last_close():
    rows = [row("BTC/USD", 0, 100.0, up=0.7, down=0.0), row("BTC/USD", 1, 100.0), row("BTC/USD", 2, 101.0)]
    r = run(rows)
    [p] = r["positions"]
    assert p["mark"] == 101.0 and p["unrealized_pnl"] > 0
    assert r["equity"] > r["cash"]


def test_buys_and_calls_cover_every_hour_not_just_recent_actions():
    rows = [row("BTC/USD", 0, 100.0, up=0.7, down=0.0)] + [row("BTC/USD", i, 100.0) for i in range(1, 6)]
    rows += [row("ETH/USD", i, 50.0) for i in range(6)]
    r = run(rows)
    assert r["calls"] == 12 and [b["symbol"] for b in r["buys"]] == ["BTC/USD"]


def test_many_symbols_replay_quickly():
    import time
    rows = [row(f"C{k}/USD", i, 100.0 + (i % 7), up=0.6 if i % 50 == 0 else 0.1, down=0.1)
            for k in range(81) for i in range(48)]
    t = time.perf_counter()
    r = run(rows)
    assert time.perf_counter() - t < 5
    assert r["calls"] == 81 * 48 and len(r["per_symbol"]) == 81


def test_entries_go_to_the_strongest_edge_not_alphabetical_order():
    # Caps leave room for less than three full positions; the weakest signal must lose out.
    rows = [row("AAA/USD", 0, 10.0, up=0.56, down=0.40),   # edge 0.16
            row("MMM/USD", 0, 10.0, up=0.70, down=0.05),   # edge 0.65
            row("ZZZ/USD", 0, 10.0, up=0.80, down=0.00)]   # edge 0.80
    rows += [row(s, 1, 10.0) for s in ("AAA/USD", "MMM/USD", "ZZZ/USD")]
    r = run(rows)
    hour0 = [a for a in reversed(r["actions"]) if a["bar_ts"] == T0]
    assert [a["symbol"] for a in hour0] == ["ZZZ/USD", "MMM/USD", "AAA/USD"]
    bought = {a["symbol"]: a["size_frac"] for a in hour0 if a["action"] == "enter"}
    assert "ZZZ/USD" in bought and "MMM/USD" in bought
    assert bought.get("AAA/USD", 0) < bought["MMM/USD"]


def _at(i, minute):
    """ISO time `minute` minutes into hour i (T0 is 04:00 UTC)."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp((T0 + i * H) / 1000 + minute * 60, tz=timezone.utc).isoformat()


def test_buy_fills_when_the_run_happened_at_the_interpolated_price():
    # Hour 0's answer was logged by a run 30 minutes into hour 1 (GitHub started late).
    rows = [dict(row("BTC/USD", 0, 100.0, up=0.6, down=0.1), called_at=_at(1, 30))]
    rows += [row("BTC/USD", 1, 110.0), row("BTC/USD", 2, 110.0)]
    r = run(rows)
    [p] = r["positions"]
    assert p["entry_ts"] == T0 + H
    # Halfway through hour 1: between its open (100) and close (110), plus 2bps slippage.
    assert p["entry_price"] == pytest.approx(105.0 * 1.0002)


def test_a_run_hours_late_fills_hours_later():
    rows = [dict(row("BTC/USD", 0, 100.0, up=0.6, down=0.1), called_at=_at(3, 0))]
    rows += [row("BTC/USD", i, 100.0 + i) for i in range(1, 3)]
    r = run(rows)
    # The run that saw the answer had not happened by the last logged hour.
    assert r["positions"] == [] and [p["kind"] for p in r["pending"]] == ["enter"]
    assert r["pending"][0]["fill_after"] == T0 + 3 * H
    r = run(rows + [row("BTC/USD", 3, 103.0), row("BTC/USD", 4, 104.0)])
    [p] = r["positions"]
    assert p["entry_ts"] == T0 + 3 * H and p["entry_price"] == pytest.approx(102.0 * 1.0002)


def test_rows_without_called_at_fill_at_the_next_open():
    rows = [dict(row("BTC/USD", 0, 100.0, up=0.6, down=0.1), called_at=None), row("BTC/USD", 1, 104.0)]
    [p] = run(rows)["positions"]
    assert p["entry_ts"] == T0 + H and p["entry_price"] == pytest.approx(100.0 * 1.0002)
