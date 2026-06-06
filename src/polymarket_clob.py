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


def _best_price(levels: list[dict[str, Any]], minimum: bool) -> float | None:
    prices = []
    for level in levels:
        price = level.get("price")
        if price is not None:
            prices.append(float(price))
    if not prices:
        return None
    return min(prices) if minimum else max(prices)
