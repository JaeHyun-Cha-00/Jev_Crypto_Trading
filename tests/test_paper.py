import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from jevtrade.backtest import BacktestConfig, Backtester
from jevtrade.config import AppConfig
from jevtrade.data.store import CandleStore, connect
from jevtrade.decision.mock import MockModel
from jevtrade.paper import PaperLock, PaperLockError, PaperStore, PaperTrader
from jevtrade.paper.engine import seconds_until_next_close

from conftest import H, T0, synthetic_candles

SYMS = ("BTC/USD", "ETH/USD")
N = 700           # candles per symbol
FIRST = 400       # paper trading starts at this bar


class MultiSource:
    """Public-OHLCV stand-in serving a different series per symbol."""

    def __init__(self, series: dict[str, list[list[float]]], lag: dict[str, int] | None = None):
        self.series = series
        self.lag = lag or {}   # symbol -> bars its latest candles are withheld
        self.now_ms = 0

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        # Closed candles only, minus the `lag` most recent ones for a slow symbol.
        cutoff = self.now_ms - (self.lag.get(symbol, 0) + 1) * H
        rows = [c for c in self.series[symbol] if (since is None or c[0] >= since) and c[0] <= cutoff]
        return rows[: limit or 500]


def app_cfg(**paper) -> AppConfig:
    return AppConfig.model_validate({
        "data": {"symbols": list(SYMS), "start": "2024-01-01T00:00:00Z", "page_limit": 300},
        "paper": {"fee_bps": 10, "slippage_bps": 2, **paper},
    })


def series():
    return {s: synthetic_candles(N, seed=k + 1) for k, s in enumerate(SYMS)}


def close_of(i: int) -> int:
    """`now` just after bar i closed."""
    return T0 + (i + 1) * H + 1_000


def trader(path, src, model=None, **paper):
    return PaperTrader(app_cfg(**paper), connect(path), model or MockModel(abstain_rate=0.1), src)


def run_hourly(path, src, lo, hi, **kw):
    t = trader(path, src, **kw)
    for i in range(lo, hi):
        src.now_ms = close_of(i)
        t.step(src.now_ms)
    return t


def test_paper_matches_backtest(tmp_path):
    data = series()
    src = MultiSource(data)
    t = run_hourly(tmp_path / "p.sqlite", src, FIRST, N)

    cfg = app_cfg()
    candles = CandleStore(connect(":memory:"))
    for s, rows in data.items():
        candles.upsert("x", s, "1h", rows)
    bt_cfg = BacktestConfig(fee_bps=10, slippage_bps=2, start=_iso(T0 + FIRST * H))
    res = Backtester(MockModel(abstain_rate=0.1), cfg.decision.resolved_questions("1h"), cfg.policy,
                     bt_cfg, H, "1h", cfg.decision.horizon_bars, cfg.features, cfg.state,
                     run_id="bt").run({s: candles.load("x", s, "1h") for s in SYMS})

    paper_trades = t.store.trades()
    bt_trades = sorted((x for x in res.trades if not x.exit_reason.startswith("end_of_backtest")),
                       key=lambda x: (x.exit_ts, x.symbol))
    assert paper_trades
    assert [(x.symbol, x.entry_ts, x.exit_ts, x.exit_reason) for x in paper_trades] == \
           [(x.symbol, x.entry_ts, x.exit_ts, x.exit_reason) for x in bt_trades]
    assert [x.pnl for x in paper_trades] == pytest.approx([x.pnl for x in bt_trades])
    eq = t.store.equity()
    # The backtest rewrites its last point after closing leftovers; compare the rest.
    assert [e for _, e in eq][:-1] == pytest.approx(list(res.equity.values)[:-1])


