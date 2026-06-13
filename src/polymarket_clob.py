from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from models import TradeSide


@dataclass(frozen=True)
class BookQuote:
    token_id: str
    best_bid: float | None
    best_ask: float | None
    raw: dict[str, Any]

    def executable_price(self, side: TradeSide) -> float | None:
        return self.best_ask if side == TradeSide.BUY else self.best_bid


@dataclass(frozen=True)
class BookFill:
    price: float
    shares: float
    notional_usd: float


@dataclass(frozen=True)
class ExecutionEstimate:
    token_id: str
    side: TradeSide
    requested_notional_usd: float
    requested_shares: float
    filled_notional_usd: float
    filled_shares: float
    average_price: float | None
    worst_price: float | None
    fill_ratio: float
    estimated_fee_usd: float
    fee_rate: float
    fee_exponent: float
    fills: tuple[BookFill, ...]
    raw_book: dict[str, Any]


class PublicClobClient:
    def __init__(self, base_url: str = "https://clob.polymarket.com", timeout: int = 15):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    @retry(wait=wait_exponential(multiplier=1, min=1, max=10), stop=stop_after_attempt(4))
    def get_order_book(self, token_id: str) -> dict[str, Any]:
        response = self.session.get(
            f"{self.base_url}/book",
            params={"token_id": token_id},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def get_quote(self, token_id: str) -> BookQuote:
        book = self.get_order_book(token_id)
        asks = book.get("asks") or []
        bids = book.get("bids") or []
        best_ask = _best_price(asks, minimum=True)
        best_bid = _best_price(bids, minimum=False)
        return BookQuote(token_id=token_id, best_bid=best_bid, best_ask=best_ask, raw=book)

    @retry(wait=wait_exponential(multiplier=1, min=1, max=10), stop=stop_after_attempt(4))
    def get_fee_rate(self, token_id: str) -> float:
        response = self.session.get(f"{self.base_url}/fee-rate/{token_id}", timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        base_fee = float(payload.get("base_fee") or payload.get("baseFee") or 0)
        return max(0.0, base_fee / 10_000.0)

    def estimate_execution(
        self,
        token_id: str,
        side: TradeSide,
        *,
        requested_notional_usd: float = 0.0,
        requested_shares: float = 0.0,
        limit_price: float | None = None,
        fee_rate: float = 0.0,
        fee_exponent: float = 1.0,
    ) -> ExecutionEstimate:
        book = self.get_order_book(token_id)
        levels = book.get("asks" if side == TradeSide.BUY else "bids") or []
        levels = sorted(
            (level for level in levels if level.get("price") is not None and level.get("size") is not None),
            key=lambda level: float(level["price"]),
            reverse=side == TradeSide.SELL,
        )
        remaining_notional = max(0.0, requested_notional_usd)
        remaining_shares = max(0.0, requested_shares)
        fills: list[BookFill] = []
        for level in levels:
            price = float(level["price"])
            available_shares = max(0.0, float(level["size"]))
            if available_shares <= 0 or not _within_limit(side, price, limit_price):
                continue
            if side == TradeSide.BUY:
                if remaining_notional <= 1e-12 or (
                    requested_shares > 0 and remaining_shares <= 1e-12
                ):
                    break
                shares = min(available_shares, remaining_notional / price)
                if requested_shares > 0:
                    shares = min(shares, remaining_shares)
                notional = shares * price
                remaining_notional -= notional
                if requested_shares > 0:
                    remaining_shares -= shares
            else:
                if remaining_shares <= 1e-12:
                    break
                shares = min(available_shares, remaining_shares)
                notional = shares * price
                remaining_shares -= shares
            fills.append(BookFill(price, shares, notional))

        filled_shares = sum(fill.shares for fill in fills)
        filled_notional = sum(fill.notional_usd for fill in fills)
        share_sized_buy = side == TradeSide.BUY and requested_shares > 0
        requested = (
            requested_shares
            if share_sized_buy
            else requested_notional_usd if side == TradeSide.BUY else requested_shares
        )
        filled = filled_shares if share_sized_buy or side == TradeSide.SELL else filled_notional
        average = filled_notional / filled_shares if filled_shares > 0 else None
        fee = sum(
            fill.shares * fee_rate * (fill.price * (1.0 - fill.price)) ** fee_exponent
            for fill in fills
        )
        return ExecutionEstimate(
            token_id=token_id,
            side=side,
            requested_notional_usd=requested_notional_usd,
            requested_shares=requested_shares,
            filled_notional_usd=filled_notional,
            filled_shares=filled_shares,
            average_price=average,
            worst_price=fills[-1].price if fills else None,
            fill_ratio=min(1.0, filled / requested) if requested > 0 else 0.0,
            estimated_fee_usd=fee,
            fee_rate=fee_rate,
            fee_exponent=fee_exponent,
            fills=tuple(fills),
            raw_book=book,
        )


def _best_price(levels: list[dict[str, Any]], minimum: bool) -> float | None:
    prices = []
    for level in levels:
        price = level.get("price")
        if price is not None:
            prices.append(float(price))
    if not prices:
        return None
    return min(prices) if minimum else max(prices)


def _within_limit(side: TradeSide, price: float, limit_price: float | None) -> bool:
    if limit_price is None:
        return True
    return price <= limit_price if side == TradeSide.BUY else price >= limit_price
