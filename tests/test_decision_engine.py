from dataclasses import replace
from datetime import datetime, timedelta, timezone

from config import Settings
from db import Database
from decision_engine import calculate_copy_size, passes_slippage, DecisionEngine
from models import MarketTypeFilter, TradeEvent, TradeSide
from polymarket_clob import BookQuote
from polymarket_gamma import MarketMetadata, OutcomeToken
from positions import apply_buy


class FakeClob:
    def __init__(self, ask: float | None = 0.51, bid: float | None = 0.49):
        self.ask = ask
        self.bid = bid

    def get_quote(self, token_id: str) -> BookQuote:
        return BookQuote(token_id=token_id, best_bid=self.bid, best_ask=self.ask, raw={})


class FakeBalance:
    def __init__(self, balance: float):
        self.balance = balance

    def available_balance_usd(self) -> float:
        return self.balance


class FakeMarketEnd:
    def __init__(self, end_time: datetime | None):
        self.end_time = end_time

    def get_market_end_time(self, market_id: str, asset_id: str, condition_id: str | None = None):
        return self.end_time


class FakeMarketMetadata(FakeMarketEnd):
    def __init__(self, duration_seconds: int):
        self.start_time = datetime.now(timezone.utc)
        self.metadata = MarketMetadata(
            start_time=self.start_time,
            end_time=self.start_time + timedelta(seconds=duration_seconds),
            up_down_tokens=(OutcomeToken("up", "Up"), OutcomeToken("down", "Down")),
            fee_rate=0,
            fee_exponent=1,
            taker_only_fee=True,
        )
        super().__init__(self.metadata.end_time)

    def get_market_metadata(self, market_id: str, asset_id: str, condition_id: str | None = None):
        return self.metadata


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


def test_copy_size_calculation() -> None:
    assert calculate_copy_size(100, 0.25, 10) == 10
    assert calculate_copy_size(100, 0.25, 100) == 25


def test_slippage_checks() -> None:
    assert passes_slippage(TradeSide.BUY, 0.50, 0.52, 2)
    assert not passes_slippage(TradeSide.BUY, 0.50, 0.53, 2)
    assert passes_slippage(TradeSide.SELL, 0.50, 0.48, 2)
    assert not passes_slippage(TradeSide.SELL, 0.50, 0.47, 2)


def test_decision_allows_buy(tmp_path) -> None:
    settings = Settings(
        target_wallets=["0xabc"],
        max_trade_usd=10,
        min_trade_usd=1,
        max_trade_age_seconds=60,
        daily_spend_cap_usd=100,
        per_market_exposure_cap_usd=100,
        max_buy_price=None,
        max_seconds_until_market_end=None,
    )
    db = Database(tmp_path / "db.sqlite3")
    engine = DecisionEngine(settings, db, FakeClob())

    decision = engine.decide(make_trade())

    assert decision.should_copy
    assert decision.copy_notional_usd == 10
    assert decision.copy_shares == 10 / 0.51


def test_market_title_allowlist_matches_buy_case_insensitively(tmp_path) -> None:
    settings = Settings(
        allow_market_title_keywords=["bitcoin"],
        max_seconds_until_market_end=None,
        max_trade_age_seconds=60,
    )
    db = Database(tmp_path / "db.sqlite3")
    engine = DecisionEngine(settings, db, FakeClob())
    trade = replace(make_trade(), market_title="BITCOIN Up or Down")

    decision = engine.decide(trade)

    assert decision.should_copy
    assert decision.details["strategy_config"]["allow_market_title_keywords"] == ["bitcoin"]


def test_market_title_allowlist_rejects_nonmatching_buy(tmp_path) -> None:
    settings = Settings(
        allow_market_title_keywords=["bitcoin", "ethereum"],
        max_seconds_until_market_end=None,
        max_trade_age_seconds=60,
    )
    db = Database(tmp_path / "db.sqlite3")
    engine = DecisionEngine(settings, db, FakeClob())

    decision = engine.decide(make_trade())

    assert not decision.should_copy
    assert decision.reason == (
        "market title does not match allowed keywords: bitcoin, ethereum"
    )
    assert decision.details["strategy_config"]["allow_market_title_keywords"] == [
        "bitcoin",
        "ethereum",
    ]


def test_market_title_allowlist_does_not_block_sell(tmp_path) -> None:
    settings = Settings(
        allow_market_title_keywords=["bitcoin"],
        max_trade_usd=100,
        max_trade_age_seconds=60,
    )
    db = Database(tmp_path / "db.sqlite3")
    position = apply_buy(None, "m1", "tok1", "Yes", 20, 0.5, "0xabc")
    db.upsert_position(position)
    db.record_copied_source_trade(make_trade(TradeSide.BUY))
    engine = DecisionEngine(settings, db, FakeClob(bid=0.49))

    decision = engine.decide(make_trade(TradeSide.SELL))

    assert decision.should_copy


