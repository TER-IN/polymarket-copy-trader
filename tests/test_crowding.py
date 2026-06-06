from datetime import datetime, timedelta, timezone

from crowding import calculate_crowding_score
from models import TradeEvent, TradeSide


def trade(wallet: str, seconds: int, price: float, side: TradeSide = TradeSide.BUY) -> TradeEvent:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return TradeEvent(
        source_wallet=wallet,
        transaction_hash=f"tx-{wallet}-{seconds}",
        timestamp=base + timedelta(seconds=seconds),
        market_id="m1",
        condition_id="c1",
        asset_id="tok1",
        token_id="tok1",
        market_title="Market",
        outcome="Yes",
        side=side,
        price=price,
        size=10,
        notional_usd=price * 10,
        raw_payload={},
    )


def test_crowding_score_calculation() -> None:
    target = trade("0xtarget", 0, 0.50)
    nearby = [
        target,
        trade("0xf1", 5, 0.51),
        trade("0xf2", 10, 0.52),
        trade("0xf1", 15, 0.53),
        trade("0xbetter", 20, 0.49),
    ]

    score = calculate_crowding_score(target, nearby)

    assert score.follower_count == 2
    assert score.median_delay_seconds == 10
    assert "0xf1" in score.repeat_follower_wallets