def test_step_is_idempotent_per_closed_candle(tmp_path):
    src = MultiSource(series())
    path = tmp_path / "p.sqlite"
    t = run_hourly(path, src, FIRST, FIRST + 30)
    conn = t.conn
    snap = [conn.execute(f"SELECT COUNT(*) FROM {tb}").fetchone()[0]
            for tb in ("decisions", "paper_bars", "paper_trades")]
    state = conn.execute("SELECT state FROM paper_state").fetchone()[0]
    for _ in range(3):
        r = t.step(src.now_ms)              # same candle again
        assert r.processed == []
        r = t.step(src.now_ms + 20 * 60_000)  # later, but the next candle has not closed
        assert r.processed == []
    assert snap == [conn.execute(f"SELECT COUNT(*) FROM {tb}").fetchone()[0]
                    for tb in ("decisions", "paper_bars", "paper_trades")]
    assert state == conn.execute("SELECT state FROM paper_state").fetchone()[0]
    # One decision per symbol per bar, never duplicated.
    dup = conn.execute("SELECT COUNT(*) FROM (SELECT symbol, bar_ts FROM decisions WHERE run_id='paper'"
                       " GROUP BY 1, 2 HAVING COUNT(*) > 1)").fetchone()[0]
    assert dup == 0


def test_restart_resumes_identically(tmp_path):
    a, b = MultiSource(series()), MultiSource(series())
    one = run_hourly(tmp_path / "one.sqlite", a, FIRST, FIRST + 120)
    run_hourly(tmp_path / "two.sqlite", b, FIRST, FIRST + 50)
    # A new process (fresh connection and trader) picks up where the last one stopped.
    two = run_hourly(tmp_path / "two.sqlite", b, FIRST + 50, FIRST + 120)
    assert one.store.trades() == two.store.trades()
    assert one.store.equity() == two.store.equity()


def test_downtime_is_caught_up_in_order(tmp_path):
    a, b = MultiSource(series()), MultiSource(series())
    one = run_hourly(tmp_path / "one.sqlite", a, FIRST, FIRST + 60)
    run_hourly(tmp_path / "two.sqlite", b, FIRST, FIRST + 20)
    b.now_ms = close_of(FIRST + 59)    # down for 40 bars, under max_catchup_bars
    two = trader(tmp_path / "two.sqlite", b)
    r = two.step(b.now_ms)
    assert len(r.processed) == 40 and not r.risk_only
    assert one.store.trades() == two.store.trades()
    assert one.store.equity() == two.store.equity()


def test_long_downtime_beyond_catchup_is_risk_only(tmp_path):
    src = MultiSource(series())
    path = tmp_path / "p.sqlite"
    run_hourly(path, src, FIRST, FIRST + 5, max_catchup_bars=10)
    src.now_ms = close_of(FIRST + 44)
    t = trader(path, src, max_catchup_bars=10)
    before = t.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    r = t.step(src.now_ms)
    assert len(r.processed) == 40 and len(r.risk_only) == 30
    after = t.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    assert after - before == 10 * len(SYMS)   # the model is only asked about the last 10 bars
    modes = dict(t.conn.execute("SELECT mode, COUNT(*) FROM paper_bars GROUP BY mode").fetchall())
    assert modes["risk_only"] == 30


def test_crash_mid_bar_rolls_back_and_retries(tmp_path):
    src = MultiSource(series())
    path = tmp_path / "p.sqlite"
    run_hourly(path, src, FIRST, FIRST + 10)

    class Boom(MockModel):
        calls = 0

        def decide(self, state, questions):
            Boom.calls += 1
            if Boom.calls == 2:  # second symbol of the bar
                raise RuntimeError("network down")
            return super().decide(state, questions)

    src.now_ms = close_of(FIRST + 10)
    t = trader(path, src, model=Boom(abstain_rate=0.1))
    counts = lambda: [t.conn.execute(f"SELECT COUNT(*) FROM {tb}").fetchone()[0]  # noqa: E731
                      for tb in ("decisions", "paper_bars", "paper_trades")]
    before = counts()
    with pytest.raises(RuntimeError):
        t.step(src.now_ms)
    assert counts() == before          # nothing from the half-done bar was committed
    r = t.step(src.now_ms)
    assert len(r.processed) == 1
    assert counts()[0] == before[0] + len(SYMS)

    ref = MultiSource(series())
    clean = run_hourly(tmp_path / "ref.sqlite", ref, FIRST, FIRST + 11)
    assert clean.store.equity() == t.store.equity()


BULL = {"direction": {"up": 0.9, "flat": 0.05, "down": 0.05}}
BEAR = {"direction": {"up": 0.05, "flat": 0.05, "down": 0.9}}


