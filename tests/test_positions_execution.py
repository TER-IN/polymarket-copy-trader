from config import Settings
from db import Database
from execution import Executor
from models import CopyDecision, TradeSide
from datetime import datetime, timezone

from models import TradeEvent


def make_trade(side: TradeSide = TradeSide.BUY) -> TradeEvent:
    return TradeEvent(
        source_wallet="0xabc",
        transaction_hash="0xtx",
        timestamp=datetime.now(timezone.utc),
        market_id="m1",
        condition_id="c1",
        asset_id="tok1",
        token_id="tok1",
        market_title="Market",
        outcome="Yes",
        side=side,
        price=0.50,
        size=100,
        notional_usd=50,
        raw_payload={},
    )


def test_position_update_buy_and_sell(tmp_path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    from positions import apply_buy, apply_sell

    position = apply_buy(None, "m1", "tok1", "Yes", 10, 0.5, "0xabc")
    db.upsert_position(position)
    position = db.get_position("m1", "tok1", "Yes")
    assert position is not None
    position = apply_sell(position, 4, 0.6)

    assert position.total_shares == 6
    assert round(position.realized_pnl, 2) == 0.4


def test_dry_run_execution_records_order_and_position(tmp_path) -> None:
    settings = Settings(max_trade_usd=10)
    db = Database(tmp_path / "db.sqlite3")
    executor = Executor(settings, db)
    trade = make_trade()
    decision = CopyDecision(True, "ok", copy_notional_usd=10, current_price=0.5, allowed_price=0.52)

    executor.execute(trade, decision)

    position = db.get_position("m1", "tok1", "Yes")
    assert position is not None
    assert position.total_shares == 20
    assert len(db.positions()) == 1
