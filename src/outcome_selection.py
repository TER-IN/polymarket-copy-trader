from __future__ import annotations

from dataclasses import replace
from typing import Protocol

from models import OutcomeSelectionMode, TradeEvent, TradeSide
from polymarket_gamma import OutcomeToken


class OutcomeSelectionError(RuntimeError):
    pass


class OutcomeSelectionSkip(RuntimeError):
    pass


class UpDownTokenProvider(Protocol):
    def get_up_down_tokens(
        self,
        market_id: str,
        asset_id: str,
        condition_id: str | None = None,
    ) -> tuple[OutcomeToken, OutcomeToken] | None:
        ...


class OutcomeSelector:
    def __init__(
        self,
        mode: OutcomeSelectionMode,
        token_provider: UpDownTokenProvider,
        inverse_down_max_source_price: float = 0.5,
    ):
        self.mode = mode
        self.token_provider = token_provider
        self.inverse_down_max_source_price = inverse_down_max_source_price
        self._cache: dict[str, tuple[OutcomeToken, OutcomeToken]] = {}

    def select(self, source_trade: TradeEvent) -> TradeEvent:
        if self.mode == OutcomeSelectionMode.SOURCE:
            return source_trade
        if self.mode == OutcomeSelectionMode.INVERSE_DOWN_UNDERDOG:
            if (source_trade.outcome or "").casefold() != "down":
                raise OutcomeSelectionSkip(
                    "inverse Down-underdog signal skipped: source outcome is not Down"
                )
            if (
                source_trade.side == TradeSide.BUY
                and source_trade.price >= self.inverse_down_max_source_price
            ):
                raise OutcomeSelectionSkip(
                    "inverse Down-underdog signal skipped: "
                    f"source price {source_trade.price:.4f} is not below "
                    f"{self.inverse_down_max_source_price:.4f}"
                )

        market_id = source_trade.market_id or source_trade.condition_id
        source_token_id = source_trade.asset_id or source_trade.token_id
        if not market_id or not source_token_id:
            raise OutcomeSelectionError("inverse outcome unavailable: missing market or token id")

        cache_key = source_trade.condition_id or market_id
        pair = self._cache.get(cache_key)
        if pair is None:
            pair = self.token_provider.get_up_down_tokens(
                market_id,
                source_token_id,
                source_trade.condition_id,
            )
            if pair is None:
                raise OutcomeSelectionError(
                    "inverse outcome unavailable: market is not an authoritative two-outcome Up/Down pair"
                )
            self._cache[cache_key] = pair

        up, down = pair
        by_token = {up.token_id: up, down.token_id: down}
        source_token = by_token.get(str(source_token_id))
        if source_token is None:
            raise OutcomeSelectionError(
                "inverse outcome unavailable: source token does not belong to the Up/Down pair"
            )
        target_token = down if source_token.outcome.casefold() == "up" else up
        inverse_price = max(0.0, min(1.0, 1.0 - source_trade.price))
        selection = {
            "mode": self.mode.value,
            "source_asset_id": str(source_token_id),
            "source_outcome": source_trade.outcome,
            "source_price": source_trade.price,
            "copied_asset_id": target_token.token_id,
            "copied_outcome": target_token.outcome,
            "reference_price": inverse_price,
        }
        return replace(
            source_trade,
            asset_id=target_token.token_id,
            token_id=target_token.token_id,
            outcome=target_token.outcome,
            price=inverse_price,
            raw_payload=source_trade.raw_payload | {"_outcome_selection": selection},
        )
