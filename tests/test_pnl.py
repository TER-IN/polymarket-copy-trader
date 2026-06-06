from polymarket_pnl import PnlPoint, daily_candles


def test_daily_candles_use_previous_close_as_open() -> None:
    candles = daily_candles(
        [
            PnlPoint(timestamp=1_700_000_000, pnl=10),
            PnlPoint(timestamp=1_700_086_400, pnl=7),
            PnlPoint(timestamp=1_700_172_800, pnl=12),
        ]
    )

    assert candles[0]["open"] == 0
    assert candles[0]["close"] == 10
    assert candles[0]["daily_pnl"] == 10
    assert candles[1]["open"] == 10
    assert candles[1]["close"] == 7
    assert candles[1]["daily_pnl"] == -3
    assert candles[1]["high"] == 10
    assert candles[1]["low"] == 7
    assert candles[2]["open"] == 7
    assert candles[2]["close"] == 12
    assert candles[2]["daily_pnl"] == 5
