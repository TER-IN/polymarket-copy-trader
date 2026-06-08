from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests
from tenacity import retry, stop_after_attempt, wait_exponential


@dataclass(frozen=True)
class MarketResolution:
    resolved: bool
    payout_by_token_id: dict[str, float] = field(default_factory=dict)
    market_title: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)

    def payout_for_token(self, token_id: str) -> float | None:
        return self.payout_by_token_id.get(str(token_id))


@dataclass(frozen=True)
class OutcomeToken:
    token_id: str
    outcome: str


@dataclass(frozen=True)
class MarketMetadata:
    start_time: datetime | None
    end_time: datetime | None
    up_down_tokens: tuple[OutcomeToken, OutcomeToken] | None
    fee_rate: float
    fee_exponent: float
    taker_only_fee: bool
    raw_payload: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float | None:
        if not self.start_time or not self.end_time:
            return None
        return (self.end_time - self.start_time).total_seconds()


class GammaClient:
    def __init__(
        self,
        base_url: str = "https://gamma-api.polymarket.com",
        timeout: int = 15,
        clob_base_url: str = "https://clob.polymarket.com",
    ):
        self.base_url = base_url.rstrip("/")
        self.clob_base_url = clob_base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def get_resolution(
        self,
        market_id: str,
        asset_id: str,
        condition_id: str | None = None,
    ) -> MarketResolution | None:
        condition = condition_id or market_id
        if _looks_like_condition_id(condition):
            clob_payload = self._get_clob_market_by_condition_id(condition)
            if clob_payload:
                resolution = _clob_market_resolution(clob_payload)
                if resolution.payout_for_token(asset_id) is not None or not resolution.resolved:
                    return resolution
        payload = self._find_market(market_id, asset_id, condition_id)
        if not payload:
            return None
        return _market_resolution(payload)

    def get_market_end_time(
        self,
        market_id: str,
        asset_id: str,
        condition_id: str | None = None,
    ) -> datetime | None:
        payload = self._find_market(market_id, asset_id, condition_id)
        if not payload:
            return None
        return _parse_datetime(payload.get("endDate") or payload.get("endDateIso") or payload.get("end_date_iso"))

    def get_market_metadata(
        self,
        market_id: str,
        asset_id: str,
        condition_id: str | None = None,
    ) -> MarketMetadata | None:
        payload = self._find_market(market_id, asset_id, condition_id)
        if not payload:
            return None
        fee_schedule = payload.get("feeSchedule") or payload.get("fee_schedule") or {}
        if not isinstance(fee_schedule, dict):
            fee_schedule = {}
        raw_rate = fee_schedule.get("rate", fee_schedule.get("r", 0))
        raw_exponent = fee_schedule.get("exponent", fee_schedule.get("e", 1))
        rate = max(0.0, _to_float(raw_rate) or 0.0)
        if rate > 1:
            rate /= 10_000.0
        return MarketMetadata(
            start_time=_parse_datetime(
                payload.get("eventStartTime")
                or payload.get("event_start_time")
                or payload.get("gameStartTime")
                or payload.get("game_start_time")
                or payload.get("startDate")
                or payload.get("startDateIso")
                or payload.get("start_date_iso")
            ),
            end_time=_parse_datetime(
                payload.get("endDate") or payload.get("endDateIso") or payload.get("end_date_iso")
            ),
            up_down_tokens=_up_down_tokens_from_gamma(payload),
            fee_rate=rate,
            fee_exponent=max(0.0, _to_float(raw_exponent) or 1.0),
            taker_only_fee=bool(fee_schedule.get("takerOnly", fee_schedule.get("to", True))),
            raw_payload=payload,
        )

    def get_up_down_tokens(
        self,
        market_id: str,
        asset_id: str,
        condition_id: str | None = None,
    ) -> tuple[OutcomeToken, OutcomeToken] | None:
        condition = condition_id or market_id
        if _looks_like_condition_id(condition):
            clob_payload = self._get_clob_market_by_condition_id(condition)
            pair = _up_down_tokens_from_clob(clob_payload)
            if pair:
                return pair
        payload = self._find_market(market_id, asset_id, condition_id)
        return _up_down_tokens_from_gamma(payload)

    @retry(wait=wait_exponential(multiplier=1, min=1, max=10), stop=stop_after_attempt(4))
    def _get_clob_market_by_condition_id(self, condition_id: str) -> dict[str, Any] | None:
        response = self.session.get(f"{self.clob_base_url}/markets/{condition_id}", timeout=self.timeout)
        if response.status_code in (400, 404, 422):
            return None
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else None

    def _find_market(self, market_id: str, asset_id: str, condition_id: str | None) -> dict[str, Any] | None:
        candidates: list[dict[str, Any] | None] = []
        if not _looks_like_condition_id(market_id):
            candidates.append(self._get_market_by_id(market_id))
        if condition_id:
            candidates.extend(self._get_markets(condition_ids=condition_id))
        candidates.extend(self._get_markets(condition_ids=market_id))
        candidates.extend(self._get_markets(clob_token_ids=asset_id))

        asset_text = str(asset_id)
        for candidate in candidates:
            if not candidate:
                continue
            token_ids = [str(item) for item in _decode_list(candidate.get("clobTokenIds"))]
            if asset_text in token_ids:
                return candidate
        return next((candidate for candidate in candidates if candidate), None)

    @retry(wait=wait_exponential(multiplier=1, min=1, max=10), stop=stop_after_attempt(4))
    def _get_market_by_id(self, market_id: str) -> dict[str, Any] | None:
        response = self.session.get(f"{self.base_url}/markets/{market_id}", timeout=self.timeout)
        if response.status_code in (400, 404, 422):
            return None
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else None

    @retry(wait=wait_exponential(multiplier=1, min=1, max=10), stop=stop_after_attempt(4))
    def _get_markets(self, **params: str) -> list[dict[str, Any]]:
        response = self.session.get(f"{self.base_url}/markets", params=params, timeout=self.timeout)
        if response.status_code in (400, 404, 422):
            return []
        response.raise_for_status()
        data = response.json()
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("data", "markets", "results"):
                if isinstance(data.get(key), list):
                    return [item for item in data[key] if isinstance(item, dict)]
        return []


