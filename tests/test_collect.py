"""Forward collection: one Jev call per closed candle, backfill, outcomes.

Jev is replayed from the recorded fixture; nothing touches the network.
"""

import json
from pathlib import Path

import pytest

from jevtrade.collect.collector import Collector, outcome_record, candles_frame
from jevtrade.config import load_config
from jevtrade.decision.base import JevConfig
from jevtrade.decision.jev import HttpResponse, JevError, JevModel

from conftest import H, T0, WindowedSource, synthetic_candles

FIXTURE = Path(__file__).parent / "fixtures" / "jev" / "btc_live" / "response.json"
N = 1500  # candles of history per symbol
OK = HttpResponse(200, FIXTURE.read_text().strip())


class Transport:
    """Replays the recorded response, or a scripted sequence, and counts calls."""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls = 0

    def __call__(self, url, payload, headers, timeout_s):
        self.calls += 1
        r = self.script.pop(0) if self.script else OK
        if isinstance(r, Exception):
            raise r
        return r


class MultiSource(WindowedSource):
    """Different synthetic series per symbol, Coinbase-style windows."""

    def __init__(self, n=N):
        super().__init__([])
        self.series = {"BTC/USD": synthetic_candles(n, seed=1), "ETH/USD": synthetic_candles(n, seed=2)}

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.candles = self.series[symbol]
        return super().fetch_ohlcv(symbol, timeframe, since, limit)


def _collector(tmp_path, transport, source=None, max_backfill=24, symbols=("BTC/USD", "ETH/USD"), start=None,
               prices=None):
    cfg = load_config()
    cfg.data.symbols = list(symbols)
    cfg.forward_log.start = start   # the default fresh-start date is after the synthetic candles
    model = JevModel(JevConfig(max_retries=0), transport=transport, sleep=lambda s: None)
    return Collector(cfg, model, source or MultiSource(), tmp_path, max_backfill=max_backfill, prices=prices)


def _now(last_closed_index: int) -> int:
    # A few minutes after candle `last_closed_index` closed.
    return T0 + (last_closed_index + 1) * H + 7 * 60_000


def _decisions(tmp_path):
    out = []
    for p in sorted((tmp_path / "decisions").glob("*.jsonl")):
        out += [json.loads(line) for line in p.read_text().splitlines()]
    return out


def test_first_run_backfills_24_candles_per_symbol(tmp_path):
    t = Transport()
    res = _collector(tmp_path, t).run(_now(1200))
    assert t.calls == 48 and len(res.called) == 48
    recs = _decisions(tmp_path)
    assert {r["symbol"] for r in recs} == {"BTC/USD", "ETH/USD"}
    btc = sorted(r["candle_ts"] for r in recs if r["symbol"] == "BTC/USD")
    assert btc == [T0 + i * H for i in range(1177, 1201)]
    r = recs[0]
    assert r["status"] == "answered" and r["served_model"] == "typesafe/jev-1.13-20260917"
    assert set(r["answers"]) == {"direction", "regime", "adverse_move", "clear_signal"}
    assert r["answers"]["adverse_move"]["noul"] == 0.32
    assert "state" not in r and r["raw_response"] is None and r["cost_usd"] > 0
    assert (tmp_path / "README.md").exists()
    # The candle itself rides along, for intrabar stops and volume-based costs.
    c = MultiSource().series["BTC/USD"][1177]
    first = min((r for r in recs if r["symbol"] == "BTC/USD"), key=lambda r: r["candle_ts"])
    assert [first[k] for k in ("open", "high", "low", "close", "volume")] == pytest.approx(c[1:6])
    assert first["low"] <= min(first["open"], first["close"]) <= max(first["open"], first["close"]) <= first["high"]


def test_fresh_start_cutoff_limits_the_backfill(tmp_path):
    from datetime import datetime, timezone

    start = T0 + 1195 * H
    t = Transport()
    c = _collector(tmp_path, t, start=datetime.fromtimestamp(start / 1000, tz=timezone.utc))
    c.run(_now(1200))
    btc = sorted(r["candle_ts"] for r in _decisions(tmp_path) if r["symbol"] == "BTC/USD")
    assert btc == [T0 + i * H for i in range(1195, 1201)] and t.calls == 12
    # Before the start hour nothing is asked at all.
    t2 = Transport()
    _collector(tmp_path / "early", t2, start=datetime.fromtimestamp((T0 + 1300 * H) / 1000, tz=timezone.utc)).run(_now(1200))
    assert t2.calls == 0 and _decisions(tmp_path / "early") == []


