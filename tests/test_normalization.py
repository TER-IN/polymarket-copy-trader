from datetime import timezone

from models import TradeSide
from polymarket_data import PolymarketDataClient, normalize_trade


def test_normalize_trade_payload() -> None:
    trade = normalize_trade(
        {
            "user": "0xabc",
            "transactionHash": "0xtx",
            "timestamp": 1_700_000_000,
            "market": "m1",
            "conditionId": "c1",
            "asset": "tok1",
            "title": "Will it rain?",
            "outcome": "Yes",
            "side": "BUY",
            "price": "0.42",
            "size": "10",
        }
    )

    assert trade.source_wallet == "0xabc"
    assert trade.transaction_hash == "0xtx"
    assert trade.timestamp.tzinfo == timezone.utc
    assert trade.side == TradeSide.BUY
    assert trade.notional_usd == 4.2
    assert "0xtx" in trade.dedupe_key


def test_recent_wallet_trades_combines_activity_and_trades(monkeypatch) -> None:
    client = PolymarketDataClient()
    trade_payload = {
        "proxyWallet": "0xabc",
        "transactionHash": "0xtx",
        "timestamp": 1_700_000_000,
        "asset": "tok1",
        "conditionId": "c1",
        "title": "Market",
        "outcome": "Yes",
        "side": "BUY",
        "price": 0.5,
        "size": 10,
    }
    activity_payload = trade_payload | {"usdcSize": 5}

    monkeypatch.setattr(client, "get_trades", lambda **_: [trade_payload.copy()])
    monkeypatch.setattr(client, "get_activity", lambda **_: [activity_payload.copy()])

    trades = client.recent_wallet_trades("0xabc")

    assert len(trades) == 1
    assert trades[0].raw_payload["_api_source"] == "activity"
