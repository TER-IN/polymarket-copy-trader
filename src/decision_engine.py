from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

from config import Settings
from db import Database
from funds import BalanceProvider, BalanceUnavailableError
from models import (
    CopyDecision,
    CrowdingScore,
    MarketTypeFilter,
    RiskMismatchScope,
    SourceTokenStatus,
    TradeEvent,
    TradeSide,
    utc_now,
)
from polymarket_clob import BookFill, ExecutionEstimate, PublicClobClient
from polymarket_gamma import MarketMetadata


class MarketEndProvider(Protocol):
    def get_market_end_time(
        self,
        market_id: str,
        asset_id: str,
        condition_id: str | None = None,
    ) -> datetime | None:
        ...

    def get_market_metadata(
        self,
        market_id: str,
        asset_id: str,
        condition_id: str | None = None,
    ) -> MarketMetadata | None:
        ...


def calculate_copy_size(notional_usd: float, copy_ratio: float, max_trade_usd: float | None) -> float:
    size = notional_usd * copy_ratio
    if max_trade_usd is not None:
        size = min(size, max_trade_usd)
    return max(0.0, size)


def allowed_buy_price(original_price: float, max_slippage_cents: float) -> float:
    return min(1.0, original_price + (max_slippage_cents / 100.0))


def allowed_sell_price(original_price: float, max_slippage_cents: float) -> float:
    return max(0.0, original_price - (max_slippage_cents / 100.0))


def passes_slippage(side: TradeSide, original_price: float, current_price: float, max_slippage_cents: float) -> bool:
    if side == TradeSide.BUY:
        return current_price <= allowed_buy_price(original_price, max_slippage_cents)
    return current_price >= allowed_sell_price(original_price, max_slippage_cents)


def trade_market_key(trade: TradeEvent) -> str:
    return trade.market_id or trade.condition_id or ""


