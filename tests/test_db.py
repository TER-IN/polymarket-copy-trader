from datetime import datetime, timedelta, timezone
from dataclasses import replace
from zoneinfo import ZoneInfo

from db import Database
from models import RedemptionEvent, SourceTokenStatus, TradeEvent, TradeSide


def make_trade(size: float = 10, price: float = 0.5) -> TradeEvent:
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
        price=price,
        size=size,
        notional_usd=size * price,
        raw_payload={},
    )


def test_insert_trade_deduplicates(tmp_path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    trade = make_trade()

    assert db.insert_trade(trade) is True
    assert db.insert_trade(trade) is False


def test_wallet_profile_names_are_read_from_latest_trade_payload(tmp_path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    trade = replace(make_trade(), raw_payload={"name": "justdance"})

    assert db.insert_trade(trade) is True

    assert db.wallet_profile_names(["0xABC"]) == {"0xabc": "justdance"}


def test_position_dashboard_rows_include_event_slug(tmp_path) -> None:
    from positions import apply_buy

    db = Database(tmp_path / "db.sqlite3")
    trade = replace(make_trade(), raw_payload={"eventSlug": "eth-updown-5m-1780776900"})
    db.insert_trade(trade)
    db.upsert_position(apply_buy(None, "m1", "tok1", "Yes", 10, 0.5, "0xabc"))

    row = db.position_dashboard_rows()[0]

    assert row["event_slug"] == "eth-updown-5m-1780776900"


def test_position_dashboard_rows_include_resolution_state(tmp_path) -> None:
    from positions import apply_buy

    db = Database(tmp_path / "db.sqlite3")
    trade = make_trade()
    db.insert_trade(trade)
    db.record_copy_decision(
        trade,
        True,
        "copy allowed",
        {"market_end_time": "2026-06-08T15:00:00+00:00"},
    )
    db.record_order(trade.dedupe_key, trade, 5, 10, 0.5, "dry_run")
    db.upsert_position(apply_buy(None, "m1", "tok1", "Yes", 10, 0.5, "0xabc"))
    db.record_market_resolution_observation(
        "m1",
        False,
        {},
        "Market",
        {"closed": False},
    )

    row = db.position_dashboard_rows()[0]

    assert row["market_end_time"] == "2026-06-08T15:00:00+00:00"
    assert row["resolution_resolved"] == 0
    assert row["resolution_checked_at"] is not None


def test_freeze_source_token_preserves_original_reason(tmp_path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    trade = make_trade()

    db.freeze_source_token_for_trade(trade, "slippage check failed")
    db.freeze_source_token_for_trade(trade, "source token frozen: slippage check failed")

    state = db.get_source_token_state_for_trade(trade)
    assert state is not None
    assert state["status"] == SourceTokenStatus.FROZEN.value
    assert state["freeze_reason"] == "slippage check failed"


def test_source_state_includes_structured_decision_details(tmp_path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    trade = make_trade()
    details = {"source_price": 0.5, "executable_price": 0.55, "slippage_cents": 5}

    db.record_copy_decision(trade, False, "slippage check failed", details)
    db.freeze_source_token_for_trade(trade, "slippage check failed")

    row = db.source_token_states()[0]
    assert row["decision_details"] is not None
    assert '"executable_price": 0.55' in row["decision_details"]


def test_simulated_cash_balance_tracks_buys_sells_and_settlements(tmp_path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    buy = make_trade(size=20, price=0.5)
    db.record_order(buy.dedupe_key, buy, 10, 20, 0.5, "dry_run")
    sell = replace(buy, transaction_hash="0xsell", side=TradeSide.SELL)
    db.record_order(sell.dedupe_key, sell, 4, 8, 0.5, "dry_run")
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO copied_redemptions (
              source_redemption_key, market_id, asset_id, outcome, shares,
              payout_usd, realized_pnl, status
            ) VALUES ('resolution:test', 'm1', 'tok1', 'Yes', 2, 2, 1, 'dry_run_resolution')
            """
        )

    assert db.simulated_cash_balance(100) == 96


def test_simulated_cash_balance_accounts_for_fees_and_filled_notional(tmp_path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    buy = make_trade(size=20, price=0.5)
    db.record_order(
        buy.dedupe_key,
        buy,
        10,
        20,
        0.5,
        "dry_run",
        filled_shares=10,
        filled_notional_usd=5,
        estimated_fee_usd=0.25,
    )

    assert db.simulated_cash_balance(100) == 94.75


def test_redemption_settles_open_position(tmp_path) -> None:
    from positions import apply_buy

    db = Database(tmp_path / "db.sqlite3")
    trade = make_trade(size=10, price=0.9)
    db.upsert_position(apply_buy(None, "m1", "tok1", "Yes", 10, 0.9, "0xabc"))
    redemption = RedemptionEvent(
        source_wallet="0xabc",
        transaction_hash="0xredeem",
        timestamp=datetime.now(timezone.utc),
        market_id="m1",
        condition_id="c1",
        asset_id="tok1",
        token_id="tok1",
        market_title="Market",
        outcome="Yes",
        size=10,
        payout_usd=10,
        raw_payload={},
    )

    assert db.insert_redemption(redemption) is True
    assert db.insert_redemption(redemption) is False
    positions = db.copied_positions_for_redemption(redemption)
    assert len(positions) == 1
    db.settle_position_from_redemption(redemption, positions[0], "dry_run")

    position = db.get_position("m1", "tok1", "Yes")
    assert position is not None
    assert position.total_shares == 0
    assert position.total_cost == 0
    assert position.realized_pnl == 1


def test_wallet_pnl_points_are_upserted_and_detectable(tmp_path) -> None:
    db = Database(tmp_path / "db.sqlite3")

    changed = db.upsert_wallet_pnl_points("0xABC", "all", "1d", [(100, 1.25), (200, 2.5)])

    assert changed == 2
    assert db.has_wallet_pnl_timestamp("0xabc", "all", "1d", 200)
    rows = db.wallet_pnl_points("0xabc", "all", "1d", before_ts=250)
    assert [(row["timestamp"], row["pnl"]) for row in rows] == [(100, 1.25), (200, 2.5)]

    db.upsert_wallet_pnl_points("0xabc", "all", "1d", [(200, 3.0)])
    rows = db.wallet_pnl_points("0xabc", "all", "1d", before_ts=250)
    assert [(row["timestamp"], row["pnl"]) for row in rows] == [(100, 1.25), (200, 3.0)]


def test_daily_performance_uses_configured_trading_day_timezone(tmp_path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    tz = ZoneInfo("Europe/Prague")
    today_start_local = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    today_0030_utc = (today_start_local + timedelta(minutes=30)).astimezone(timezone.utc)
    yesterday_2330_utc = (today_start_local - timedelta(minutes=30)).astimezone(timezone.utc)

    db.record_order("today", make_trade(), 10, 20, 0.5, "dry_run")
    db.record_order("yesterday", replace(make_trade(), transaction_hash="0xy"), 7, 14, 0.5, "dry_run")
    with db.connect() as conn:
        conn.execute(
            "UPDATE copied_orders SET created_at = ? WHERE source_trade_key = ?",
            (today_0030_utc.strftime("%Y-%m-%d %H:%M:%S"), "today"),
        )
        conn.execute(
            "UPDATE copied_orders SET created_at = ? WHERE source_trade_key = ?",
            (yesterday_2330_utc.strftime("%Y-%m-%d %H:%M:%S"), "yesterday"),
        )

    rows = db.daily_performance_rows("Europe/Prague", days=2)

    assert rows[0]["date"] == today_start_local.date().isoformat()
    assert rows[0]["buy_spend"] == 10
    assert rows[1]["buy_spend"] == 7
