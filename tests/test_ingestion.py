from datetime import datetime, timezone

import pytest

from config import Settings
from db import Database
from decision_engine import DecisionEngine
from execution import Executor
from ingestion import PollingIngestor
from models import OutcomeSelectionMode, TradeEvent, TradeSide
from outcome_selection import OutcomeSelector
from polymarket_clob import BookQuote
from polymarket_gamma import OutcomeToken


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


class FakeClob:
    def get_quote(self, token_id: str) -> BookQuote:
        return BookQuote(token_id=token_id, best_bid=0.4, best_ask=0.4, raw={})


class FakeUpDownProvider:
    def get_up_down_tokens(self, market_id: str, asset_id: str, condition_id: str | None = None):
        return OutcomeToken("up", "Up"), OutcomeToken("down", "Down")


class MissingPairProvider:
    def get_up_down_tokens(self, market_id: str, asset_id: str, condition_id: str | None = None):
        return None


def test_inverse_mode_opens_and_closes_opposite_position(tmp_path) -> None:
    settings = Settings(
        outcome_selection_mode=OutcomeSelectionMode.INVERSE_UP_DOWN,
        copy_ratio=1,
        max_trade_usd=100,
        max_trade_age_seconds=60,
        daily_spend_cap_usd=1000,
        per_market_exposure_cap_usd=1000,
        enable_crowding_check=False,
        max_buy_price=None,
        max_seconds_until_market_end=None,
        dry_run_starting_balance_usd=None,
    )
    db = Database(tmp_path / "db.sqlite3")
    ingestor = PollingIngestor(
        settings,
        db,
        FakeDataClient([]),
        DecisionEngine(settings, db, FakeClob()),
        Executor(settings, db),
        outcome_selector=OutcomeSelector(settings.outcome_selection_mode, FakeUpDownProvider()),
    )
    buy = TradeEvent(
        source_wallet="0xabc",
        transaction_hash="0xbuy",
        timestamp=datetime.now(timezone.utc),
        market_id="condition",
        condition_id="condition",
        asset_id="down",
        token_id="down",
        market_title="Ethereum Up or Down",
        outcome="Down",
        side=TradeSide.BUY,
        price=0.6,
        size=100,
        notional_usd=60,
        raw_payload={"eventSlug": "eth-updown-test"},
    )

    assert ingestor.process_trade(buy)
    position = db.get_position("condition", "up", "Up")
    assert position is not None
    assert position.total_shares == 150
    source_state = db.get_source_token_state_for_trade(buy)
    assert source_state is not None
    assert source_state["observed_source_shares"] == 100
    order = db.copied_order_rows(1)[0]
    assert order["source_outcome"] == "Down"
    assert order["copied_outcome"] == "Up"
    assert order["token_id"] == "up"
    assert order["reference_price"] == pytest.approx(0.4)
    dashboard_position = db.position_dashboard_rows()[0]
    assert dashboard_position["market_title"] == "Ethereum Up or Down"
    assert dashboard_position["event_slug"] == "eth-updown-test"

    sell = TradeEvent(
        **{
            **buy.__dict__,
            "transaction_hash": "0xsell",
            "side": TradeSide.SELL,
        }
    )
    assert ingestor.process_trade(sell)

    position = db.get_position("condition", "up", "Up")
    assert position is not None
    assert position.total_shares == 0
    assert position.status.value == "closed"


def test_inverse_mode_records_strict_pair_rejection(tmp_path) -> None:
    settings = Settings(
        outcome_selection_mode=OutcomeSelectionMode.INVERSE_UP_DOWN,
        enable_crowding_check=False,
    )
    db = Database(tmp_path / "db.sqlite3")
    ingestor = PollingIngestor(
        settings,
        db,
        FakeDataClient([]),
        DecisionEngine(settings, db, FakeClob()),
        Executor(settings, db),
        outcome_selector=OutcomeSelector(settings.outcome_selection_mode, MissingPairProvider()),
    )
    trade = make_trade()

    assert ingestor.process_trade(trade)

    state = db.get_source_token_state_for_trade(trade)
    assert state is not None
    assert state["status"] == "frozen"
    assert "authoritative two-outcome Up/Down pair" in state["freeze_reason"]
    decision = db.source_token_states()[0]
    assert '"outcome_selection_mode": "inverse_up_down"' in decision["decision_details"]
