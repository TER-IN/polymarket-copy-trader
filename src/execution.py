from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from config import Settings
from db import Database
from models import CopyDecision, CopyMode, ExecutionResult, TradeEvent, TradeSide
from positions import apply_buy, apply_sell


class ExecutionError(RuntimeError):
    pass


class Executor:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db

    def execute(
        self,
        trade: TradeEvent,
        decision: CopyDecision,
        source_trade_key: str | None = None,
    ) -> ExecutionResult:
        if not decision.should_copy:
            return ExecutionResult(False, "rejected")
        source_trade_key = source_trade_key or trade.dedupe_key
        if self.settings.stop_trading_file.exists():
            self.db.record_order(
                source_trade_key,
                trade,
                decision.copy_notional_usd,
                None,
                decision.allowed_price,
                "blocked",
                estimated_fee_usd=decision.estimated_fee_usd,
                quote_snapshot=decision.details.get("execution_estimate"),
                error_message="STOP_TRADING file exists",
            )
            return ExecutionResult(False, "blocked")
        if self.settings.copy_mode == CopyMode.DRY_RUN:
            return self._dry_run(trade, decision, source_trade_key)
        return self._live(trade, decision, source_trade_key)

    def _dry_run(
        self, trade: TradeEvent, decision: CopyDecision, source_trade_key: str
    ) -> ExecutionResult:
        shares = decision.copy_shares
        if shares is None:
            shares = _shares_from_notional(decision.copy_notional_usd, decision.current_price or trade.price)
        self.db.record_order(
            source_trade_key,
            trade,
            decision.copy_notional_usd,
            shares,
            decision.allowed_price,
            "dry_run",
            filled_shares=shares,
            filled_notional_usd=decision.copy_notional_usd,
            avg_fill_price=decision.current_price,
            estimated_fee_usd=decision.estimated_fee_usd,
            quote_snapshot=decision.details.get("execution_estimate"),
            raw_response={
                "message": "dry run; no order submitted",
                "outcome_selection": trade.raw_payload.get("_outcome_selection"),
            },
        )
        self._update_position_from_fill(
            trade,
            shares,
            decision.current_price or trade.price,
            decision.estimated_fee_usd,
        )
        return ExecutionResult(
            True,
            "dry_run",
            shares,
            decision.copy_notional_usd,
            decision.estimated_fee_usd,
        )

    def _live(
        self, trade: TradeEvent, decision: CopyDecision, source_trade_key: str
    ) -> ExecutionResult:
        try:
            response = self._submit_live_order(trade, decision)
            response = self._reconcile_live_response(response)
            status = str(response.get("status") or response.get("state") or "submitted")
            order_id = response.get("orderID") or response.get("order_id") or response.get("id")
            filled_shares = float(
                response.get("filledSize")
                or response.get("filled_size")
                or response.get("size_matched")
                or 0
            )
            avg_price = response.get("avgPrice") or response.get("avg_fill_price") or response.get("price")
            avg_fill_price = float(avg_price) if avg_price is not None else None
            filled_notional = filled_shares * avg_fill_price if avg_fill_price else 0.0
            actual_fee = float(
                response.get("fee_usd")
                or response.get("feeUsd")
                or response.get("actual_fee_usd")
                or 0
            )
            self.db.record_order(
                source_trade_key,
                trade,
                decision.copy_notional_usd,
                None,
                decision.allowed_price,
                status,
                clob_order_id=order_id,
                filled_shares=filled_shares,
                filled_notional_usd=filled_notional,
                avg_fill_price=avg_fill_price,
                estimated_fee_usd=decision.estimated_fee_usd,
                actual_fee_usd=actual_fee,
                quote_snapshot=decision.details.get("execution_estimate"),
                raw_response=response
                | {"outcome_selection": trade.raw_payload.get("_outcome_selection")},
            )
            if filled_shares and avg_fill_price:
                self._update_position_from_fill(
                    trade,
                    filled_shares,
                    avg_fill_price,
                    actual_fee or decision.estimated_fee_usd,
                )
            accepted = status.lower() not in {"failed", "rejected", "cancelled", "canceled", "unmatched"}
            return ExecutionResult(
                accepted,
                status,
                filled_shares,
                filled_notional,
                actual_fee or decision.estimated_fee_usd,
            )
        except Exception as exc:
            self.db.record_order(
                source_trade_key,
                trade,
                decision.copy_notional_usd,
                None,
                decision.allowed_price,
                "failed",
                estimated_fee_usd=decision.estimated_fee_usd,
                quote_snapshot=decision.details.get("execution_estimate"),
                error_message=str(exc),
            )
            raise

    def _reconcile_live_response(self, response: dict[str, Any]) -> dict[str, Any]:
        order_id = response.get("orderID") or response.get("order_id") or response.get("id")
        if not order_id:
            return response
        client = getattr(self, "_live_client", None)
        if client is None or not hasattr(client, "get_order"):
            return response
        try:
            reconciled = client.get_order(order_id)
        except Exception:
            return response
        return response | reconciled if isinstance(reconciled, dict) else response

    def _submit_live_order(self, trade: TradeEvent, decision: CopyDecision) -> dict[str, Any]:
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
        except ImportError as exc:
            raise ExecutionError("Install live extras first: uv sync --extra live") from exc

        token_id = trade.asset_id or trade.token_id
        if not token_id:
            raise ExecutionError("missing token id")

        client = getattr(self, "_live_client", None)
        if client is None:
            client = ClobClient(
                self.settings.clob_base_url,
                key=self.settings.polymarket_private_key,
                chain_id=self.settings.chain_id,
                signature_type=self.settings.polymarket_signature_type,
                funder=self.settings.polymarket_funder,
            )
            client.set_api_creds(client.create_or_derive_api_creds())
            self._live_client = client

        if trade.side == TradeSide.BUY:
            args = MarketOrderArgs(
                token_id=token_id,
                amount=decision.copy_notional_usd,
                side="BUY",
                price=decision.allowed_price,
            )
        else:
            shares = decision.copy_shares
            if shares is None:
                raise ExecutionError("missing reduce-only sell share amount")
            args = MarketOrderArgs(token_id=token_id, amount=shares, side="SELL", price=decision.allowed_price)

        signed_order = client.create_market_order(args)
        response = client.post_order(signed_order, OrderType.FAK)
        return response if isinstance(response, dict) else {"raw": str(response)}

    def _update_position_from_fill(
        self, trade: TradeEvent, shares: float, price: float, fee_usd: float = 0.0
    ) -> None:
        token_id = trade.asset_id or trade.token_id
        market_id = trade.market_id or trade.condition_id
        outcome = trade.outcome or ""
        if not token_id or not market_id or shares <= 0:
            return
        existing = self.db.get_position(market_id, token_id, outcome)
        if trade.side == TradeSide.BUY:
            position = apply_buy(
                existing,
                market_id,
                token_id,
                outcome,
                shares,
                price,
                trade.source_wallet,
                fee_usd,
            )
            self.db.upsert_position(position)
        elif existing:
            position = apply_sell(existing, shares, price, fee_usd)
            self.db.upsert_position(position)


def _shares_from_notional(notional_usd: float, price: float) -> float:
    if price <= 0:
        return 0.0
    return notional_usd / price
