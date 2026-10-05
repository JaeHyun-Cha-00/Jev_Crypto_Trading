import pytest

from jevtrade.decision.base import Decision
from jevtrade.policy.engine import (
    AccountView, Policy, PolicyConfig, Position, RiskState, register_trade_result, roll_day, stop_fill,
)

from conftest import H, T0

SYM = "BTC/USDT"


def dec(up=0.0, down=0.0, abstain=False):
    flat = max(0.0, 1 - up - down)
    probs = {} if abstain else {"direction": {"up": up, "flat": flat, "down": down}}
    return Decision("mock", "m", "h", probs, abstain=abstain, abstain_reason="test" if abstain else None)


def acct(equity=10_000.0, positions=None, marks=None, day_start=None, cooldown_until=0):
    return AccountView(
        equity=equity,
        positions=positions or {},
        marks=marks or {},
        risk=RiskState(day_start_equity=day_start or equity, day=T0 // 86_400_000,
                       cooldown_until_ts=cooldown_until),
    )


@pytest.fixture
def pol():
    return Policy(PolicyConfig(), H)


def test_enter_when_confident(pol):
    a = pol.evaluate(SYM, T0, 100.0, dec(up=0.7, down=0.1), acct())
    assert a.kind == "enter" and a.passed_threshold
    # risk 1% / 3% stop = 33% capped at max_position_frac 25%
    assert a.size_frac == pytest.approx(0.25)
    assert a.stop_price == pytest.approx(97.0)
    assert "entry" in a.reason


def test_sizing_uses_risk_budget_when_below_cap():
    p = Policy(PolicyConfig(risk_per_trade=0.005, stop_loss_pct=0.05), H)
    a = p.evaluate(SYM, T0, 100.0, dec(up=0.7), acct())
    assert a.size_frac == pytest.approx(0.1)


@pytest.mark.parametrize("up,down", [(0.5, 0.1), (0.6, 0.55), (0.0, 0.0)])
def test_low_confidence_is_no_trade(pol, up, down):
    a = pol.evaluate(SYM, T0, 100.0, dec(up=up, down=down), acct())
    assert a.kind == "skip" and a.reason.startswith("below_threshold") and not a.passed_threshold


def test_abstain_and_missing_decision_are_no_trade(pol):
    assert pol.evaluate(SYM, T0, 100.0, dec(abstain=True), acct()).reason.startswith("abstain")
    assert pol.evaluate(SYM, T0, 100.0, None, acct()).reason.startswith("no_decision")


def test_gross_exposure_cap_limits_size(pol):
    other = {"ETH/USDT": Position("ETH/USDT", 40.0, 100.0, T0, 90.0)}  # 4000 / 10000 = 40%
    a = pol.evaluate(SYM, T0, 100.0, dec(up=0.7), acct(positions=other, marks={"ETH/USDT": 100.0}))
    assert a.kind == "enter" and a.size_frac == pytest.approx(0.10)
    full = {"ETH/USDT": Position("ETH/USDT", 50.0, 100.0, T0, 90.0)}  # 50%: no room
    a = pol.evaluate(SYM, T0, 100.0, dec(up=0.7), acct(positions=full, marks={"ETH/USDT": 100.0}))
    assert a.kind == "skip" and a.reason.startswith("no_capacity")


def test_stop_loss_overrides_bullish_model(pol):
    pos = {SYM: Position(SYM, 10, 100.0, T0, 97.0)}
    a = pol.evaluate(SYM, T0 + H, 96.5, dec(up=0.99), acct(positions=pos))
    assert a.kind == "exit" and a.reason.startswith("stop_loss")


def test_stop_loss_applies_when_model_abstains(pol):
    pos = {SYM: Position(SYM, 10, 100.0, T0, 97.0)}
    assert pol.evaluate(SYM, T0 + H, 96.0, dec(abstain=True), acct(positions=pos)).kind == "exit"
    assert pol.evaluate(SYM, T0 + H, 98.0, dec(abstain=True), acct(positions=pos)).kind == "hold"


def test_stop_triggers_on_low_and_fills_at_stop_plus_slippage(pol):
    # Bar opens above the stop, wicks through it, and closes back above it.
    pos = {SYM: Position(SYM, 10, 100.0, T0, 97.0)}
    a = pol.evaluate(SYM, T0 + H, 99.0, dec(up=0.99), acct(positions=pos), bar_open=99.5, bar_low=96.0)
    assert a.kind == "exit" and a.reason.startswith("stop_loss")
    assert a.fill_price == pytest.approx(97.0 * (1 - 5 / 10_000))