def test_state_text_goes_to_a_gzip_file_beside_the_decisions(tmp_path):
    c = _collector(tmp_path, Transport())
    c.run(_now(1200))
    recs = _decisions(tmp_path)
    states = [s for p in sorted((tmp_path / "state").glob("*.jsonl.gz")) for s in c.states.read(p.name[:10])]
    assert {(s["symbol"], s["candle_ts"], s["input_hash"]) for s in states} == \
        {(r["symbol"], r["candle_ts"], r["input_hash"]) for r in recs}
    assert all(len(s["state"]) > 500 for s in states)
    # Decision lines stay small without the state text.
    assert max(len(json.dumps(r)) for r in recs) < 2000
    # The next hour appends a gzip member; the earlier ones still read.
    before = len(states)
    _collector(tmp_path, Transport()).run(_now(1201))
    after = [s for p in sorted((tmp_path / "state").glob("*.jsonl.gz")) for s in c.states.read(p.name[:10])]
    assert len(after) == before + 2 and after[:before] == states


def test_old_decision_lines_with_inline_state_still_count_as_done(tmp_path):
    _collector(tmp_path, Transport()).run(_now(1200))
    for p in (tmp_path / "decisions").glob("*.jsonl"):   # rewrite as the pre-split format
        lines = [dict(json.loads(line), state="old inline state") for line in p.read_text().splitlines()]
        p.write_text("".join(json.dumps(r) + "\n" for r in lines))
    t = Transport()
    _collector(tmp_path, t).run(_now(1200) + 20 * 60_000)
    assert t.calls == 0


def test_rerun_in_the_same_hour_calls_nothing(tmp_path):
    _collector(tmp_path, Transport()).run(_now(1200))
    t = Transport()
    res = _collector(tmp_path, t).run(_now(1200) + 20 * 60_000)
    assert t.calls == 0 and res.called == []
    assert len(_decisions(tmp_path)) == 48


def test_next_hour_calls_once_per_symbol(tmp_path):
    _collector(tmp_path, Transport()).run(_now(1200))
    t = Transport()
    res = _collector(tmp_path, t).run(_now(1201))
    assert t.calls == 2
    assert sorted(res.called) == [("BTC/USD", T0 + 1201 * H), ("ETH/USD", T0 + 1201 * H)]


def test_skipped_runs_are_backfilled_up_to_the_limit(tmp_path):
    _collector(tmp_path, Transport()).run(_now(1200))
    t = Transport()
    _collector(tmp_path, t).run(_now(1205))  # five runs skipped
    assert t.calls == 10
    t = Transport()
    _collector(tmp_path, t).run(_now(1260))  # down for 55 hours: only the latest 24
    assert t.calls == 48
    keys = [(r["symbol"], r["candle_ts"]) for r in _decisions(tmp_path)]
    assert len(keys) == len(set(keys))


def test_one_symbols_fetch_error_does_not_stop_the_others(tmp_path):
    # A coin the source can't serve (delisted, exchange error) is skipped for
    # this pass; the coins after it are still asked about.
    t = Transport()
    res = _collector(tmp_path, t, max_backfill=1,
                     symbols=("BTC/USD", "GONE/USD", "ETH/USD")).run(_now(1200))
    assert res.failed_symbols == ["GONE/USD"]
    assert sorted(s for s, _ in res.called) == ["BTC/USD", "ETH/USD"]
    assert t.calls == 2


def test_candle_not_yet_published_is_picked_up_next_run(tmp_path):
    src = MultiSource()
    late = T0 + 1200 * H
    src.missing = {late}
    _collector(tmp_path, Transport(), src).run(_now(1200))
    assert late not in {r["candle_ts"] for r in _decisions(tmp_path)}
    src.missing = set()
    t = Transport()
    _collector(tmp_path, t, src).run(_now(1201))
    assert t.calls == 4  # the late candle and the new one, for both symbols


def test_failed_request_is_retried_but_abstain_is_not(tmp_path):
    t = Transport([HttpResponse(503, "busy"),
                   HttpResponse(200, json.dumps({"model": "typesafe/other", "answers": {}}))])
    _collector(tmp_path, t, max_backfill=1).run(_now(1200))
    by_sym = {r["symbol"]: r for r in _decisions(tmp_path)}
    assert by_sym["BTC/USD"]["status"] == "error" and by_sym["BTC/USD"]["raw_response"] == "busy"
    assert by_sym["ETH/USD"]["status"] == "abstain"

    t = Transport()
    res = _collector(tmp_path, t, max_backfill=1).run(_now(1200))
    assert res.called == [("BTC/USD", T0 + 1200 * H)]
    t = Transport()
    _collector(tmp_path, t, max_backfill=1).run(_now(1200))
    assert t.calls == 0


def test_fatal_error_stops_but_keeps_earlier_answers(tmp_path):
    t = Transport([OK, OK, HttpResponse(401, "no key")])
    with pytest.raises(JevError):
        _collector(tmp_path, t, max_backfill=3).run(_now(1200))
    assert len(_decisions(tmp_path)) == 2
    t = Transport()
    _collector(tmp_path, t, max_backfill=3).run(_now(1200))
    assert t.calls == 4


