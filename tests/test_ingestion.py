from dataclasses import replace
from datetime import datetime, timezone

import pytest

from config import Settings
from db import Database
from decision_engine import DecisionEngine
from execution import Executor
from ingestion import PollingIngestor
from models import MarketTypeFilter, OutcomeSelectionMode, TradeEvent, TradeSide
from models import CopyDecision, ExecutionResult
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
    def __init__(self, ask_by_token=None):
        self.ask_by_token = ask_by_token or {}

    def get_quote(self, token_id: str) -> BookQuote:
        price = self.ask_by_token.get(token_id, 0.4)
        return BookQuote(token_id=token_id, best_bid=price, best_ask=price, raw={})


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
        inverse_share_copy_ratio=0.1,
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
    assert position.total_shares == 10
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


def test_inverse_down_underdog_skip_does_not_freeze_source_state(tmp_path) -> None:
    settings = Settings(
        outcome_selection_mode=OutcomeSelectionMode.INVERSE_DOWN_UNDERDOG,
        inverse_down_max_source_price=0.5,
        enable_crowding_check=False,
    )
    db = Database(tmp_path / "db.sqlite3")
    ingestor = PollingIngestor(
        settings,
        db,
        FakeDataClient([]),
        DecisionEngine(settings, db, FakeClob()),
        Executor(settings, db),
        outcome_selector=OutcomeSelector(
            settings.outcome_selection_mode,
            FakeUpDownProvider(),
            settings.inverse_down_max_source_price,
        ),
    )
    trade = TradeEvent(
        **{
            **make_trade().__dict__,
            "asset_id": "up",
            "token_id": "up",
            "outcome": "Up",
            "price": 0.4,
            "notional_usd": 40,
        }
    )

    assert ingestor.process_trade(trade)
    assert db.get_source_token_state_for_trade(trade) is None
    with db.connect() as conn:
        decision = conn.execute(
            "SELECT should_copy, reason FROM copy_decisions WHERE source_trade_key = ?",
            (trade.dedupe_key,),
        ).fetchone()
    assert decision["should_copy"] == 0
    assert "source outcome is not Down" in decision["reason"]


def shadow_regime_settings(**overrides) -> Settings:
    values = {
        "outcome_selection_mode": OutcomeSelectionMode.SHADOW_REGIME_DOWN_UNDERDOG,
        "inverse_down_max_source_price": 0.45,
        "shadow_regime_window": 2,
        "shadow_regime_confirmation_markets": 2,
        "shadow_real_trade_policy": "auto_regime",
        "inverse_share_copy_ratio": 0.1,
        "max_trade_usd": 100,
        "max_trade_age_seconds": 60,
        "daily_spend_cap_usd": None,
        "per_market_exposure_cap_usd": 1000,
        "condition_exposure_cap_usd": None,
        "enable_crowding_check": False,
        "max_buy_price": None,
        "max_seconds_until_market_end": None,
        "market_type_filter": MarketTypeFilter.ALL,
        "min_net_upside_usd": None,
        "min_net_upside_percent": None,
        "net_upside_safety_margin_usd": 0,
        "allow_market_title_keywords": [],
        "dry_run_starting_balance_usd": None,
    }
    values.update(overrides)
    return Settings(**values)


def shadow_source_trade(market_id: str, transaction_hash: str) -> TradeEvent:
    return TradeEvent(
        source_wallet="0xabc",
        transaction_hash=transaction_hash,
        timestamp=datetime.now(timezone.utc),
        market_id=market_id,
        condition_id=market_id,
        asset_id="down",
        token_id="down",
        market_title="Bitcoin Up or Down",
        outcome="Down",
        side=TradeSide.BUY,
        price=0.4,
        size=100,
        notional_usd=40,
        raw_payload={},
    )


def seed_shadow_result(db: Database, market_id: str, payout: float) -> None:
    source = shadow_source_trade(market_id, f"0x{market_id}")
    shadow = TradeEvent(
        **{
            **source.__dict__,
            "asset_id": "up",
            "token_id": "up",
            "outcome": "Up",
            "price": 0.6,
        }
    )
    decision = CopyDecision(
        True,
        "copy allowed",
        copy_notional_usd=4,
        copy_shares=10,
        current_price=0.4,
        details={"market_end_time": "2020-01-01T00:00:00+00:00"},
    )
    db.record_shadow_order(source, shadow, "down", "Down", decision)
    db.record_market_resolution_observation(
        market_id,
        True,
        {"up": payout, "down": 1.0 - payout},
        "Bitcoin Up or Down",
        {},
    )


