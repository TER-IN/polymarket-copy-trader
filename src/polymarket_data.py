from __future__ import annotations

from datetime import datetime
from typing import Any

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from models import RedemptionEvent, SourcePositionSnapshot, TradeEvent, TradeSide, parse_timestamp


def _first(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    return None


def normalize_trade(payload: dict[str, Any], source_wallet: str | None = None) -> TradeEvent:
    wallet = source_wallet or _first(payload, "user", "proxyWallet", "wallet", "maker") or ""
    tx_hash = _first(payload, "transactionHash", "transaction_hash", "txHash", "hash") or ""
    timestamp = parse_timestamp(_first(payload, "timestamp", "time", "createdAt", "created_at"))
    side_raw = str(_first(payload, "side", "type", "takerSide") or "BUY").upper()
    side = TradeSide.SELL if "SELL" in side_raw else TradeSide.BUY
    price = float(_first(payload, "price", "avgPrice", "averagePrice") or 0)
    size = float(_first(payload, "size", "shares", "amount", "quantity") or 0)
    notional = _first(payload, "notional", "notionalUsd", "notional_usd", "usdcSize")
    notional_usd = float(notional) if notional is not None else price * size

    return TradeEvent(
        source_wallet=wallet,
        transaction_hash=tx_hash,
        timestamp=timestamp,
        market_id=_first(payload, "market", "marketId", "market_id"),
        condition_id=_first(payload, "conditionId", "condition_id"),
        asset_id=_first(payload, "asset", "assetId", "asset_id"),
        token_id=_first(payload, "tokenId", "token_id", "asset"),
        market_title=_first(payload, "title", "marketTitle", "question", "slug"),
        outcome=_first(payload, "outcome", "outcomeName"),
        side=side,
        price=price,
        size=size,
        notional_usd=notional_usd,
        raw_payload=payload,
    )


def normalize_position(payload: dict[str, Any], source_wallet: str | None = None) -> SourcePositionSnapshot:
    wallet = source_wallet or _first(payload, "user", "proxyWallet", "wallet") or ""
    avg_price = _first(payload, "avgPrice", "avg_price")
    return SourcePositionSnapshot(
        source_wallet=wallet,
        market_id=_first(payload, "conditionId", "condition_id", "market", "marketId") or "",
        asset_id=_first(payload, "asset", "assetId", "tokenId", "token_id") or "",
        outcome=_first(payload, "outcome", "outcomeName") or "",
        size=float(_first(payload, "size", "shares", "tokens") or 0),
        avg_price=float(avg_price) if avg_price is not None else None,
        raw_payload=payload,
    )


def normalize_redemption(payload: dict[str, Any], source_wallet: str | None = None) -> RedemptionEvent:
    wallet = source_wallet or _first(payload, "user", "proxyWallet", "wallet") or ""
    size = float(_first(payload, "size", "shares", "amount", "quantity") or 0)
    payout = _first(payload, "usdcSize", "payout", "payoutUsd", "notional", "notionalUsd")
    payout_usd = float(payout) if payout is not None else size
    return RedemptionEvent(
        source_wallet=wallet,
        transaction_hash=_first(payload, "transactionHash", "transaction_hash", "txHash", "hash") or "",
        timestamp=parse_timestamp(_first(payload, "timestamp", "time", "createdAt", "created_at")),
        market_id=_first(payload, "market", "marketId", "market_id", "conditionId", "condition_id"),
        condition_id=_first(payload, "conditionId", "condition_id"),
        asset_id=_first(payload, "asset", "assetId", "asset_id"),
        token_id=_first(payload, "tokenId", "token_id", "asset"),
        market_title=_first(payload, "title", "marketTitle", "question", "slug"),
        outcome=_first(payload, "outcome", "outcomeName"),
        size=size,
        payout_usd=payout_usd,
        raw_payload=payload,
    )


class PolymarketDataClient:
    def __init__(self, base_url: str = "https://data-api.polymarket.com", timeout: int = 15):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    @retry(wait=wait_exponential(multiplier=1, min=1, max=10), stop=stop_after_attempt(4))
    def get_trades(self, **params: Any) -> list[dict[str, Any]]:
        response = self.session.get(f"{self.base_url}/trades", params=params, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict):
            for key in ("data", "trades", "results"):
                if isinstance(data.get(key), list):
                    return data[key]
        if isinstance(data, list):
            return data
        return []

    @retry(wait=wait_exponential(multiplier=1, min=1, max=10), stop=stop_after_attempt(4))
    def get_activity(self, **params: Any) -> list[dict[str, Any]]:
        response = self.session.get(f"{self.base_url}/activity", params=params, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict):
            for key in ("data", "activity", "results"):
                if isinstance(data.get(key), list):
                    return data[key]
        if isinstance(data, list):
            return data
        return []

    @retry(wait=wait_exponential(multiplier=1, min=1, max=10), stop=stop_after_attempt(4))
    def get_positions(self, **params: Any) -> list[dict[str, Any]]:
        response = self.session.get(f"{self.base_url}/positions", params=params, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict):
            for key in ("data", "positions", "results"):
                if isinstance(data.get(key), list):
                    return data[key]
        if isinstance(data, list):
            return data
        return []

    def recent_wallet_trades(self, wallet: str, limit: int = 100) -> list[TradeEvent]:
        payloads = []
        for item in self.get_trades(user=wallet, limit=limit):
            item["_api_source"] = "trades"
            payloads.append(item)
        for item in self.get_activity(user=wallet, type="TRADE", limit=min(limit, 500)):
            item["_api_source"] = "activity"
            payloads.append(item)

        events = [normalize_trade(item, source_wallet=wallet) for item in payloads]
        deduped: dict[str, TradeEvent] = {}
        for event in events:
            existing = deduped.get(event.dedupe_key)
            if existing is None or event.raw_payload.get("_api_source") == "activity":
                deduped[event.dedupe_key] = event
        return sorted(deduped.values(), key=lambda trade: trade.timestamp, reverse=True)

    def recent_wallet_activity(self, wallet: str, limit: int = 100) -> list[TradeEvent]:
        payloads = self.get_activity(user=wallet, type="TRADE", limit=min(limit, 500))
        for item in payloads:
            item["_api_source"] = "activity"
        return [normalize_trade(item, source_wallet=wallet) for item in payloads]

    def recent_wallet_redemptions(self, wallet: str, limit: int = 100) -> list[RedemptionEvent]:
        payloads = self.get_activity(user=wallet, type="REDEEM", limit=min(limit, 500))
        for item in payloads:
            item["_api_source"] = "activity"
        events = [normalize_redemption(item, source_wallet=wallet) for item in payloads]
        deduped: dict[str, RedemptionEvent] = {}
        for event in events:
            deduped[event.dedupe_key] = event
        return sorted(deduped.values(), key=lambda redemption: redemption.timestamp, reverse=True)

    def current_wallet_positions(self, wallet: str, size_threshold: float = 0.01, limit: int = 500) -> list[SourcePositionSnapshot]:
        payloads = self.get_positions(user=wallet, sizeThreshold=size_threshold, limit=limit)
        return [normalize_position(item, source_wallet=wallet) for item in payloads]

    def market_trades_between(
        self,
        market: str | None,
        condition_id: str | None,
        start: datetime,
        end: datetime,
        limit: int = 500,
    ) -> list[TradeEvent]:
        params: dict[str, Any] = {
            "limit": limit,
            "startTs": int(start.timestamp()),
            "endTs": int(end.timestamp()),
        }
        if market:
            params["market"] = market
        if condition_id:
            params["conditionId"] = condition_id
        return [normalize_trade(item) for item in self.get_trades(**params)]