def _market_resolution(payload: dict[str, Any]) -> MarketResolution:
    token_ids = [str(item) for item in _decode_list(payload.get("clobTokenIds"))]
    prices = [_to_float(item) for item in _decode_list(payload.get("outcomePrices"))]
    closed = bool(payload.get("closed") or payload.get("archived"))
    payout_by_token: dict[str, float] = {}

    if closed and token_ids and len(token_ids) == len(prices):
        for token_id, price in zip(token_ids, prices):
            if price is None:
                continue
            if price >= 0.999:
                payout_by_token[token_id] = 1.0
            elif price <= 0.001:
                payout_by_token[token_id] = 0.0

    resolved = closed and bool(payout_by_token)
    return MarketResolution(
        resolved=resolved,
        payout_by_token_id=payout_by_token,
        market_title=payload.get("question") or payload.get("title") or payload.get("slug"),
        raw_payload=payload,
    )


def _clob_market_resolution(payload: dict[str, Any]) -> MarketResolution:
    closed = bool(payload.get("closed") or payload.get("archived"))
    payout_by_token: dict[str, float] = {}
    tokens = payload.get("tokens")

    if closed and isinstance(tokens, list):
        for token in tokens:
            if not isinstance(token, dict):
                continue
            token_id = token.get("token_id") or token.get("tokenId")
            if token_id is None:
                continue
            if token.get("winner") is True:
                payout_by_token[str(token_id)] = 1.0
                continue
            if token.get("winner") is False:
                payout_by_token[str(token_id)] = 0.0
                continue
            price = _to_float(token.get("price"))
            if price is None:
                continue
            if price >= 0.999:
                payout_by_token[str(token_id)] = 1.0
            elif price <= 0.001:
                payout_by_token[str(token_id)] = 0.0

    return MarketResolution(
        resolved=closed and bool(payout_by_token),
        payout_by_token_id=payout_by_token,
        market_title=payload.get("question") or payload.get("market_slug") or payload.get("condition_id"),
        raw_payload=payload,
    )


def _up_down_tokens_from_clob(
    payload: dict[str, Any] | None,
) -> tuple[OutcomeToken, OutcomeToken] | None:
    if not payload or not isinstance(payload.get("tokens"), list):
        return None
    tokens = []
    for item in payload["tokens"]:
        if not isinstance(item, dict):
            return None
        token_id = item.get("token_id") or item.get("tokenId")
        outcome = item.get("outcome")
        if token_id is None or outcome is None:
            return None
        tokens.append(OutcomeToken(str(token_id), str(outcome)))
    return _validated_up_down_pair(tokens)


def _up_down_tokens_from_gamma(
    payload: dict[str, Any] | None,
) -> tuple[OutcomeToken, OutcomeToken] | None:
    if not payload:
        return None
    token_ids = [str(item) for item in _decode_list(payload.get("clobTokenIds"))]
    outcomes = [str(item) for item in _decode_list(payload.get("outcomes"))]
    if len(token_ids) != len(outcomes):
        return None
    return _validated_up_down_pair(
        [OutcomeToken(token_id, outcome) for token_id, outcome in zip(token_ids, outcomes)]
    )


def _validated_up_down_pair(tokens: list[OutcomeToken]) -> tuple[OutcomeToken, OutcomeToken] | None:
    if len(tokens) != 2:
        return None
    by_outcome = {token.outcome.strip().casefold(): token for token in tokens}
    if set(by_outcome) != {"up", "down"}:
        return None
    if by_outcome["up"].token_id == by_outcome["down"].token_id:
        return None
    return by_outcome["up"], by_outcome["down"]


def _decode_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            return []
        return decoded if isinstance(decoded, list) else []
    return []


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _looks_like_condition_id(value: str | None) -> bool:
    if not value:
        return False
    text = str(value)
    return text.startswith("0x") and len(text) > 20


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
