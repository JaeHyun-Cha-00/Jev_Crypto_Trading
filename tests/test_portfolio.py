"""Jev paper account replayed from forward-log rows: no model or network calls."""

import pytest

from jevtrade.forward.log import join
from jevtrade.forward.portfolio import replay
from jevtrade.forward.settings import JevPaperConfig
from jevtrade.config import AppConfig

H = 3_600_000
T0 = 1791086400000


def row(sym, i, close, up=0.1, down=0.1, status="answered"):
    probs = {"up": up, "flat": round(1 - up - down, 4), "down": down}
    return {"symbol": sym, "candle_ts": T0 + i * H, "close": close, "status": status,
            "called_at": f"2026-10-04T{i:02d}:07:00Z", "served_model": "typesafe/jev-test",
            "answers": {"direction": {"type": "choice", "choice": max(probs, key=probs.get),
                                      "probabilities": probs}} if status == "answered" else {}}


def old_costs():
    """The pre-spread cost model (10bps fee, 2bps slippage), so timing tests read in round numbers."""
    cfg = AppConfig()
    cfg.jev_paper = JevPaperConfig(fee_bps=10, slippage_bps=2, spread_bps=0, thin_extra_bps=0)
    return cfg


def run(rows, cfg=None):
    return replay(join(rows, []), cfg or old_costs(), H)


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


def _ohlc(r, o, h, lo, v=1000.0):
    return dict(r, open=o, high=h, low=lo, volume=v)


def test_stop_triggers_on_the_logged_intrabar_low():
    # Hour 2 dips 5% inside the hour but closes flat: only the real low reveals it.
    rows = [_ohlc(row("BTC/USD", 0, 100.0, up=0.6, down=0.1), 100.0, 100.5, 99.5),
            _ohlc(row("BTC/USD", 1, 100.0), 100.0, 100.5, 99.5),
            _ohlc(row("BTC/USD", 2, 100.0), 100.0, 100.5, 95.0),
            _ohlc(row("BTC/USD", 3, 100.0), 100.0, 100.5, 99.5)]
    [t] = run(rows)["trades"]
    assert t["exit_reason"].startswith("stop_loss") and t["exit_ts"] == T0 + 2 * H
    stop = 100.0 * 1.0002 * 0.97
    assert t["exit_price"] == pytest.approx(stop * (1 - 5 / 10_000))
    # The same hours logged with closes only never stop out.
    closes_only = run([{k: v for k, v in r.items() if k not in ("open", "high", "low")} for r in rows])
    assert closes_only["trades"] == [] and len(closes_only["positions"]) == 1


def test_close_only_rows_still_open_at_the_previous_close():
    rows = [row("BTC/USD", 0, 100.0, up=0.6, down=0.1), _ohlc(row("BTC/USD", 1, 104.0), 101.0, 105.0, 100.5)]
    [p] = run(rows)["positions"]
    assert p["entry_price"] == pytest.approx(101.0 * 1.0002)   # hour 1's logged open, not hour 0's close


def test_a_late_fill_only_sees_part_of_the_hours_dip():
    from jevtrade.forward.portfolio import Bar, _fill_bar
    bar = Bar(100.0, 110.0, 90.0, 100.0)
    rest = _fill_bar(bar, 0, int(0.75 * H), H)
    assert rest.open == 100.0 and rest.low == pytest.approx(97.5) and rest.high == pytest.approx(102.5)
    assert _fill_bar(bar, 0, 0, H) == bar


def test_robinhood_spread_is_paid_on_both_sides_and_wider_for_thin_coins():
    cfg = AppConfig()   # defaults: no fee, 95bps per side, up to +20bps for thin coins
    assert cfg.jev_paper.fee_bps == 0
    deep, thin = 1_000_000.0, 10.0          # x $100 close: $100M vs $1k an hour
    rows = []
    for sym, vol in (("BTC/USD", deep), ("PNUT/USD", thin)):
        rows += [_ohlc(row(sym, 0, 100.0, up=0.6, down=0.1), 100.0, 100.0, 100.0, vol)]
        rows += [_ohlc(row(sym, i, 100.0), 100.0, 100.0, 100.0, vol) for i in range(1, 6)]
    r = run(rows, cfg)
    by = {t["symbol"]: t for t in r["trades"]}
    assert by["BTC/USD"]["entry_price"] == pytest.approx(100.95)
    assert by["BTC/USD"]["exit_price"] == pytest.approx(99.05)
    assert by["PNUT/USD"]["entry_price"] == pytest.approx(101.15)
    assert by["BTC/USD"]["fees"] == 0 and by["BTC/USD"]["pnl"] < 0   # a flat price still loses ~1.9%
    assert r["spread_bps"] == {"min": 95, "max": 115}


