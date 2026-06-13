from models import TradeSide
from polymarket_clob import PublicClobClient


def test_buy_estimate_walks_book_and_allows_partial_fill() -> None:
    client = PublicClobClient()
    client.get_order_book = lambda _token: {
        "asks": [
            {"price": "0.50", "size": "10"},
            {"price": "0.52", "size": "5"},
            {"price": "0.55", "size": "100"},
        ],
        "bids": [],
    }

    estimate = client.estimate_execution(
        "token",
        TradeSide.BUY,
        requested_notional_usd=10,
        limit_price=0.52,
        fee_rate=0.1,
    )

    assert estimate.filled_shares == 15
    assert estimate.filled_notional_usd == 7.6
    assert estimate.fill_ratio == 0.76
    assert estimate.average_price == 7.6 / 15
    assert estimate.worst_price == 0.52
    assert estimate.estimated_fee_usd > 0


def test_share_sized_buy_stops_at_requested_shares_and_notional_cap() -> None:
    client = PublicClobClient()
    client.get_order_book = lambda _token: {
        "asks": [
            {"price": "0.40", "size": "5"},
            {"price": "0.50", "size": "20"},
        ],
        "bids": [],
    }

    complete = client.estimate_execution(
        "token",
        TradeSide.BUY,
        requested_notional_usd=10,
        requested_shares=10,
        limit_price=0.50,
    )
    capped = client.estimate_execution(
        "token",
        TradeSide.BUY,
        requested_notional_usd=3,
        requested_shares=10,
        limit_price=0.50,
    )

    assert complete.filled_shares == 10
    assert complete.filled_notional_usd == 4.5
    assert complete.fill_ratio == 1
    assert capped.filled_shares == 7
    assert capped.filled_notional_usd == 3
    assert capped.fill_ratio == 0.7


def test_sell_estimate_uses_highest_bids_first() -> None:
    client = PublicClobClient()
    client.get_order_book = lambda _token: {
        "asks": [],
        "bids": [
            {"price": "0.45", "size": "10"},
            {"price": "0.49", "size": "4"},
        ],
    }

    estimate = client.estimate_execution(
        "token",
        TradeSide.SELL,
        requested_shares=8,
        limit_price=0.44,
    )

    assert [(fill.price, fill.shares) for fill in estimate.fills] == [(0.49, 4), (0.45, 4)]
