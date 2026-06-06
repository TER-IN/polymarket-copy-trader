from __future__ import annotations

from collections import Counter
from datetime import timedelta
from statistics import median

from models import CrowdingScore, TradeEvent, TradeSide
from polymarket_data import PolymarketDataClient


def calculate_crowding_score(target: TradeEvent, nearby_trades: list[TradeEvent]) -> CrowdingScore:
    followers: list[TradeEvent] = []
    for trade in nearby_trades:
        if trade.source_wallet.lower() == target.source_wallet.lower():
            continue
        if (trade.market_id or trade.condition_id) != (target.market_id or target.condition_id):
            continue
        if trade.outcome != target.outcome or trade.side != target.side:
            continue
        if trade.timestamp < target.timestamp:
            continue
        if target.side == TradeSide.BUY and trade.price < target.price:
            continue
        if target.side == TradeSide.SELL and trade.price > target.price:
            continue
        followers.append(trade)

    delays = [(trade.timestamp - target.timestamp).total_seconds() for trade in followers]
    slippages = [trade.price - target.price for trade in followers]
    wallet_counts = Counter(trade.source_wallet.lower() for trade in followers)
    repeat_wallets = sorted(wallet for wallet, count in wallet_counts.items() if count > 1)

    return CrowdingScore(
        follower_count=len(wallet_counts),
        follower_notional=sum(trade.notional_usd for trade in followers),
        median_delay_seconds=median(delays) if delays else None,
        average_price_slippage_vs_target=(sum(slippages) / len(slippages)) if slippages else None,
        repeat_follower_wallets=repeat_wallets,
    )


class CrowdingAnalyzer:
    def __init__(self, data_client: PolymarketDataClient, lookback_seconds: int):
        self.data_client = data_client
        self.lookback_seconds = lookback_seconds

    def analyze(self, trade: TradeEvent) -> tuple[CrowdingScore, dict]:
        end = trade.timestamp + timedelta(seconds=self.lookback_seconds)
        nearby = self.data_client.market_trades_between(
            trade.market_id,
            trade.condition_id,
            trade.timestamp,
            end,
        )
        score = calculate_crowding_score(trade, nearby)
        return score, {"nearby_trade_count": len(nearby), "window_seconds": self.lookback_seconds}
