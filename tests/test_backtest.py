import subprocess
import sys

import pandas as pd
import pytest

from jevtrade.backtest import BacktestConfig, Backtester, summarize, write_outputs
from jevtrade.backtest.engine import contamination_label, state_window
from jevtrade.config import AppConfig
from jevtrade.decision.base import Decision, default_questions
from jevtrade.decision.baseline import BaselineModel
from jevtrade.decision.log import DecisionLog
from jevtrade.decision.mock import MockModel
from jevtrade.features.compute import FeatureConfig, compute_features
from jevtrade.policy.engine import Position
from jevtrade.sim import OpenPosition, SimState, Simulator
from jevtrade.data.store import connect
from jevtrade.state.builder import StateConfig, build_state

from conftest import H, synthetic_candles

QUESTIONS = default_questions(4, 1.0)
BULL = {"direction": {"up": 0.9, "flat": 0.05, "down": 0.05}}
WARMUP = 210  # features need 200 bars


def frame(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df.pop("ts"), unit="ms", utc=True)
    return df


def flat_tail(n_warm: int, n_flat: int, price: float = 100.0, seed: int = 0) -> pd.DataFrame:
    """Random-walk warm-up, then bars with open = close = `price` and a tight range."""
    rows = synthetic_candles(n_warm, seed=seed)
    last = rows[-1][0]
    for k in range(1, n_flat + 1):
        rows.append([last + k * H, price, price * 1.001, price * 0.999, price, 1e4])
    return frame(rows)


def at_flat(df) -> BacktestConfig:
    """No costs; decisions start at the first flat bar."""
    return BacktestConfig(fee_bps=0, slippage_bps=0, start=df.index[WARMUP].isoformat())


def bt(model, cfg=None, log=None, horizon=4):
    pcfg = AppConfig().policy  # max_holding_bars follows decision.horizon_bars
    return Backtester(model, QUESTIONS, pcfg, cfg or BacktestConfig(fee_bps=0, slippage_bps=0),
                      H, "1h", horizon, decision_log=log, run_id="test")