def test_waits_for_a_lagging_held_symbol(tmp_path):
    src = MultiSource(series())
    t = run_hourly(tmp_path / "p.sqlite", src, FIRST, FIRST + 2, model=MockModel(fixed=BULL),
                   stall_grace_bars=3)
    st = t.store.load()[0]
    assert "ETH/USD" in st.open or "ETH/USD" in st.pending
    src.lag = {"ETH/USD": 1}
    src.now_ms = close_of(FIRST + 2)
    r = t.step(src.now_ms)
    assert r.processed == [] and r.waiting_on == ["ETH/USD"]
    src.lag = {}
    r = t.step(src.now_ms + 60_000)
    assert r.processed == [T0 + (FIRST + 2) * H] and not r.waiting_on


def test_lagging_flat_symbol_is_not_waited_for(tmp_path):
    # Nothing is ever bought, so a coin with no candle yet is just skipped that bar.
    src = MultiSource(series(), lag={"ETH/USD": 1})
    t = trader(tmp_path / "p.sqlite", src, model=MockModel(fixed=BEAR), stall_grace_bars=3)
    src.now_ms = close_of(FIRST)
    r = t.step(src.now_ms)
    assert r.processed == [T0 + FIRST * H] and not r.waiting_on


def test_lagging_held_symbol_is_skipped_after_grace(tmp_path):
    src = MultiSource(series())
    t = run_hourly(tmp_path / "p.sqlite", src, FIRST, FIRST + 2, model=MockModel(fixed=BULL),
                   stall_grace_bars=3)
    src.lag = {"ETH/USD": 10}
    for i in (FIRST + 2, FIRST + 3, FIRST + 4):
        src.now_ms = close_of(i)
        assert t.step(src.now_ms).processed == []
    src.now_ms = close_of(FIRST + 5)
    assert t.step(src.now_ms).processed == [T0 + (FIRST + 2) * H]  # 3 bars late: go without ETH


def test_model_change_on_restart_is_refused(tmp_path):
    src = MultiSource(series())
    path = tmp_path / "p.sqlite"
    run_hourly(path, src, FIRST, FIRST + 3)
    src.now_ms = close_of(FIRST + 3)
    with pytest.raises(SystemExit, match="run_id"):
        trader(path, src, model=MockModel(version="mock-2")).step(src.now_ms)


def test_lock_allows_one_loop(tmp_path):
    with PaperLock(tmp_path / "x.lock"):
        with pytest.raises(PaperLockError):
            with PaperLock(tmp_path / "x.lock"):
                pass
    with PaperLock(tmp_path / "x.lock"):  # released
        pass


def test_seconds_until_next_close():
    assert seconds_until_next_close(T0, H, 30) == pytest.approx(3600 + 30)
    assert seconds_until_next_close(T0 + H - 1_000, H, 30) == pytest.approx(31)


def test_paper_code_cannot_reach_order_endpoints():
    src = Path(__file__).resolve().parents[1] / "src" / "jevtrade"
    for f in [*(src / "paper").glob("*.py"), src / "sim.py"]:
        text = f.read_text()
        assert not re.search(r"^\s*(import|from)\s+ccxt", text, re.M), f
        assert not re.search(r"order\(|apiKey|secret", text), f


def test_status_cli_and_jev_refusal(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"storage: {{sqlite_path: {tmp_path / 'p.sqlite'}}}\n")
    r = subprocess.run([sys.executable, "-m", "jevtrade.paper", "status", "--config", str(cfg)],
                       capture_output=True, text=True)
    assert r.returncode == 0 and "not started" in r.stdout
    cfg.write_text(cfg.read_text() + "decision: {model: jev}\n")
    r = subprocess.run([sys.executable, "-m", "jevtrade.paper", "run", "--once", "--config", str(cfg)],
                       capture_output=True, text=True,
                       env={k: v for k, v in os.environ.items() if k != "JEVTRADE_ALLOW_LIVE_MODEL"})
    assert r.returncode != 0 and "--allow-live-model" in r.stderr


def _iso(ms: int) -> str:
    from jevtrade.data.timeframes import ms_to_iso
    return ms_to_iso(ms)
