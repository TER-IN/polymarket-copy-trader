from datetime import datetime, timezone

from config import Settings
from db import Database
from ingestion import PollingIngestor
from models import TradeEvent, TradeSide


class FakeDataClient:
    def __init__(self, trades):
        self.trades = trades

    def recent_wallet_trades(self, wallet: str):
        return self.trades

    def recent_wallet_redemptions(self, wallet: str):
        return []


class FakeDecisionEngine:
    def __init__(self):
        self.calls = 0

    def decide(self, trade, crowding_score=None):
        self.calls += 1
        raise AssertionError("seeded trades should not be decision-processed")


class FakeExecutor:
    def execute(self, trade, decision):
        raise AssertionError("seeded trades should not be executed")


def make_trade() -> TradeEvent:
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
        side=TradeSide.BUY,
        price=0.50,
        size=100,
        notional_usd=50,
        raw_payload={},
    )


def test_first_poll_seeds_existing_trades_without_processing(tmp_path) -> None:
    settings = Settings(target_wallets=["0xabc"], seed_existing_trades_on_startup=True)
    db = Database(tmp_path / "db.sqlite3")
    decision_engine = FakeDecisionEngine()
    ingestor = PollingIngestor(
        settings,
        db,
        FakeDataClient([make_trade()]),
        decision_engine,
        FakeExecutor(),
    )

    ingestor.poll_once()

    assert len(db.recent_trades()) == 1
    assert decision_engine.calls == 0