def shadow_regime_ingestor(
    settings: Settings,
    db: Database,
    clob: FakeClob | None = None,
) -> PollingIngestor:
    return PollingIngestor(
        settings,
        db,
        FakeDataClient([]),
        DecisionEngine(settings, db, clob or FakeClob()),
        Executor(settings, db),
        outcome_selector=OutcomeSelector(
            settings.outcome_selection_mode,
            FakeUpDownProvider(),
            settings.inverse_down_max_source_price,
        ),
    )


def test_shadow_regime_records_shadow_order_without_real_order_during_warmup(tmp_path) -> None:
    settings = shadow_regime_settings()
    db = Database(tmp_path / "db.sqlite3")

    assert shadow_regime_ingestor(settings, db).process_trade(
        shadow_source_trade("current", "0xcurrent")
    )

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM shadow_orders").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM copied_orders").fetchone()[0] == 0
        decision = conn.execute("SELECT reason FROM copy_decisions").fetchone()
    assert "regime warm-up 0/2" in decision["reason"]


def test_shadow_regime_initial_path_trades_during_warmup(tmp_path) -> None:
    settings = shadow_regime_settings(shadow_regime_initial_path="follow_shadow")
    db = Database(tmp_path / "db.sqlite3")

    assert shadow_regime_ingestor(settings, db).process_trade(
        shadow_source_trade("current", "0xcurrent")
    )

    order = db.copied_order_rows(1)[0]
    assert order["copied_outcome"] == "Up"
    with db.connect() as conn:
        shadow = conn.execute("SELECT * FROM shadow_orders").fetchone()
        decision = conn.execute("SELECT details FROM copy_decisions").fetchone()
    assert shadow["opposite_should_copy"] == 1
    assert shadow["opposite_avg_fill_price"] == 0.4
    assert '"effective_path": "follow_shadow"' in decision["details"]


def test_shadow_regime_runtime_override_takes_precedence_and_survives_restart(
    tmp_path,
) -> None:
    settings = shadow_regime_settings(shadow_regime_initial_path="follow_shadow")
    path = tmp_path / "db.sqlite3"
    Database(path).set_shadow_regime_override("0xabc", "invert_shadow")
    db = Database(path)

    assert shadow_regime_ingestor(settings, db).process_trade(
        shadow_source_trade("current", "0xcurrent")
    )

    order = db.copied_order_rows(1)[0]
    assert order["copied_outcome"] == "Down"
    with db.connect() as conn:
        decision = conn.execute("SELECT details FROM copy_decisions").fetchone()
    assert '"override": "invert_shadow"' in decision["details"]
    assert '"effective_path": "invert_shadow"' in decision["details"]


def test_shadow_price_filter_follows_high_shadow_price_during_warmup(tmp_path) -> None:
    settings = shadow_regime_settings(shadow_real_trade_policy="price_filter")
    db = Database(tmp_path / "db.sqlite3")
    source = replace(
        shadow_source_trade("current", "0xcurrent"),
        price=0.2,
        notional_usd=20,
    )

    assert shadow_regime_ingestor(settings, db, FakeClob({"up": 0.72, "down": 0.42})).process_trade(
        source
    )

    order = db.copied_order_rows(1)[0]
    assert order["copied_outcome"] == "Up"
    with db.connect() as conn:
        decision = conn.execute("SELECT reason, details FROM copy_decisions").fetchone()
    assert "price filter selected follow_shadow" in decision["reason"]
    assert '"real_execution_path": "follow_shadow"' in decision["details"]


def test_shadow_price_filter_inverts_mid_price_opposite_during_warmup(tmp_path) -> None:
    settings = shadow_regime_settings(shadow_real_trade_policy="price_filter")
    db = Database(tmp_path / "db.sqlite3")

    assert shadow_regime_ingestor(settings, db, FakeClob({"up": 0.62, "down": 0.42})).process_trade(
        shadow_source_trade("current", "0xcurrent")
    )

    order = db.copied_order_rows(1)[0]
    assert order["copied_outcome"] == "Down"
    with db.connect() as conn:
        decision = conn.execute("SELECT reason, details FROM copy_decisions").fetchone()
    assert "price filter selected invert_shadow" in decision["reason"]
    assert '"real_execution_path": "invert_shadow"' in decision["details"]


def test_shadow_price_filter_can_disable_invert_branch(tmp_path) -> None:
    settings = shadow_regime_settings(
        shadow_real_trade_policy="price_filter",
        shadow_enable_invert_branch=False,
    )
    db = Database(tmp_path / "db.sqlite3")

    assert shadow_regime_ingestor(settings, db, FakeClob({"up": 0.62, "down": 0.42})).process_trade(
        shadow_source_trade("current", "0xcurrent")
    )

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM shadow_orders").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM copied_orders").fetchone()[0] == 0
        decision = conn.execute("SELECT reason, details FROM copy_decisions").fetchone()
    assert "invert branch disabled" in decision["reason"]
    assert '"real_execution_path": null' in decision["details"]