def test_decision_refuses_sell_without_position(tmp_path) -> None:
    settings = Settings(max_trade_usd=10, max_trade_age_seconds=60)
    db = Database(tmp_path / "db.sqlite3")
    engine = DecisionEngine(settings, db, FakeClob())

    decision = engine.decide(make_trade(TradeSide.SELL))

    assert not decision.should_copy
    assert "lifecycle not tracked" in decision.reason


def test_decision_finds_position_by_condition_id_when_market_id_missing(tmp_path) -> None:
    settings = Settings(max_trade_usd=10, max_trade_age_seconds=60)
    db = Database(tmp_path / "db.sqlite3")
    position = apply_buy(None, "c1", "tok1", "Yes", 5, 0.5, "0xabc")
    db.upsert_position(position)
    source_buy = make_trade(TradeSide.BUY)
    db.record_copied_source_trade(source_buy)
    engine = DecisionEngine(settings, db, FakeClob(bid=0.49))
    sell = make_trade(TradeSide.SELL)
    sell = TradeEvent(
        source_wallet=sell.source_wallet,
        transaction_hash=sell.transaction_hash,
        timestamp=sell.timestamp,
        market_id=None,
        condition_id="c1",
        asset_id=sell.asset_id,
        token_id=sell.token_id,
        market_title=sell.market_title,
        outcome=sell.outcome,
        side=sell.side,
        price=sell.price,
        size=sell.size,
        notional_usd=sell.notional_usd,
        raw_payload=sell.raw_payload,
    )

    decision = engine.decide(sell)

    assert decision.should_copy
    assert decision.copy_shares == 5


def test_sell_copy_is_capped_by_local_position(tmp_path) -> None:
    settings = Settings(copy_ratio=1, max_trade_usd=100, max_trade_age_seconds=60)
    db = Database(tmp_path / "db.sqlite3")
    position = apply_buy(None, "m1", "tok1", "Yes", 20, 0.5, "0xabc")
    db.upsert_position(position)
    source_buy = make_trade(TradeSide.BUY)
    db.record_copied_source_trade(source_buy)
    engine = DecisionEngine(settings, db, FakeClob(bid=0.49))
    sell = make_trade(TradeSide.SELL)
    sell = TradeEvent(
        source_wallet=sell.source_wallet,
        transaction_hash=sell.transaction_hash,
        timestamp=sell.timestamp,
        market_id=sell.market_id,
        condition_id=sell.condition_id,
        asset_id=sell.asset_id,
        token_id=sell.token_id,
        market_title=sell.market_title,
        outcome=sell.outcome,
        side=sell.side,
        price=sell.price,
        size=25,
        notional_usd=12.5,
        raw_payload=sell.raw_payload,
    )

    decision = engine.decide(sell)

    assert decision.should_copy
    assert decision.copy_shares == 5
    assert decision.copy_notional_usd == 5 * 0.49


def test_sell_copy_sells_all_when_source_sells_more_than_observed(tmp_path) -> None:
    settings = Settings(copy_ratio=1, max_trade_usd=100, max_trade_age_seconds=60)
    db = Database(tmp_path / "db.sqlite3")
    position = apply_buy(None, "m1", "tok1", "Yes", 20, 0.5, "0xabc")
    db.upsert_position(position)
    source_buy = make_trade(TradeSide.BUY)
    db.record_copied_source_trade(source_buy)
    engine = DecisionEngine(settings, db, FakeClob(bid=0.49))
    sell = make_trade(TradeSide.SELL)

    decision = engine.decide(sell)

    assert decision.should_copy
    assert decision.copy_shares == 20


def test_decision_skips_preexisting_source_position(tmp_path) -> None:
    from models import SourcePositionSnapshot

    settings = Settings(max_trade_usd=10, max_trade_age_seconds=60)
    db = Database(tmp_path / "db.sqlite3")
    db.insert_preexisting_source_position(
        SourcePositionSnapshot(
            source_wallet="0xabc",
            market_id="m1",
            asset_id="tok1",
            outcome="Yes",
            size=10,
            avg_price=0.4,
            raw_payload={},
        )
    )
    engine = DecisionEngine(settings, db, FakeClob())

    decision = engine.decide(make_trade())

    assert not decision.should_copy
    assert "before bot startup" in decision.reason