class DecisionEngine:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        clob_client: PublicClobClient,
        balance_provider: BalanceProvider | None = None,
        market_end_provider: MarketEndProvider | None = None,
    ):
        self.settings = settings
        self.db = db
        self.clob_client = clob_client
        self.balance_provider = balance_provider
        self.market_end_provider = market_end_provider

    def decide(
        self,
        trade: TradeEvent,
        crowding_score: CrowdingScore | None = None,
        source_trade: TradeEvent | None = None,
    ) -> CopyDecision:
        source_trade = source_trade or trade
        decision_time = utc_now()
        details = {
            "decision_time": decision_time.isoformat(),
            "source_price": source_trade.price,
            "reference_price": trade.price,
            "source_outcome": source_trade.outcome,
            "copied_outcome": trade.outcome,
            "source_asset_id": source_trade.asset_id or source_trade.token_id,
            "copied_asset_id": trade.asset_id or trade.token_id,
            "outcome_selection_mode": (
                trade.raw_payload.get("_outcome_selection", {}).get("mode", "source")
            ),
            "source_notional_usd": source_trade.notional_usd,
            "side": trade.side.value,
            "strategy_config": {
                "copy_ratio": self.settings.copy_ratio,
                "max_trade_usd": self.settings.max_trade_usd,
                "max_slippage_cents": self.settings.max_slippage_cents,
                "max_buy_price": self.settings.max_buy_price,
                "max_seconds_until_market_end": self.settings.max_seconds_until_market_end,
                "market_type_filter": self.settings.market_type_filter.value,
                "up_down_min_duration_seconds": self.settings.up_down_min_duration_seconds,
                "up_down_max_duration_seconds": self.settings.up_down_max_duration_seconds,
                "min_net_upside_usd": self.settings.min_net_upside_usd,
                "min_net_upside_percent": self.settings.min_net_upside_percent,
                "net_upside_safety_margin_usd": self.settings.net_upside_safety_margin_usd,
                "include_exit_fee_in_upside": self.settings.include_exit_fee_in_upside,
                "daily_spend_cap_usd": self.settings.daily_spend_cap_usd,
                "per_market_exposure_cap_usd": self.settings.per_market_exposure_cap_usd,
                "outcome_selection_mode": self.settings.outcome_selection_mode.value,
                "allow_market_title_keywords": self.settings.allow_market_title_keywords,
                "risk_mismatch_scope": self.settings.risk_mismatch_scope.value,
            },
        }
        age = (decision_time - source_trade.timestamp.astimezone(timezone.utc)).total_seconds()
        details["trade_age_seconds"] = age
        if age > self.settings.max_trade_age_seconds:
            return _reject(f"trade too old: {age:.1f}s", details)
        if source_trade.notional_usd < self.settings.min_trade_usd:
            return _reject("below MIN_TRADE_USD", details)
        title = (trade.market_title or "").lower()
        if (
            trade.side == TradeSide.BUY
            and self.settings.allow_market_title_keywords
            and not any(
                keyword.lower() in title
                for keyword in self.settings.allow_market_title_keywords
            )
        ):
            return _reject(
                "market title does not match allowed keywords: "
                + ", ".join(self.settings.allow_market_title_keywords),
                details,
            )
        for keyword in self.settings.block_market_keywords:
            if keyword.lower() in title:
                return _reject(f"blocked market keyword: {keyword}", details)
        if crowding_score and crowding_score.follower_count > self.settings.crowding_max_followers:
            return _reject("suspected copy pressure follower count exceeded", details)

        token_id = trade.asset_id or trade.token_id
        if not token_id:
            return _reject("missing token_id/asset_id", details)

        if (
            trade.side == TradeSide.BUY
            and self.settings.risk_mismatch_scope == RiskMismatchScope.WALLET_MARKET
        ):
            frozen_market_state = self.db.get_frozen_source_market_state_for_trade(source_trade)
            if frozen_market_state:
                reason = frozen_market_state["freeze_reason"] or "risk mismatch"
                details["market_freeze"] = {
                    "asset_id": frozen_market_state["asset_id"],
                    "outcome": frozen_market_state["outcome"],
                    "reason": reason,
                }
                return _reject(f"source wallet market frozen: {reason}", details)

        source_state = self.db.get_source_token_state_for_trade(source_trade)
        if source_state and source_state["status"] == SourceTokenStatus.PRE_EXISTING.value:
            return _reject("source held token before bot startup", details)
        if (
            source_state
            and source_state["status"] == SourceTokenStatus.FROZEN.value
            and not (
                trade.side == TradeSide.SELL
                and self.settings.risk_mismatch_scope == RiskMismatchScope.WALLET_MARKET
            )
        ):
            reason = source_state["freeze_reason"] or "risk mismatch"
            return _reject(f"source token frozen: {reason}", details)

        position = None
        if trade.side == TradeSide.SELL:
            allowed_sell_states = {SourceTokenStatus.CLEAN.value}
            if self.settings.risk_mismatch_scope == RiskMismatchScope.WALLET_MARKET:
                allowed_sell_states.add(SourceTokenStatus.FROZEN.value)
            if not source_state or source_state["status"] not in allowed_sell_states:
                return _reject("source token lifecycle not tracked from clean entry", details)
            if source_state["observed_source_shares"] <= 0:
                return _reject("source observed position is zero", details)
            position = self.db.get_position(trade_market_key(trade), token_id, trade.outcome or "")
            if not position or position.total_shares <= 0:
                return _reject("no copied position to sell", details)

        allowed = _allowed_price(trade.side, trade.price, self.settings.max_slippage_cents)
        details.update(
            {
                "allowed_slippage_price": allowed,
                "max_slippage_cents": self.settings.max_slippage_cents,
            }
        )

        metadata = None
        if trade.side == TradeSide.BUY:
            metadata = self._market_metadata(trade, token_id, details)
            if isinstance(metadata, CopyDecision):
                return metadata
        fee_rate = metadata.fee_rate if metadata else 0.0
        fee_exponent = metadata.fee_exponent if metadata else 1.0
        if fee_rate <= 0 and hasattr(self.clob_client, "get_fee_rate"):
            try:
                fee_rate = self.clob_client.get_fee_rate(token_id)
            except Exception as exc:
                details["fee_rate_error"] = str(exc)
        details.update({"fee_rate": fee_rate, "fee_exponent": fee_exponent})

        requested_notional = 0.0
        requested_shares = 0.0
        all_in_limit = None
        reason = "copy allowed"
        if trade.side == TradeSide.SELL and position and source_state:
            sell_fraction = min(1.0, source_trade.size / source_state["observed_source_shares"])
            requested_shares = min(position.total_shares, position.total_shares * sell_fraction)
            if requested_shares <= 0:
                return _reject("no copied shares available to sell", details)
            reason = f"copy allowed; sell sized by source position ratio {sell_fraction:.4f}"

        if trade.side == TradeSide.BUY:
            position = self.db.get_position(trade_market_key(trade), token_id, trade.outcome or "")
            current_exposure = position.total_cost if position else 0.0
            base_copy_notional = source_trade.notional_usd * self.settings.copy_ratio
            caps = [base_copy_notional]
            if self.settings.max_trade_usd is not None:
                caps.append(self.settings.max_trade_usd)
            remaining_daily = None
            if self.settings.daily_spend_cap_usd is not None:
                remaining_daily = self.settings.daily_spend_cap_usd - self.db.spend_today(
                    self.settings.trading_day_timezone
                )
                caps.append(remaining_daily)
            remaining_exposure = self.settings.per_market_exposure_cap_usd - current_exposure
            caps.append(remaining_exposure)
            if self.balance_provider:
                try:
                    available_balance = self.balance_provider.available_balance_usd()
                except BalanceUnavailableError as exc:
                    details["balance_error"] = str(exc)
                    return _reject(str(exc), details)
                details["available_balance_usd"] = available_balance
                caps.append(available_balance)
            requested_notional = min(caps)
            all_in_limits = [remaining_exposure]
            if remaining_daily is not None:
                all_in_limits.append(remaining_daily)
            if self.balance_provider:
                all_in_limits.append(float(details["available_balance_usd"]))
            all_in_limit = min(all_in_limits)
            details.update(
                {
                    "base_copy_notional_usd": base_copy_notional,
                    "remaining_daily_cap_usd": remaining_daily,
                    "remaining_market_exposure_usd": remaining_exposure,
                    "requested_copy_notional_usd": requested_notional,
                }
            )
            if remaining_daily is not None and remaining_daily <= 0:
                return _reject("daily spend cap exhausted", details)
            if remaining_exposure <= 0:
                return _reject("per-market exposure cap exhausted", details)
            if self.balance_provider and details["available_balance_usd"] <= 0:
                return _reject("available balance exhausted", details)
            if requested_notional <= 0:
                return _reject("copy size capped to zero", details)
            if requested_notional < base_copy_notional:
                reason = "copy allowed; buy capped by risk limits"

        execution_limit = allowed
        if trade.side == TradeSide.BUY and self.settings.max_buy_price is not None:
            details["max_buy_price"] = self.settings.max_buy_price
            execution_limit = min(execution_limit, self.settings.max_buy_price)
        estimate = self._estimate_execution(
            token_id,
            trade.side,
            requested_notional,
            requested_shares,
            execution_limit,
            fee_rate,
            fee_exponent,
        )
        if (
            trade.side == TradeSide.BUY
            and all_in_limit is not None
            and estimate.filled_notional_usd + estimate.estimated_fee_usd
            > all_in_limit
        ):
            affordable_notional = max(
                0.0,
                requested_notional
                - (
                    estimate.filled_notional_usd
                    + estimate.estimated_fee_usd
                    - all_in_limit
                ),
            )
            estimate = self._estimate_execution(
                token_id,
                trade.side,
                affordable_notional,
                0.0,
                execution_limit,
                fee_rate,
                fee_exponent,
            )
        details["execution_estimate"] = _estimate_details(estimate)
        executable = estimate.average_price
        if executable is None or estimate.filled_shares <= 0:
            quote = self.clob_client.get_quote(token_id)
            top_price = quote.executable_price(trade.side)
            details["top_of_book_price"] = top_price
            if (
                trade.side == TradeSide.BUY
                and self.settings.max_buy_price is not None
                and top_price is not None
                and top_price > self.settings.max_buy_price
            ):
                details["executable_price"] = top_price
                return _reject(
                    (
                        f"maximum buy price exceeded: source={source_trade.price:.4f} "
                        f"executable={top_price:.4f} maximum={self.settings.max_buy_price:.4f}"
                    ),
                    details,
                    current_price=top_price,
                    allowed_price=allowed,
                )
            if top_price is not None and not passes_slippage(
                trade.side,
                trade.price,
                top_price,
                self.settings.max_slippage_cents,
            ):
                slippage_cents = (
                    (top_price - trade.price) * 100
                    if trade.side == TradeSide.BUY
                    else (trade.price - top_price) * 100
                )
                details.update(
                    {"executable_price": top_price, "slippage_cents": round(slippage_cents, 8)}
                )
                return _reject(
                    (
                        f"slippage check failed: source={source_trade.price:.4f} "
                        f"executable={top_price:.4f} difference={slippage_cents:.2f}c "
                        f"allowed={allowed:.4f}"
                    ),
                    details,
                    current_price=top_price,
                    allowed_price=allowed,
                )
            return _reject(
                "insufficient order-book depth within slippage/price limits",
                details,
                current_price=top_price,
                allowed_price=allowed,
            )

        slippage_cents = round(
            (executable - trade.price) * 100
            if trade.side == TradeSide.BUY
            else (trade.price - executable) * 100,
            8,
        )
        details.update(
            {
                "executable_price": executable,
                "worst_execution_price": estimate.worst_price,
                "slippage_cents": slippage_cents,
                "fill_ratio": estimate.fill_ratio,
                "estimated_fee_usd": estimate.estimated_fee_usd,
            }
        )
        if estimate.fill_ratio < 1:
            reason += f"; partial book fill {estimate.fill_ratio:.1%}"

        copy_notional = estimate.filled_notional_usd
        copy_shares = estimate.filled_shares
        if trade.side == TradeSide.BUY:
            all_in_cost = copy_notional + estimate.estimated_fee_usd
            assumed_exit_fee = estimate.estimated_fee_usd if self.settings.include_exit_fee_in_upside else 0.0
            net_upside = (
                copy_shares
                - all_in_cost
                - assumed_exit_fee
                - self.settings.net_upside_safety_margin_usd
            )
            net_upside_percent = (net_upside / all_in_cost * 100) if all_in_cost > 0 else 0.0
            details.update(
                {
                    "maximum_net_upside_usd": net_upside,
                    "maximum_net_upside_percent": net_upside_percent,
                    "assumed_exit_fee_usd": assumed_exit_fee,
                    "net_upside_safety_margin_usd": self.settings.net_upside_safety_margin_usd,
                }
            )
            if self.settings.min_net_upside_usd is not None and net_upside < self.settings.min_net_upside_usd:
                return _reject(
                    f"maximum net upside ${net_upside:.2f} below minimum ${self.settings.min_net_upside_usd:.2f}",
                    details,
                    current_price=executable,
                    allowed_price=allowed,
                )
            if (
                self.settings.min_net_upside_percent is not None
                and net_upside_percent < self.settings.min_net_upside_percent
            ):
                return _reject(
                    (
                        f"maximum net upside {net_upside_percent:.2f}% below minimum "
                        f"{self.settings.min_net_upside_percent:.2f}%"
                    ),
                    details,
                    current_price=executable,
                    allowed_price=allowed,
                )

        details["copy_notional_usd"] = copy_notional
        details["copy_shares"] = copy_shares
        return CopyDecision(
            True,
            reason,
            copy_notional_usd=copy_notional,
            copy_shares=copy_shares,
            current_price=executable,
            allowed_price=allowed,
            reduce_only=trade.side == TradeSide.SELL,
            estimated_fee_usd=estimate.estimated_fee_usd,
            fill_ratio=estimate.fill_ratio,
            details=details,
        )

    def _market_metadata(
        self,
        trade: TradeEvent,
        token_id: str,
        details: dict[str, Any],
    ) -> MarketMetadata | CopyDecision | None:
        needs_metadata = (
            self.settings.max_seconds_until_market_end is not None
            or self.settings.market_type_filter != MarketTypeFilter.ALL
        )
        if not self.market_end_provider:
            return (
                _reject("market metadata unavailable: provider not configured", details)
                if needs_metadata
                else None
            )
        metadata = None
        if hasattr(self.market_end_provider, "get_market_metadata"):
            try:
                metadata = self.market_end_provider.get_market_metadata(
                    trade_market_key(trade), token_id, trade.condition_id
                )
            except Exception as exc:
                details["market_metadata_error"] = str(exc)
                if needs_metadata:
                    return _reject(f"market metadata unavailable: {exc}", details)
        if metadata:
            details["market_metadata"] = {
                "start_time": metadata.start_time.isoformat() if metadata.start_time else None,
                "end_time": metadata.end_time.isoformat() if metadata.end_time else None,
                "duration_seconds": metadata.duration_seconds,
                "up_down_tokens": (
                    [
                        {"token_id": token.token_id, "outcome": token.outcome}
                        for token in metadata.up_down_tokens
                    ]
                    if metadata.up_down_tokens
                    else None
                ),
                "fee_rate": metadata.fee_rate,
                "fee_exponent": metadata.fee_exponent,
                "taker_only_fee": metadata.taker_only_fee,
                "raw": metadata.raw_payload,
            }
        end_time = metadata.end_time if metadata else None
        if end_time is None and self.settings.max_seconds_until_market_end is not None:
            try:
                end_time = self.market_end_provider.get_market_end_time(
                    trade_market_key(trade), token_id, trade.condition_id
                )
            except Exception as exc:
                details["market_end_error"] = str(exc)
                return _reject(f"market end time unavailable: {exc}", details)
        if end_time:
            seconds_until_end = (end_time - utc_now()).total_seconds()
            details.update(
                {"market_end_time": end_time.isoformat(), "seconds_until_market_end": seconds_until_end}
            )
            if seconds_until_end <= 0:
                return _reject("market end time has already passed", details)
            if (
                self.settings.max_seconds_until_market_end is not None
                and seconds_until_end > self.settings.max_seconds_until_market_end
            ):
                return _reject(
                    (
                        f"market ends too late: {seconds_until_end:.0f}s remaining; "
                        f"maximum={self.settings.max_seconds_until_market_end}s"
                    ),
                    details,
                )
        elif self.settings.max_seconds_until_market_end is not None:
            return _reject("market end time unavailable", details)

        if self.settings.market_type_filter == MarketTypeFilter.SHORT_DURATION_UP_DOWN:
            if not metadata or not metadata.up_down_tokens:
                return _reject("market is not an authoritative two-outcome Up/Down market", details)
            duration = metadata.duration_seconds
            details["market_duration_seconds"] = duration
            if duration is None:
                return _reject("market duration unavailable", details)
            if not (
                self.settings.up_down_min_duration_seconds
                <= duration
                <= self.settings.up_down_max_duration_seconds
            ):
                return _reject(
                    (
                        f"Up/Down market duration {duration:.0f}s outside configured "
                        f"{self.settings.up_down_min_duration_seconds}-"
                        f"{self.settings.up_down_max_duration_seconds}s range"
                    ),
                    details,
                )
        return metadata

    def _estimate_execution(
        self,
        token_id: str,
        side: TradeSide,
        requested_notional: float,
        requested_shares: float,
        limit_price: float,
        fee_rate: float,
        fee_exponent: float,
    ) -> ExecutionEstimate:
        if hasattr(self.clob_client, "estimate_execution"):
            return self.clob_client.estimate_execution(
                token_id,
                side,
                requested_notional_usd=requested_notional,
                requested_shares=requested_shares,
                limit_price=limit_price,
                fee_rate=fee_rate,
                fee_exponent=fee_exponent,
            )
        quote = self.clob_client.get_quote(token_id)
        price = quote.executable_price(side)
        if price is None or not passes_slippage(side, limit_price, price, 0):
            fills: tuple[BookFill, ...] = ()
        elif side == TradeSide.BUY:
            shares = requested_notional / price if price > 0 else 0.0
            fills = (BookFill(price, shares, requested_notional),)
        else:
            fills = (BookFill(price, requested_shares, requested_shares * price),)
        filled_shares = sum(fill.shares for fill in fills)
        filled_notional = sum(fill.notional_usd for fill in fills)
        fee = sum(
            fill.shares * fee_rate * (fill.price * (1.0 - fill.price)) ** fee_exponent
            for fill in fills
        )
        return ExecutionEstimate(
            token_id,
            side,
            requested_notional,
            requested_shares,
            filled_notional,
            filled_shares,
            filled_notional / filled_shares if filled_shares else None,
            fills[-1].price if fills else None,
            1.0 if fills else 0.0,
            fee,
            fee_rate,
            fee_exponent,
            fills,
            quote.raw,
        )


