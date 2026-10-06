from jevtrade.data.timeframes import last_closed_open_ms, timeframe_ms

from conftest import H, T0


def test_timeframe_and_iso():
    assert timeframe_ms("1h") == H
    # at 10:30 the last closed 1h candle opened at 09:00
    assert last_closed_open_ms(T0 + 10 * H + H // 2, H) == T0 + 9 * H