def test_logged_robinhood_quotes_replace_the_spread_estimate():
    cfg = AppConfig()   # estimate would be 95-115bps
    rows = [_ohlc(row("BTC/USD", 0, 100.0, up=0.6, down=0.1), 100.0, 100.0, 100.0, 1e6)]
    rows += [_ohlc(row("BTC/USD", i, 100.0), 100.0, 100.0, 100.0, 1e6) for i in range(1, 6)]
    rows[0].update(rh_bid=99.5, rh_ask=100.5)   # 50bps per side, carried forward to later fills
    rows[3].update(rh_bid=99.0, rh_ask=101.0)   # 100bps from hour 3 on
    r = run(rows, cfg)
    t = r["trades"][0]
    assert t["entry_price"] == pytest.approx(100.5)
    assert t["exit_price"] == pytest.approx(99.0)
    assert r["spread_source"] == "robinhood" and r["quoted_symbols"] == 1
    assert r["spread_bps"] == {"min": pytest.approx(100), "max": pytest.approx(100)}


def test_without_quotes_the_estimate_is_reported():
    r = run([row("BTC/USD", 0, 100.0)], AppConfig())
    assert r["spread_source"] == "estimate" and r["quoted_symbols"] == 0


def test_spread_scales_on_log_volume():
    c = JevPaperConfig()
    assert c.spread_for(5e6) == c.spread_for(5e9) == 95
    assert c.spread_for(5e5) == pytest.approx(105) and c.spread_for(5e4) == pytest.approx(115)
    assert c.spread_for(None) == c.spread_for(0) == 115


def test_buys_are_capped_at_a_share_of_hourly_dollar_volume():
    cfg = old_costs()
    cfg.jev_paper.max_volume_frac = 0.01
    # $100 x 500 = $50k an hour: at most $500 of it, though the rules want ~$3.3k.
    rows = [_ohlc(row("PNUT/USD", 0, 100.0, up=0.6, down=0.1), 100.0, 100.0, 100.0, 500.0),
            _ohlc(row("PNUT/USD", 1, 100.0), 100.0, 100.0, 100.0, 500.0)]
    [p] = run(rows, cfg)["positions"]
    assert p["qty"] * p["entry_price"] == pytest.approx(500.0)
    cfg.jev_paper.max_volume_frac = None
    [p] = run(rows, cfg)["positions"]
    assert p["qty"] * p["entry_price"] == pytest.approx(10_000 / 3, rel=1e-3)


def gated(lookback, min_signals):
    """Old costs, no time exit, a wide stop, and the skill gate on (horizon: 4 bars)."""
    cfg = old_costs()
    cfg.jev_paper.gate_lookback_hours = lookback
    cfg.jev_paper.gate_min_signals = min_signals
    cfg.policy = cfg.policy.model_copy(update={"max_holding_bars_by_model": {"jev": None},
                                               "stop_loss_pct": 0.5})
    return cfg


def test_skill_gate_waits_for_enough_resolved_signals():
    rows = [row("BTC/USD", i, 100.0 + i, up=0.7, down=0.0) for i in range(8)]
    r = run(rows, gated(24, 1000))
    assert r["buys"] == [] and r["positions"] == [] and r["pending"] == []
    blocked = [a for a in r["actions"] if a["reason"].startswith("skill_gate")]
    assert len(blocked) == 8 and "of 1000 buy signals" in blocked[0]["reason"]
    assert r["gate"]["open"] is False and r["gate"]["signals"] == 4   # hours 0-3 resolved by hour 7


def test_skill_gate_opens_once_signals_paid_and_beat_the_average_coin():
    rows = [row("BTC/USD", i, 100.0 + 2 * i, up=0.7, down=0.0) for i in range(8)]
    rows += [row("ETH/USD", i, 50.0) for i in range(8)]   # flat: the average coin rose less than BTC
    r = run(rows, gated(24, 2))
    # Hour 0's and 1's signals resolve at hours 4 and 5: the first buy waits for hour 5.
    assert [(b["symbol"], b["bar_ts"]) for b in r["buys"]] == [("BTC/USD", T0 + 5 * H)]
    assert all(a["reason"].startswith("skill_gate") for a in r["actions"]
               if a["symbol"] == "BTC/USD" and a["bar_ts"] < T0 + 5 * H)
    g = r["gate"]
    assert g["open"] is True and g["avg_net"] > 0 and g["avg_excess"] > 0


