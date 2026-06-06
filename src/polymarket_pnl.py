from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests
from tenacity import retry, stop_after_attempt, wait_exponential


@dataclass(frozen=True)
class PnlPoint:
    timestamp: int
    pnl: float


class UserPnlClient:
    def __init__(self, base_url: str = "https://user-pnl-api.polymarket.com", timeout: int = 15):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    @retry(wait=wait_exponential(multiplier=1, min=1, max=10), stop=stop_after_attempt(4))
    def get_user_pnl(self, wallet: str, interval: str = "all", fidelity: str = "1d") -> list[PnlPoint]:
        response = self.session.get(
            f"{self.base_url}/user-pnl",
            params={"user_address": wallet, "interval": interval, "fidelity": fidelity},
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            return []
        points = []
        for item in data:
            if not isinstance(item, dict):
                continue
            timestamp = item.get("t")
            pnl = item.get("p")
            if timestamp is None or pnl is None:
                continue
            points.append(PnlPoint(timestamp=int(timestamp), pnl=float(pnl)))
        return sorted(points, key=lambda point: point.timestamp)


def daily_candles(points: list[PnlPoint]) -> list[dict[str, Any]]:
    candles = []
    previous_close = 0.0
    for point in sorted(points, key=lambda item: item.timestamp):
        open_value = previous_close
        close_value = point.pnl
        daily_pnl = close_value - open_value
        candles.append(
            {
                "t": point.timestamp,
                "date": datetime.fromtimestamp(point.timestamp, tz=timezone.utc).date().isoformat(),
                "open": open_value,
                "high": max(open_value, close_value),
                "low": min(open_value, close_value),
                "close": close_value,
                "daily_pnl": daily_pnl,
            }
        )
        previous_close = close_value
    return candles


class CachedUserPnlClient:
    def __init__(self, client: UserPnlClient, ttl_seconds: int = 300):
        self.client = client
        self.ttl_seconds = ttl_seconds
        self._cache: dict[tuple[str, str, str], tuple[float, list[PnlPoint]]] = {}

    def get_user_pnl(self, wallet: str, interval: str = "all", fidelity: str = "1d") -> list[PnlPoint]:
        key = (wallet.lower(), interval, fidelity)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and now - cached[0] < self.ttl_seconds:
            return cached[1]
        points = self.client.get_user_pnl(wallet, interval=interval, fidelity=fidelity)
        self._cache[key] = (now, points)
        return points