def test_stop_gap_through_fills_at_open_plus_slippage(pol):
    # Bar opens below the stop: the stop price was never tradable.
    pos = {SYM: Position(SYM, 10, 100.0, T0, 97.0)}
    a = pol.evaluate(SYM, T0 + H, 96.0, dec(up=0.99), acct(positions=pos), bar_open=95.0, bar_low=94.0)
    assert a.kind == "exit" and "gap open" in a.reason
    assert a.fill_price == pytest.approx(95.0 * (1 - 5 / 10_000))


def test_stop_not_hit_when_low_stays_above(pol):
    pos = {SYM: Position(SYM, 10, 100.0, T0, 97.0)}
    a = pol.evaluate(SYM, T0 + H, 98.0, dec(up=0.99), acct(positions=pos), bar_open=99.0, bar_low=97.01)
    assert a.kind == "hold" and a.fill_price is None


def test_stop_fill_uses_configured_slippage():
    assert stop_fill(97.0, 99.0, 98.0, 10) is None
    assert stop_fill(97.0, 99.0, 97.0, 0) == pytest.approx(97.0)    # touch counts
    assert stop_fill(97.0, 99.0, 90.0, 20) == pytest.approx(97.0 * 0.998)
    assert stop_fill(97.0, 97.0, 96.0, 0) == pytest.approx(97.0)    # open exactly at stop
    p = Policy(PolicyConfig(stop_slippage_bps=50), H)
    pos = {SYM: Position(SYM, 10, 100.0, T0, 97.0)}
    a = p.evaluate(SYM, T0 + H, 99.0, dec(), acct(positions=pos), bar_open=99.0, bar_low=96.0)
    assert a.fill_price == pytest.approx(97.0 * 0.995)


def test_max_holding_exit(pol):
    pos = {SYM: Position(SYM, 10, 100.0, T0, 97.0)}
    assert pol.evaluate(SYM, T0 + 22 * H, 101.0, dec(up=0.9), acct(positions=pos)).kind == "hold"
    a = pol.evaluate(SYM, T0 + 23 * H, 101.0, dec(up=0.9), acct(positions=pos))
    assert a.kind == "exit" and a.reason.startswith("max_holding")


def test_model_exit(pol):
    pos = {SYM: Position(SYM, 10, 100.0, T0, 97.0)}
    a = pol.evaluate(SYM, T0 + H, 100.0, dec(up=0.1, down=0.6), acct(positions=pos))
    assert a.kind == "exit" and a.reason.startswith("model_exit")


def test_max_daily_loss_blocks_entries(pol):
    a = pol.evaluate(SYM, T0, 100.0, dec(up=0.9), acct(equity=9_690, day_start=10_000))
    assert a.kind == "skip" and a.reason.startswith("max_daily_loss") and a.passed_threshold
    a = pol.evaluate(SYM, T0, 100.0, dec(up=0.9), acct(equity=9_710, day_start=10_000))
    assert a.kind == "enter"


def test_roll_day_resets_daily_baseline():
    r = RiskState(day_start_equity=10_000, day=T0 // 86_400_000)
    roll_day(r, T0 + 5 * H, 9_000)
    assert r.day_start_equity == 10_000
    roll_day(r, T0 + 25 * H, 9_000)
    assert r.day_start_equity == 9_000


def test_cooldown_after_consecutive_losses(pol):
    cfg = PolicyConfig()
    r = RiskState(day_start_equity=10_000, day=0)
    register_trade_result(r, -10, T0, H, cfg)
    register_trade_result(r, +5, T0, H, cfg)   # win resets the streak
    assert r.consecutive_losses == 0 and r.cooldown_until_ts == 0
    for _ in range(3):
        register_trade_result(r, -10, T0, H, cfg)
    assert r.cooldown_until_ts == T0 + 24 * H

    blocked = pol.evaluate(SYM, T0 + 23 * H, 100.0, dec(up=0.9), acct(cooldown_until=r.cooldown_until_ts))
    assert blocked.kind == "skip" and blocked.reason.startswith("cooldown")
    ok = pol.evaluate(SYM, T0 + 24 * H, 100.0, dec(up=0.9), acct(cooldown_until=r.cooldown_until_ts))
    assert ok.kind == "enter"


def test_config_rejects_position_cap_above_gross():
    with pytest.raises(ValueError):
        PolicyConfig(max_position_frac=0.6, max_gross_exposure=0.5)


def test_every_action_has_reason(pol):
    for d in [dec(up=0.9), dec(up=0.2), dec(abstain=True), None]:
        assert pol.evaluate(SYM, T0, 100.0, d, acct()).reason