def test_records_split_by_candle_day(tmp_path):
    # Candle 1200 opens 2024-02-20T00:00Z, so the 24-candle backfill spans two days.
    _collector(tmp_path, Transport()).run(_now(1200))
    files = sorted(p.name for p in (tmp_path / "decisions").glob("*.jsonl"))
    assert files == ["2024-02-19.jsonl", "2024-02-20.jsonl"]
    for p in (tmp_path / "decisions").glob("*.jsonl"):
        day = p.stem
        assert all(json.loads(line)["candle_open"].startswith(day)
                   for line in p.read_text().splitlines())


def test_outcomes_logged_once_after_horizon(tmp_path):
    h = load_config().decision.horizon_bars
    _collector(tmp_path, Transport(), max_backfill=1).run(_now(1200))
    assert not (tmp_path / "outcomes").exists() or not list((tmp_path / "outcomes").glob("*"))
    for k in range(1201, 1202 + h):
        _collector(tmp_path, Transport(), max_backfill=1).run(_now(k))
    outs = [json.loads(line) for p in (tmp_path / "outcomes").glob("*.jsonl")
            for line in p.read_text().splitlines()]
    keys = [(o["symbol"], o["candle_ts"]) for o in outs]
    assert len(keys) == len(set(keys))
    # Decisions at 1200 and 1201 have closed their h-bar horizon by candle 1201 + h.
    assert {ts for _, ts in keys} == {T0 + 1200 * H, T0 + 1201 * H}
    assert {o["horizon_bars"] for o in outs} == {h}


def test_outcome_labels():
    rows = [[T0 + i * H, 100, 100, 100, 100, 1] for i in range(5)]
    rows[2][3] = 96.5          # dips 3.5% below the decision close
    rows[4][4] = 101.5         # closes +1.5% at the horizon
    df = candles_frame(rows)
    dec = {"symbol": "BTC/USD", "candle_ts": T0, "candle_open": "x", "close": 100.0}
    o = outcome_record(dec, df, H, 4, 1.0, 3.0)
    assert o["direction"] == "up" and o["ret_pct"] == pytest.approx(1.5)
    assert o["adverse_move"] is True and o["min_low_pct"] == pytest.approx(-3.5)
    assert outcome_record(dec, df.iloc[:4], H, 4, 1.0, 3.0) is None


def test_decision_cli_state_matches_the_collector(tmp_path):
    """`python -m jevtrade.decision` must show Jev the same state the hourly log does."""
    from jevtrade.collect.collector import StateLog
    from jevtrade.decision.__main__ import _pretty, latest_state

    c = _collector(tmp_path, Transport(), max_backfill=1, symbols=("BTC/USD",))
    now = _now(N - 1)
    c.run(now)
    [logged] = StateLog(tmp_path / "state").read("2024-03-03")
    candles, state = latest_state(c.cfg, MultiSource(), "BTC/USD", now)
    assert int(candles.index[-1].value // 1_000_000) == logged["candle_ts"]
    assert state.text == logged["state"]
    assert _pretty("<html>502</html>") == "<html>502</html>"   # error pages print as-is


def test_each_run_writes_one_gzip_member(tmp_path):
    import zlib
    _collector(tmp_path, Transport(), max_backfill=1).run(_now(1200))   # one row per symbol
    [p] = (tmp_path / "state").glob("*.jsonl.gz")
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    assert len(d.decompress(p.read_bytes()).splitlines()) == 2
    assert d.eof and d.unused_data == b""   # nothing after the first member


def test_coinbase_price_goes_on_the_newest_candle_only(tmp_path):
    asked = []

    def prices(symbols):
        asked.append(symbols)
        return {"BTC/USD": 101.5}

    _collector(tmp_path, Transport(), max_backfill=3, prices=prices).run(_now(N - 2))
    assert asked == [["BTC/USD", "ETH/USD"]]   # one price call per run
    recs = _decisions(tmp_path)
    priced = [r for r in recs if "cb_price" in r]
    newest = max(r["candle_ts"] for r in recs)
    assert [(r["symbol"], r["candle_ts"]) for r in priced] == [("BTC/USD", newest)]
    assert priced[0]["cb_price"] == 101.5 and priced[0]["cb_price_at"].endswith("Z")


def test_coinbase_outage_never_stops_a_run(tmp_path):
    def prices(symbols):
        raise OSError("coinbase down")

    res = _collector(tmp_path, Transport(), max_backfill=2, prices=prices).run(_now(N - 2))
    assert len(res.called) == 4 and not any("cb_price" in r for r in _decisions(tmp_path))


def test_coinbase_prices_reads_every_symbol_in_one_call():
    from jevtrade.data.market import coinbase_prices
    calls = []

    def get(url, headers, timeout_s):
        calls.append(url)
        return json.dumps({"BTC-USD": {"stats_24hour": {"last": "85000.5"}},
                           "ETH-USD": {"stats_24hour": {"last": "0"}}}).encode()

    assert coinbase_prices(["BTC/USD", "ETH/USD", "SOL/USD"], http_get=get) == {"BTC/USD": 85000.5}
    assert calls == ["https://api.exchange.coinbase.com/products/stats"]