def test_shadow_price_filter_records_but_skips_unqualified_signal(tmp_path) -> None:
    settings = shadow_regime_settings(shadow_real_trade_policy="price_filter")
    db = Database(tmp_path / "db.sqlite3")

    assert shadow_regime_ingestor(settings, db, FakeClob({"up": 0.62, "down": 0.36})).process_trade(
        shadow_source_trade("current", "0xcurrent")
    )

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM shadow_orders").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM copied_orders").fetchone()[0] == 0
        decision = conn.execute("SELECT reason, details FROM copy_decisions").fetchone()
    assert "price filter skipped" in decision["reason"]
    assert '"real_trade_policy": "price_filter"' in decision["details"]


def test_shadow_regime_follows_shadow_after_winning_window(tmp_path) -> None:
    settings = shadow_regime_settings()
    db = Database(tmp_path / "db.sqlite3")
    seed_shadow_result(db, "history1", 1.0)
    seed_shadow_result(db, "history2", 1.0)

    assert shadow_regime_ingestor(settings, db).process_trade(
        shadow_source_trade("current", "0xcurrent")
    )

    order = db.copied_order_rows(1)[0]
    assert order["copied_outcome"] == "Up"
    with db.connect() as conn:
        decision = conn.execute("SELECT reason, details FROM copy_decisions").fetchone()
    assert "real path=follow_shadow" in decision["reason"]
    assert '"real_execution_path": "follow_shadow"' in decision["details"]


def test_shadow_regime_inverts_same_shadow_signal_after_losing_window(tmp_path) -> None:
    settings = shadow_regime_settings()
    db = Database(tmp_path / "db.sqlite3")
    seed_shadow_result(db, "history1", 0.0)
    seed_shadow_result(db, "history2", 0.0)

    assert shadow_regime_ingestor(settings, db).process_trade(
        shadow_source_trade("current", "0xcurrent")
    )

    order = db.copied_order_rows(1)[0]
    assert order["copied_outcome"] == "Down"
    with db.connect() as conn:
        decision = conn.execute("SELECT reason, details FROM copy_decisions").fetchone()
    assert "real path=invert_shadow" in decision["reason"]
    assert '"real_execution_path": "invert_shadow"' in decision["details"]


def test_stop_block_does_not_advance_source_lifecycle(tmp_path) -> None:
    class AllowDecision:
        def decide(self, trade, crowding_score=None, source_trade=None):
            return CopyDecision(True, "ok", copy_notional_usd=10, copy_shares=20, current_price=0.5)

    class BlockExecutor:
        def execute(self, trade, decision, source_trade_key=None):
            return ExecutionResult(False, "blocked")

    settings = Settings(enable_crowding_check=False)
    db = Database(tmp_path / "db.sqlite3")
    ingestor = PollingIngestor(
        settings,
        db,
        FakeDataClient([]),
        AllowDecision(),
        BlockExecutor(),
    )
    trade = make_trade()

    assert ingestor.process_trade(trade)
    assert db.get_source_token_state_for_trade(trade) is None


def test_processed_trade_records_precise_observation_and_decision_timing(tmp_path) -> None:
    class RejectDecision:
        def decide(self, trade, crowding_score=None, source_trade=None):
            return CopyDecision(False, "test rejection", details={})

    settings = Settings(enable_crowding_check=False)
    db = Database(tmp_path / "db.sqlite3")
    ingestor = PollingIngestor(
        settings,
        db,
        FakeDataClient([]),
        RejectDecision(),
        FakeExecutor(),
    )

    assert ingestor.process_trade(make_trade())

    trade_row = db.recent_trades(1)[0]
    state = db.source_token_states()[0]
    details = __import__("json").loads(state["decision_details"])
    assert trade_row["observed_at"]
    assert details["observed_at"]
    assert details["decision_completed_at"]
    assert details["decision_processing_ms"] >= 0
    assert details["observation_delay_seconds"] >= 0


def test_market_freeze_rejection_does_not_freeze_opposite_token_state(tmp_path) -> None:
    class MarketFreezeDecision:
        def decide(self, trade, crowding_score=None, source_trade=None):
            return CopyDecision(
                False,
                "source wallet market frozen: earlier slippage check failed",
                details={},
            )

    settings = Settings(enable_crowding_check=False)
    db = Database(tmp_path / "db.sqlite3")
    ingestor = PollingIngestor(
        settings,
        db,
        FakeDataClient([]),
        MarketFreezeDecision(),
        FakeExecutor(),
    )
    trade = make_trade()

    assert ingestor.process_trade(trade)
    assert db.get_source_token_state_for_trade(trade) is None
