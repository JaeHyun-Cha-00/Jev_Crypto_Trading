"""Jev's fixed prompt (jevtrade.decision.agent) and the account it trades (jevtrade.forward.agent)."""

import json
from pathlib import Path

import pytest

from jevtrade.config import load_config
from jevtrade.decision import agent
from jevtrade.forward.agent import Account, replay, run
from jevtrade.forward.settings import JevPaperConfig
from jevtrade.state.builder import MarketState

H = 3_600_000
T = 1_800_000_000_000 - 1_800_000_000_000 % H


def row(sym, ts, q, pick, p=0.7, close=100.0, **kw):
    opts = {"entry": ["buy_small", "buy_normal", "buy_large", "wait"], "manage": ["hold", "sell"]}[q]
    probs = {o: (p if o == pick else (1 - p) / (len(opts) - 1)) for o in opts}
    return {"symbol": sym, "candle_ts": ts, "close": close, "high": close, "low": close, "volume": 1e9,
            "status": "answered", "question": q, "called_at": f"t{ts}",
            "answers": {q: {"choice": pick, "probabilities": probs}}, **kw}


def paper(**kw):
    return JevPaperConfig(fee_bps=0, spread_bps=0, thin_extra_bps=0, **kw)


def test_prompt_doc_matches_the_questions_as_sent():
    cfg = load_config()
    doc = (Path(__file__).parents[1] / "docs" / "jev_prompt.md").read_text()
    questions, side = agent.configured(cfg)
    assert f"`{agent.PROMPT_VERSION}`" in doc
    assert agent.mandate(side) in doc
    for q in questions.values():
        assert f"`{q.id}` ({q.type.capitalize()})" in doc
        assert q.instructions in doc
        for k, v in q.options.items():
            assert f"- `{k}`: {v}" in doc


def test_mandate_quotes_the_accounts_costs_and_sizes():
    cfg = load_config()
    questions, side = agent.configured(cfg)
    assert side == pytest.approx(0.0077)
    assert "about 0.8% of the amount traded" in agent.mandate(side) and "about 1.5%" in agent.mandate(side)
    assert "no stop-loss and no time limit" in agent.mandate(side)
    e = questions["entry"].options
    assert "about 5%" in e["buy_small"] and "about 10%" in e["buy_normal"] and "about 20%" in e["buy_large"]
    assert set(questions["manage"].options) == {"hold", "sell"}


def test_state_wraps_the_market_with_mandate_account_and_position():
    market = MarketState(json.dumps({"description": "d", "horizon_bars": 24, "current": {"ret_1": 0.1}}), {}, 10)
    view = agent.View({"cash_pct_of_equity": 80.0, "assets_held": 1}, {"hours_held": 3})
    st = json.loads(agent.agent_state(market, view, 0.0077).text)
    assert list(st) == ["mandate", "market", "account", "position"]
    assert "horizon_bars" not in st["market"] and st["market"]["current"] == {"ret_1": 0.1}
    assert st["position"] == {"hours_held": 3}


def test_buys_go_most_confident_first_and_are_cut_to_cash():
    p = paper(sizes={"small": 0.1, "normal": 0.3, "large": 0.6})
    acct = run([row("A", T, "entry", "buy_large", 0.6), row("B", T, "entry", "buy_large", 0.9),
                row("C", T, "entry", "buy_normal", 0.5)], p, H)
    held = {s: h.qty * h.entry_price for s, h in acct.holdings.items()}
    assert held["B"] == pytest.approx(6000) and held["A"] == pytest.approx(4000) and "C" not in held
    assert acct.cash == pytest.approx(0)
    actions = {a["symbol"]: a["action"] for a in acct.actions}
    assert actions == {"A": "buy", "B": "buy", "C": "no_cash"}


def test_sells_free_cash_for_the_same_hours_buys():
    p = paper(sizes={"small": 0.1, "normal": 0.5, "large": 1.0})
    rows = [row("A", T, "entry", "buy_large"),
            row("A", T + H, "manage", "sell", close=110.0), row("B", T + H, "entry", "buy_normal")]
    acct = run(rows, p, H)
    [t] = acct.trades
    assert t["pnl"] == pytest.approx(1000) and t["bars_held"] == 1
    # B is sized on the equity after A's sale: half of 11,000.
    assert acct.holdings["B"].qty * 100 == pytest.approx(5500)


def test_costs_and_cb_price_fills():
    p = JevPaperConfig(fee_bps=60, spread_bps=10, thin_extra_bps=0, sizes={"small": 0.1, "normal": 0.5, "large": 1.0})
    acct = run([row("A", T, "entry", "buy_small", cb_price=200.0, close=100.0)], p, H)
    h = acct.holdings["A"]
    assert h.entry_price == pytest.approx(200.0 * 1.001)       # the logged price plus the spread
    assert h.entry_fee == pytest.approx(1000 * 0.006)
    assert acct.cash == pytest.approx(10_000 - 1000 - 6)


def test_nothing_happens_on_error_abstain_or_a_mismatched_choice():
    p = paper()
    err = dict(row("A", T, "entry", "buy_large"), status="error")
    abst = dict(row("B", T, "entry", "buy_large"), status="abstain")
    held_sell = row("C", T, "entry", "wait")
    acct = run([err, abst, held_sell], p, H)
    assert not acct.holdings and acct.cash == 10_000
    assert {a["action"] for a in acct.actions} == {"none", "wait"}


def test_view_tracks_the_position_since_the_buy():
    p = paper()
    acct = Account.new(p, H)
    acct.step(T, [row("A", T, "entry", "buy_normal", close=100.0)])
    acct.mark([dict(row("A", T + H, "manage", "hold", close=105.0), high=112.0, low=95.0)])
    v = acct.view("A", T + H)
    assert v.position == {"hours_held": 1, "return_since_buy_pct": 5.0, "best_since_buy_pct": 12.0,
                          "worst_since_buy_pct": -5.0, "share_of_equity_pct": pytest.approx(10.5, abs=0.1)}
    assert v.account == {"cash_pct_of_equity": pytest.approx(89.5, abs=0.1), "assets_held": 1}
    assert acct.view("B", T + H).position is None


def test_replay_for_the_dashboard():
    cfg = load_config()
    out_ok = {"ret_pct": 5.0}
    rows = [dict(row("A", T, "entry", "buy_large", 0.8), outcome=out_ok), dict(row("B", T, "entry", "wait"), outcome=out_ok),
            row("A", T + 2 * H, "manage", "sell", close=90.0)]
    out = replay(rows, cfg, H)
    assert out["sizes"] == {"small": 0.05, "normal": 0.10, "large": 0.20} and out["prompt_version"] == "agent-1"
    assert out["counts"] == {"buy": 1, "wait": 1, "sell": 1}
    assert [t["symbol"] for t in out["trades"]] == ["A"] and out["trades"][0]["pnl"] < 0
    assert [h["bar_ts"] for h in out["hours"]] == [T + 2 * H, T + H, T]   # the skipped hour shows
    first = out["hours"][-1]
    assert first["bought"] == ["A"] and first["picks"][0]["choice"] == "buy_large" and first["picks"][0]["ret"] == 0.05
    assert out["hours"][0]["sold"][0]["symbol"] == "A"
