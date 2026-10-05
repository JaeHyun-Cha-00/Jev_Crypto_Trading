from jevtrade.data.gaps import detect_gaps
from jevtrade.data.store import CandleStore, connect
from jevtrade.data.sync import sync_symbol
from jevtrade.data.timeframes import iso_to_ms, last_closed_open_ms, timeframe_ms

from conftest import H, T0, FakeSource, synthetic_candles

EX, SYM, TF = "fake", "BTC/USDT", "1h"


def _store():
    return CandleStore(connect(":memory:"))


def test_timeframe_and_iso():
    assert timeframe_ms("1h") == H
    assert iso_to_ms("2024-01-01T00:00:00Z") == T0
    # at 10:30 the last closed 1h candle opened at 09:00
    assert last_closed_open_ms(T0 + 10 * H + H // 2, H) == T0 + 9 * H


def test_sync_excludes_forming_candle():
    src = FakeSource(synthetic_candles(100))
    store = _store()
    now = T0 + 50 * H + 10  # candle 50 is still forming
    r = sync_symbol(src, store, EX, SYM, TF, H, T0, now_ms=now, page_limit=7)
    assert r.inserted == 50
    assert r.last_ts == T0 + 49 * H
    assert r.gaps == []


def test_sync_is_incremental():
    src = FakeSource(synthetic_candles(100))
    store = _store()
    sync_symbol(src, store, EX, SYM, TF, H, T0, now_ms=T0 + 40 * H, page_limit=10)
    r = sync_symbol(src, store, EX, SYM, TF, H, T0, now_ms=T0 + 60 * H, page_limit=10)
    assert r.inserted == 20
    assert store.timestamps(EX, SYM, TF) == [T0 + i * H for i in range(60)]
    # nothing new: no fetch needed
    src.calls = 0
    r = sync_symbol(src, store, EX, SYM, TF, H, T0, now_ms=T0 + 60 * H, page_limit=10)
    assert r.inserted == 0 and src.calls == 0


def test_gap_backfilled_when_exchange_has_data():
    all_c = synthetic_candles(30)
    store = _store()
    # simulate a previous partial sync that skipped candles 10..12
    store.upsert(EX, SYM, TF, [c for i, c in enumerate(all_c) if not 10 <= i <= 12])
    r = sync_symbol(FakeSource(all_c), store, EX, SYM, TF, H, T0, now_ms=T0 + 30 * H)
    assert r.gaps == []
    assert len(store.timestamps(EX, SYM, TF)) == 30


def test_unresolvable_gap_is_reported_not_filled():
    all_c = synthetic_candles(30)
    missing = {T0 + 10 * H, T0 + 11 * H}
    r = sync_symbol(FakeSource(all_c, missing), _store(), EX, SYM, TF, H, T0, now_ms=T0 + 30 * H)
    assert len(r.gaps) == 1
    assert r.gaps[0].after_ts == T0 + 9 * H and r.gaps[0].missing == 2


def test_detect_gaps():
    ts = [0, H, 2 * H, 5 * H, 6 * H]
    (g,) = detect_gaps(ts, H)
    assert (g.after_ts, g.before_ts, g.missing) == (2 * H, 5 * H, 2)


def test_load_returns_utc_index():
    store = _store()
    store.upsert(EX, SYM, TF, synthetic_candles(5))
    df = store.load(EX, SYM, TF)
    assert str(df.index.tz) == "UTC"
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index[0].value // 1_000_000 == T0


def test_sync_with_capped_history_source():
    """Kraken-style source: only the latest N candles are served regardless of `since`."""
    src = FakeSource(synthetic_candles(1000), max_history=720)
    store = _store()
    r = sync_symbol(src, store, EX, SYM, TF, H, T0, now_ms=T0 + 1000 * H, page_limit=720)
    ts = store.timestamps(EX, SYM, TF)
    assert len(ts) == 720 and ts[0] == T0 + 280 * H and ts[-1] == T0 + 999 * H
    assert r.gaps == []
    assert src.calls <= 3


def test_unresolvable_gap_is_fetched_once_then_left_alone():
    all_c = synthetic_candles(40)
    src = FakeSource(all_c, {T0 + 10 * H, T0 + 11 * H})
    store = _store()
    sync_symbol(src, store, EX, SYM, TF, H, T0, now_ms=T0 + 30 * H)
    src.calls = 0
    r = sync_symbol(src, store, EX, SYM, TF, H, T0, now_ms=T0 + 30 * H)
    assert src.calls == 0                       # nothing new, and the known gap is not re-fetched
    assert len(r.gaps) == 1                     # still reported
    # A new gap later on is still tried once.
    src.missing.add(T0 + 31 * H)
    src.calls = 0
    r = sync_symbol(src, store, EX, SYM, TF, H, T0, now_ms=T0 + 35 * H)
    assert src.calls == 2 and len(r.gaps) == 2  # incremental fetch + one backfill of the new gap
