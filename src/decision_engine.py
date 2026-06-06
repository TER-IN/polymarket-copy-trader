from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from config import Settings
from db import Database
from funds import BalanceProvider, BalanceUnavailableError
from models import CopyDecision, CrowdingScore, SourceTokenStatus, TradeEvent, TradeSide, utc_now
from polymarket_clob import BookQuote, PublicClobClient


class MarketEndProvider(Protocol):
    def get_market_end_time(
        self,
        market_id: str,
        asset_id: str,
        condition_id: str | None = None,
    ) -> datetime | None:
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

    def decide(self, trade: TradeEvent, crowding_score: CrowdingScore | None = None) -> CopyDecision:
        decision_time = utc_now()
        details = {
            "decision_time": decision_time.isoformat(),
            "source_price": trade.price,
            "source_notional_usd": trade.notional_usd,
            "side": trade.side.value,
        }
        age = (decision_time - trade.timestamp.astimezone(timezone.utc)).total_seconds()
        details["trade_age_seconds"] = age
        if age > self.settings.max_trade_age_seconds:
            return _reject(f"trade too old: {age:.1f}s", details)
        if trade.notional_usd < self.settings.min_trade_usd:
            return _reject("below MIN_TRADE_USD", details)
        title = (trade.market_title or "").lower()
        for keyword in self.settings.block_market_keywords:
            if keyword.lower() in title:
                return _reject(f"blocked market keyword: {keyword}", details)
        if crowding_score and crowding_score.follower_count > self.settings.crowding_max_followers:
            return _reject("suspected copy pressure follower count exceeded", details)

        token_id = trade.asset_id or trade.token_id
        if not token_id:
            return _reject("missing token_id/asset_id", details)

        source_state = self.db.get_source_token_state_for_trade(trade)
        if source_state and source_state["status"] == SourceTokenStatus.PRE_EXISTING.value:
            return _reject("source held token before bot startup", details)
        if source_state and source_state["status"] == SourceTokenStatus.FROZEN.value:
            reason = source_state["freeze_reason"] or "risk mismatch"
            return _reject(f"source token frozen: {reason}", details)

        position = None
        if trade.side == TradeSide.SELL:
            if not source_state or source_state["status"] != SourceTokenStatus.CLEAN.value:
                return _reject("source token lifecycle not tracked from clean entry", details)
            if source_state["observed_source_shares"] <= 0:
                return _reject("source observed position is zero", details)
            position = self.db.get_position(trade_market_key(trade), token_id, trade.outcome or "")
            if not position or position.total_shares <= 0:
                return _reject("no copied position to sell", details)

        quote = self.clob_client.get_quote(token_id)
        executable = quote.executable_price(trade.side)
        if executable is None:
            return _reject("insufficient liquidity/no executable quote", details)
        allowed = _allowed_price(trade.side, trade.price, self.settings.max_slippage_cents)
        slippage_cents = round(
            (
                (executable - trade.price) * 100
                if trade.side == TradeSide.BUY
                else (trade.price - executable) * 100
            ),
            8,
        )
        details.update(
            {
                "executable_price": executable,
                "allowed_slippage_price": allowed,
                "slippage_cents": slippage_cents,
                "max_slippage_cents": self.settings.max_slippage_cents,
            }
        )

        if not passes_slippage(trade.side, trade.price, executable, self.settings.max_slippage_cents):
            return _reject(
                (
                    f"slippage check failed: source={trade.price:.4f} executable={executable:.4f} "
                    f"difference={slippage_cents:.2f}c allowed={allowed:.4f}"
                ),
                details,
                current_price=executable,
                allowed_price=allowed,
            )

        copy_shares = None
        reason = "copy allowed"
        if trade.side == TradeSide.SELL and position and source_state:
            sell_fraction = min(1.0, trade.size / source_state["observed_source_shares"])
            copy_shares = min(position.total_shares, position.total_shares * sell_fraction)
            copy_notional = copy_shares * executable
            if copy_shares <= 0:
                return _reject("no copied shares available to sell", details)
            reason = f"copy allowed; sell sized by source position ratio {sell_fraction:.4f}"

        if trade.side == TradeSide.BUY:
            if self.settings.max_buy_price is not None:
                details["max_buy_price"] = self.settings.max_buy_price
                if executable > self.settings.max_buy_price:
                    return _reject(
                        (
                            f"maximum buy price exceeded: source={trade.price:.4f} "
                            f"executable={executable:.4f} maximum={self.settings.max_buy_price:.4f}"
                        ),
                        details,
                        current_price=executable,
                        allowed_price=allowed,
                    )

            if self.settings.max_seconds_until_market_end is not None:
                details["max_seconds_until_market_end"] = self.settings.max_seconds_until_market_end
                if not self.market_end_provider:
                    return _reject("market end time unavailable: metadata provider not configured", details)
                try:
                    market_end = self.market_end_provider.get_market_end_time(
                        trade_market_key(trade),
                        token_id,
                        trade.condition_id,
                    )
                except Exception as exc:
                    details["market_end_error"] = str(exc)
                    return _reject(f"market end time unavailable: {exc}", details)
                if market_end is None:
                    return _reject("market end time unavailable", details)
                seconds_until_end = (market_end - decision_time).total_seconds()
                details["market_end_time"] = market_end.isoformat()
                details["seconds_until_market_end"] = seconds_until_end
                if seconds_until_end <= 0:
                    return _reject("market end time has already passed", details)
                if seconds_until_end > self.settings.max_seconds_until_market_end:
                    return _reject(
                        (
                            f"market ends too late: {seconds_until_end:.0f}s remaining; "
                            f"maximum={self.settings.max_seconds_until_market_end}s"
                        ),
                        details,
                    )

            position = self.db.get_position(trade_market_key(trade), token_id, trade.outcome or "")
            current_exposure = position.total_cost if position else 0.0
            base_copy_notional = trade.notional_usd * self.settings.copy_ratio
            caps = [base_copy_notional]
            if self.settings.max_trade_usd is not None:
                caps.append(self.settings.max_trade_usd)
            remaining_daily = self.settings.daily_spend_cap_usd - self.db.spend_today(
                self.settings.trading_day_timezone
            )
            remaining_exposure = self.settings.per_market_exposure_cap_usd - current_exposure
            caps.extend([remaining_daily, remaining_exposure])
            if self.balance_provider:
                try:
                    available_balance = self.balance_provider.available_balance_usd()
                except BalanceUnavailableError as exc:
                    details["balance_error"] = str(exc)
                    return _reject(str(exc), details)
                details["available_balance_usd"] = available_balance
                caps.append(available_balance)
            copy_notional = min(caps)
            details.update(
                {
                    "base_copy_notional_usd": base_copy_notional,
                    "remaining_daily_cap_usd": remaining_daily,
                    "remaining_market_exposure_usd": remaining_exposure,
                    "copy_notional_usd": copy_notional,
                }
            )
            if remaining_daily <= 0:
                return _reject("daily spend cap exhausted", details)
            if remaining_exposure <= 0:
                return _reject("per-market exposure cap exhausted", details)
            if self.balance_provider and details["available_balance_usd"] <= 0:
                return _reject("available balance exhausted", details)
            if copy_notional <= 0:
                return _reject("copy size capped to zero", details)
            copy_shares = copy_notional / executable if executable > 0 else None
            if copy_notional < base_copy_notional:
                reason = "copy allowed; buy capped by risk limits"

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
            details=details,
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