def test_buy_is_capped_by_remaining_risk_limits(tmp_path) -> None:
    settings = Settings(
        copy_ratio=1,
        max_trade_usd=25,
        max_trade_age_seconds=60,
        daily_spend_cap_usd=100,
        per_market_exposure_cap_usd=10,
        max_buy_price=None,
        max_seconds_until_market_end=None,
    )
    db = Database(tmp_path / "db.sqlite3")
    engine = DecisionEngine(settings, db, FakeClob(ask=0.5))

    decision = engine.decide(make_trade())

    assert decision.should_copy
    assert decision.copy_notional_usd == 10
    assert decision.copy_shares == 20
    assert "capped" in decision.reason


def test_buy_is_capped_by_available_balance(tmp_path) -> None:
    settings = Settings(
        copy_ratio=1,
        max_trade_usd=25,
        max_trade_age_seconds=60,
        daily_spend_cap_usd=100,
        per_market_exposure_cap_usd=100,
        max_buy_price=None,
        max_seconds_until_market_end=None,
    )
    db = Database(tmp_path / "db.sqlite3")
    engine = DecisionEngine(settings, db, FakeClob(ask=0.5), balance_provider=FakeBalance(7))

    decision = engine.decide(make_trade())

    assert decision.should_copy
    assert decision.copy_notional_usd == 7
    assert decision.details["available_balance_usd"] == 7


def test_buy_fails_when_maximum_buy_price_is_exceeded(tmp_path) -> None:
    settings = Settings(max_buy_price=0.9, max_slippage_cents=50, max_trade_age_seconds=60)
    db = Database(tmp_path / "db.sqlite3")
    engine = DecisionEngine(settings, db, FakeClob(ask=0.95))

    decision = engine.decide(make_trade())

    assert not decision.should_copy
    assert "maximum buy price exceeded" in decision.reason
    assert decision.details["source_price"] == 0.5
    assert decision.details["executable_price"] == 0.95


def test_slippage_rejection_contains_exact_prices(tmp_path) -> None:
    settings = Settings(max_slippage_cents=2, max_trade_age_seconds=60)
    db = Database(tmp_path / "db.sqlite3")
    engine = DecisionEngine(settings, db, FakeClob(ask=0.55))

    decision = engine.decide(make_trade())

    assert not decision.should_copy
    assert "source=0.5000" in decision.reason
    assert "executable=0.5500" in decision.reason
    assert decision.details["slippage_cents"] == 5
    assert decision.details["allowed_slippage_price"] == 0.52


def test_buy_requires_market_to_end_within_window(tmp_path) -> None:
    settings = Settings(
        max_seconds_until_market_end=3600,
        max_trade_age_seconds=60,
    )
    db = Database(tmp_path / "db.sqlite3")
    engine = DecisionEngine(
        settings,
        db,
        FakeClob(),
        market_end_provider=FakeMarketEnd(datetime.now(timezone.utc) + timedelta(hours=2)),
    )

    decision = engine.decide(make_trade())

    assert not decision.should_copy
    assert "market ends too late" in decision.reason
    assert decision.details["seconds_until_market_end"] > 3600


def test_buy_skips_when_market_end_is_missing(tmp_path) -> None:
    settings = Settings(max_seconds_until_market_end=3600, max_trade_age_seconds=60)
    db = Database(tmp_path / "db.sqlite3")
    engine = DecisionEngine(settings, db, FakeClob(), market_end_provider=FakeMarketEnd(None))

    decision = engine.decide(make_trade())

    assert not decision.should_copy
    assert decision.reason == "market end time unavailable"


def test_short_duration_up_down_filter_accepts_configured_duration(tmp_path) -> None:
    settings = Settings(
        market_type_filter=MarketTypeFilter.SHORT_DURATION_UP_DOWN,
        up_down_min_duration_seconds=300,
        up_down_max_duration_seconds=900,
        max_seconds_until_market_end=None,
        max_trade_age_seconds=60,
    )
    db = Database(tmp_path / "db.sqlite3")
    engine = DecisionEngine(
        settings,
        db,
        FakeClob(),
        market_end_provider=FakeMarketMetadata(300),
    )

    assert engine.decide(make_trade()).should_copy


def test_minimum_net_upside_rejects_small_theoretical_return(tmp_path) -> None:
    settings = Settings(
        max_trade_usd=10,
        min_net_upside_usd=11,
        max_seconds_until_market_end=None,
        max_trade_age_seconds=60,
    )
    db = Database(tmp_path / "db.sqlite3")

    decision = DecisionEngine(settings, db, FakeClob(ask=0.5)).decide(make_trade())

    assert not decision.should_copy
    assert "maximum net upside" in decision.reason
    assert decision.details["strategy_config"]["min_net_upside_usd"] == 11
    assert decision.details["strategy_config"]["max_trade_usd"] == 10