def _allowed_price(side: TradeSide, original_price: float, max_slippage_cents: float) -> float:
    if side == TradeSide.BUY:
        return allowed_buy_price(original_price, max_slippage_cents)
    return allowed_sell_price(original_price, max_slippage_cents)


def _reject(
    reason: str,
    details: dict,
    current_price: float | None = None,
    allowed_price: float | None = None,
) -> CopyDecision:
    return CopyDecision(
        False,
        reason,
        current_price=current_price,
        allowed_price=allowed_price,
        details=details,
    )


def _estimate_details(estimate: ExecutionEstimate) -> dict[str, Any]:
    return {
        "requested_notional_usd": estimate.requested_notional_usd,
        "requested_shares": estimate.requested_shares,
        "filled_notional_usd": estimate.filled_notional_usd,
        "filled_shares": estimate.filled_shares,
        "average_price": estimate.average_price,
        "worst_price": estimate.worst_price,
        "fill_ratio": estimate.fill_ratio,
        "estimated_fee_usd": estimate.estimated_fee_usd,
        "fee_rate": estimate.fee_rate,
        "fee_exponent": estimate.fee_exponent,
        "fills": [
            {"price": fill.price, "shares": fill.shares, "notional_usd": fill.notional_usd}
            for fill in estimate.fills
        ],
        "book": estimate.raw_book,
    }