def test_skill_gate_stays_closed_when_signals_lost_money():
    rows = [row("BTC/USD", i, 100.0 - 2 * i, up=0.7, down=0.0) for i in range(10)]
    rows += [row("ETH/USD", i, 50.0) for i in range(10)]
    r = run(rows, gated(24, 1))
    assert r["buys"] == []
    assert "after costs" in r["gate"]["reason"] and r["gate"]["avg_net"] < 0


def test_skill_gate_never_blocks_a_sale():
    # Signals pay early (gate opens, BTC is bought), then lose (gate closes); Jev's later
    # "down" answer still sells the coin.
    closes = [100, 102, 104, 106, 108, 110, 100, 98, 96, 94, 92]
    rows = [row("BTC/USD", i, float(c), up=0.7, down=0.0) for i, c in enumerate(closes[:9])]
    rows += [row("BTC/USD", 9, 94.0, up=0.0, down=0.7), row("BTC/USD", 10, 92.0)]
    rows += [row("ETH/USD", i, 50.0) for i in range(11)]
    r = run(rows, gated(5, 1))
    assert [b["bar_ts"] for b in r["buys"]] == [T0 + 4 * H]
    [t] = r["trades"]
    assert t["exit_reason"].startswith("model_exit") and t["exit_ts"] == T0 + 10 * H
    assert r["gate"]["open"] is False


def test_hours_show_each_hours_picks_what_the_account_did_and_how_they_did():
    rows = [row("BTC/USD", 0, 100.0, up=0.7, down=0.0), row("SOL/USD", 0, 20.0, up=0.6, down=0.2)]
    rows += [row("BTC/USD", i, 100.0 + i) for i in range(1, 6)]
    rows += [row("SOL/USD", i, 20.0) for i in range(1, 6)]
    rows += [row("ETH/USD", i, 50.0, status="error" if i == 0 else "answered") for i in range(6)]
    r = run(rows)
    assert [h["bar_ts"] for h in r["hours"]] == [T0 + i * H for i in range(5, -1, -1)]   # newest first
    h = r["hours"][-1]
    assert (h["asked"], h["answered"]) == (3, 2)
    assert [p["symbol"] for p in h["picks"]] == ["BTC/USD", "SOL/USD"]   # highest p(up) first
    btc, sol = h["picks"]
    assert btc["action"] == "enter" and btc["why"] == "entry" and h["bought"] == ["BTC/USD", "SOL/USD"]
    # Resolved at hour 4 (horizon 4): BTC rose 4%, SOL and ETH were flat.
    assert btc["ret"] == pytest.approx(0.04) and btc["net"] == pytest.approx(0.04 - 2 * 12 / 10_000)
    assert sol["ret"] == 0.0 and h["market"] == pytest.approx(0.04 / 3)
    assert h["resolves_at"] == T0 + 4 * H
    later = r["hours"][0]   # hour 5: no picks, horizon still open
    assert later["picks"] == [] and later["market"] is None and later["bought"] == []


def test_hours_list_picks_the_skill_gate_held_back():
    rows = [row("BTC/USD", i, 100.0 + i, up=0.7, down=0.0) for i in range(8)]
    r = run(rows, gated(24, 1000))
    assert all(h["held_back"] == 1 and h["bought"] == [] for h in r["hours"])
    [p] = r["hours"][0]["picks"]
    assert (p["symbol"], p["action"], p["why"]) == ("BTC/USD", "skip", "skill_gate")


def test_an_hour_the_collector_skipped_still_gets_a_row():
    rows = [row("BTC/USD", i, 100.0) for i in (0, 1, 3)]
    r = run(rows)
    assert [(h["bar_ts"], h["asked"]) for h in r["hours"]] == [(T0 + 3 * H, 1), (T0 + 2 * H, 0), (T0 + H, 1), (T0, 1)]
    assert r["hours"][1]["picks"] == [] and r["hours"][1]["market"] is None
