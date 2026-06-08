from __future__ import annotations

from models import CopiedPosition, PositionStatus


def apply_buy(
    existing: CopiedPosition | None,
    market_id: str,
    asset_id: str,
    outcome: str,
    shares: float,
    price: float,
    source_wallet: str,
    fee_usd: float = 0.0,
) -> CopiedPosition:
    cost = shares * price + max(0.0, fee_usd)
    if existing is None:
        return CopiedPosition(
            market_id=market_id,
            asset_id=asset_id,
            outcome=outcome,
            total_shares=shares,
            avg_entry_price=cost / shares if shares else 0.0,
            total_cost=cost,
            source_wallets={source_wallet},
        )
    total_shares = existing.total_shares + shares
    total_cost = existing.total_cost + cost
    existing.total_shares = total_shares
    existing.total_cost = total_cost
    existing.avg_entry_price = total_cost / total_shares if total_shares else 0
    existing.source_wallets.add(source_wallet)
    existing.status = PositionStatus.OPEN
    return existing


def apply_sell(existing: CopiedPosition, shares: float, price: float, fee_usd: float = 0.0) -> CopiedPosition:
    sell_shares = min(shares, existing.total_shares)
    proceeds = sell_shares * price
    cost_basis = sell_shares * existing.avg_entry_price
    existing.realized_pnl += proceeds - cost_basis - max(0.0, fee_usd)
    existing.total_shares -= sell_shares
    existing.total_cost = max(0.0, existing.total_shares * existing.avg_entry_price)
    if existing.total_shares <= 1e-9:
        existing.total_shares = 0.0
        existing.total_cost = 0.0
        existing.status = PositionStatus.CLOSED
    return existing