def bars(df):
    return [int(ix.value // 1_000_000) for ix in df.index]


def test_entry_fills_at_next_open_and_stop_fills_inside_bar():
    df = flat_tail(WARMUP, 12)
    ts = bars(df)
    first = ts.index(ts[WARMUP])  # first flat bar; state is ready from here
    # Bar first+2 trades down through the stop (97 for an entry at 100) but closes above it.
    i = first + 2
    df.iloc[i, df.columns.get_loc("low")] = 95.0
    res = bt(MockModel(fixed=BULL), cfg=at_flat(df)).run({"X": df})
    t = res.trades[0]
    assert t.entry_ts == ts[first + 1] and t.entry_price == pytest.approx(100.0)  # next open
    assert t.exit_reason.startswith("stop_loss")
    assert t.exit_ts == ts[i]  # same bar as the low, not the next open
    assert t.exit_price == pytest.approx(97.0 * (1 - 5 / 10_000))  # stop less stop_slippage_bps
    assert t.bars_held == 2


def test_gap_through_stop_fills_at_open():
    df = flat_tail(WARMUP, 12)
    i = WARMUP + 2
    for col, v in (("open", 94.0), ("low", 93.0), ("high", 99.0), ("close", 98.0)):
        df.iloc[i, df.columns.get_loc(col)] = v
    t = bt(MockModel(fixed=BULL), cfg=at_flat(df)).run({"X": df}).trades[0]
    assert t.exit_ts == bars(df)[i]
    assert t.exit_price == pytest.approx(94.0 * (1 - 5 / 10_000))


def test_max_holding_defaults_to_horizon():
    assert AppConfig().policy.max_holding_bars == AppConfig().decision.horizon_bars == 4
    df = flat_tail(WARMUP, 20)
    res = bt(MockModel(fixed=BULL), cfg=at_flat(df)).run({"X": df})
    held = [t for t in res.trades if t.exit_reason.startswith("max_holding")]
    assert held and all(t.bars_held == 4 for t in held)
    # The exit is decided at the close of the 4th bar and fills at the next open.
    t = held[0]
    assert t.exit_ts - t.entry_ts == 4 * H


def test_costs_and_equity_reconcile():
    df = frame(synthetic_candles(400, seed=3))
    cfg = BacktestConfig(fee_bps=10, slippage_bps=2)
    res = bt(MockModel(), cfg=cfg).run({"X": df})
    assert res.trades
    assert res.equity.iloc[-1] == pytest.approx(cfg.initial_equity + sum(t.pnl for t in res.trades))
    assert all(t.fees > 0 for t in res.trades)
    # A next-open entry pays slippage over the bar's open.
    t = next(t for t in res.trades)
    open_px = df.loc[pd.Timestamp(t.entry_ts, unit="ms", tz="UTC"), "open"]
    assert t.entry_price == pytest.approx(open_px * 1.0002)


def test_no_look_ahead_future_bars_do_not_change_past_decisions():
    full = frame(synthetic_candles(420, seed=5))
    cut = full.iloc[:350]

    def decisions(df):
        conn = connect(":memory:")
        bt(MockModel(), log=DecisionLog(conn)).run({"X": df})
        return conn.execute("SELECT bar_ts, input_hash, policy_action FROM decisions ORDER BY bar_ts").fetchall()

    a, b = decisions(cut), decisions(full)
    assert a and a == b[: len(a)]


def test_state_window_matches_full_history():
    df = frame(synthetic_candles(1200, seed=7))
    fcfg, scfg = FeatureConfig(), StateConfig()
    feats = compute_features(df, fcfg)
    w = state_window(scfg, fcfg)
    for i in (w, 900, 1199):
        full = build_state(df.iloc[: i + 1], feats.iloc[: i + 1], 4, "1h", scfg, fcfg)
        win = build_state(df.iloc[i + 1 - w: i + 1], feats.iloc[i + 1 - w: i + 1], 4, "1h", scfg, fcfg)
        assert win.text == full.text


def test_pending_entries_count_against_gross_exposure():
    # Three symbols want in on the same bar; 25% each under a 50% gross cap.
    dfs = {s: flat_tail(WARMUP, 6, seed=k) for k, s in enumerate(("A", "B", "C"))}
    res = bt(MockModel(fixed=BULL), cfg=at_flat(dfs["A"])).run(dfs)
    first = min(t.entry_ts for t in res.trades)
    same_bar = [t for t in res.trades if t.entry_ts == first]
    notional = sum(t.qty * t.entry_price for t in same_bar)
    assert notional <= 0.5 * 10_000 + 1e-6


@pytest.mark.parametrize("model", [MockModel(abstain_rate=0.1), BaselineModel(20, 50)],
                         ids=["mock", "baseline"])
def test_end_to_end_with_offline_models(model, tmp_path):
    dfs = {"X": frame(synthetic_candles(600, seed=1)), "Y": frame(synthetic_candles(600, seed=2))}
    conn = connect(":memory:")
    res = bt(model, cfg=BacktestConfig(output_dir=str(tmp_path)), log=DecisionLog(conn)).run(dfs)
    s = summarize(res, H)
    assert s["model"] == model.name and s["contamination"] == "n/a"
    assert s["trades"] == len(res.trades) > 0
    assert s["max_drawdown"] <= 0
    assert set(s["buy_and_hold_return"]) == {"X", "Y"}
    n = conn.execute("SELECT COUNT(*) FROM decisions WHERE run_id='test' AND policy_action IS NOT NULL")
    assert n.fetchone()[0] == s["counts"]["decisions"]
    out = write_outputs(res, s, tmp_path)
    for f in ("summary.json", "summary.md", "trades.csv", "equity.csv"):
        assert (out / f).exists()


def test_start_end_bound_the_period():
    df = frame(synthetic_candles(500, seed=4))
    start, end = df.index[300], df.index[450]
    cfg = BacktestConfig(start=start.isoformat(), end=end.isoformat())
    res = bt(MockModel(), cfg=cfg).run({"X": df})
    assert res.equity.index[0] == start and res.equity.index[-1] == end
    assert res.counts["no_state"] == 0  # history before start still warms the features


def test_contamination_label():
    assert contamination_label("mock-1", 0) == "n/a"
    jan = 1_767_225_600_000  # 2026-01-01
    assert contamination_label("typesafe/jev-1.13-20260917", jan).startswith("potentially contaminated")
    assert not contamination_label("typesafe/jev-1.13-20260917", jan * 2).startswith("potentially")


def test_cli_refuses_jev_without_flag(tmp_path):
    r = subprocess.run([sys.executable, "-m", "jevtrade.backtest", "--model", "jev"],
                       capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode != 0 and "--allow-live-model" in r.stderr


def test_baseline_exits_on_cross_back_not_max_hold():
    df = frame(synthetic_candles(900, seed=11))
    res = bt(BaselineModel(20, 50), cfg=BacktestConfig(fee_bps=0, slippage_bps=0)).run({"X": df})
    assert res.trades and res.sizing["max_holding_bars"] is None
    reasons = {t.exit_reason.split(":")[0] for t in res.trades}
    assert "max_holding" not in reasons and "model_exit" in reasons
    assert max(t.bars_held for t in res.trades) > 4


def test_min_trade_interval_in_backtest():
    df = frame(synthetic_candles(600, seed=1))
    res = bt(MockModel(fixed=BULL), cfg=BacktestConfig(fee_bps=0, slippage_bps=0)).run({"X": df})
    assert len(res.trades) > 2
    for prev, nxt in zip(res.trades, res.trades[1:]):
        # Re-entry is decided >= 4 bars after the exit bar and fills at the next open.
        assert nxt.entry_ts - prev.exit_ts >= 5 * H


def test_summary_shows_sizing(tmp_path):
    df = frame(synthetic_candles(400, seed=3))
    res = bt(MockModel(), cfg=BacktestConfig(output_dir=str(tmp_path))).run({"X": df})
    s = summarize(res, H)
    z = s["sizing"]
    assert z["starting_balance"] == 10_000 and z["risk_per_trade"] == 0.01
    assert z["max_position_frac"] == 0.5 and z["max_daily_loss_pct"] == 0.03
    out = write_outputs(res, s, tmp_path)
    assert "risk-based" in (out / "summary.md").read_text()



class ByState:
    """Fixed probabilities per exact state text; anything else is a clear 'no'."""

    name, version = "mock", "by-state"

    def __init__(self, probs_by_text: dict[str, dict]):
        self.probs_by_text = probs_by_text

    def decide(self, state, questions):
        no = {"direction": {"up": 0.0, "flat": 1.0, "down": 0.0}}
        return Decision(self.name, self.version, state.input_hash, self.probs_by_text.get(state.text, no))


def _state_at(df, i):
    feats = compute_features(df)
    return build_state(df.iloc[: i + 1], feats.iloc[: i + 1], 4, "1h", StateConfig(), FeatureConfig())


def test_entries_go_to_the_strongest_edge_not_config_order():
    # Three symbols signal on the same bar; the 50% gross cap leaves room for one
    # full 33% position and a partial one. Config order is AAA, MMM, ZZZ, but the
    # slots must go to ZZZ (edge 0.90), then MMM (0.65), leaving AAA (0.16) out.
    dfs = {s: flat_tail(WARMUP, 8, seed=k) for k, s in enumerate(("AAA", "MMM", "ZZZ"))}
    edges = {"AAA": (0.56, 0.40), "MMM": (0.70, 0.05), "ZZZ": (0.90, 0.00)}
    probs = {_state_at(dfs[s], WARMUP).text: {"direction": {"up": u, "flat": 1 - u - d, "down": d}}
             for s, (u, d) in edges.items()}
    res = bt(ByState(probs), cfg=at_flat(dfs["AAA"])).run(dfs)
    entries = {t.symbol: t.qty * t.entry_price for t in res.trades}
    assert set(entries) == {"ZZZ", "MMM"}
    assert entries["ZZZ"] == pytest.approx(10_000 / 3, rel=1e-3)   # full 1% risk / 3% stop
    assert entries["MMM"] < entries["ZZZ"]                          # what was left under the cap


def test_held_positions_act_before_entries():
    sim = Simulator(AppConfig().policy, H, 0, 0)
    st = SimState.new(10_000, 0)
    st.open["HELD"] = OpenPosition(Position("HELD", 1.0, 100.0, 0, 97.0), 0.0, "test")

    def d(up, down, abstain=False):
        return Decision("m", "v", "h", {"direction": {"up": up, "flat": 1 - up - down, "down": down}},
                        abstain=abstain)
    decisions = {"AAA": d(0.6, 0.1), "BBB": d(0.9, 0.0), "CCC": d(0.6, 0.1), "HELD": d(0.0, 0.9),
                 "NONE": None, "ABST": d(0.9, 0.0, abstain=True)}
    assert sim.symbols_by_priority(st, decisions) == ["HELD", "BBB", "AAA", "CCC", "ABST", "NONE"]
