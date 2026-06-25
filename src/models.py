from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class CopyMode(StrEnum):
    DRY_RUN = "dry_run"
    LIVE = "live"


class TradeSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class PositionStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    RESOLVED = "resolved"
    REDEEM_REQUIRED = "redeem_required"


class SourceTokenStatus(StrEnum):
    CLEAN = "clean"
    PRE_EXISTING = "pre_existing"
    FROZEN = "frozen"


class SourcePositionPolicy(StrEnum):
    SKIP_PREEXISTING = "skip_preexisting"


class SellSizingMode(StrEnum):
    SOURCE_POSITION_RATIO = "source_position_ratio"


class RiskMismatchPolicy(StrEnum):
    FREEZE_TOKEN = "freeze_token"


class RiskMismatchScope(StrEnum):
    TOKEN = "token"
    WALLET_MARKET = "wallet_market"


class OutcomeSelectionMode(StrEnum):
    SOURCE = "source"
    INVERSE_UP_DOWN = "inverse_up_down"
    INVERSE_DOWN_UNDERDOG = "inverse_down_underdog"
    SHADOW_REGIME_DOWN_UNDERDOG = "shadow_regime_down_underdog"


class ShadowRealTradePolicy(StrEnum):
    AUTO_REGIME = "auto_regime"
    PRICE_FILTER = "price_filter"


class MarketTypeFilter(StrEnum):
    ALL = "all"
    SHORT_DURATION_UP_DOWN = "short_duration_up_down"


@dataclass(frozen=True)
class TradeEvent:
    source_wallet: str
    transaction_hash: str
    timestamp: datetime
    market_id: str | None
    condition_id: str | None
    asset_id: str | None
    token_id: str | None
    market_title: str | None
    outcome: str | None
    side: TradeSide
    price: float
    size: float
    notional_usd: float
    raw_payload: dict[str, Any] = field(default_factory=dict)

    @property
    def dedupe_key(self) -> str:
        parts = [
            self.transaction_hash.lower(),
            self.source_wallet.lower(),
            self.market_id or self.condition_id or "",
            self.outcome or "",
            self.side.value,
            f"{self.size:.8f}",
            f"{self.price:.8f}",
        ]
        return "|".join(parts)


@dataclass(frozen=True)
class RedemptionEvent:
    source_wallet: str
    transaction_hash: str
    timestamp: datetime
    market_id: str | None
    condition_id: str | None
    asset_id: str | None
    token_id: str | None
    market_title: str | None
    outcome: str | None
    size: float
    payout_usd: float
    raw_payload: dict[str, Any] = field(default_factory=dict)

    @property
    def dedupe_key(self) -> str:
        parts = [
            self.transaction_hash.lower(),
            self.source_wallet.lower(),
            self.market_id or self.condition_id or "",
            self.asset_id or self.token_id or "",
            self.outcome or "",
            f"{self.size:.8f}",
            f"{self.payout_usd:.8f}",
        ]
        return "|".join(parts)


@dataclass(frozen=True)
class CopyDecision:
    should_copy: bool
    reason: str
    copy_notional_usd: float = 0.0
    copy_shares: float | None = None
    allowed_price: float | None = None
    current_price: float | None = None
    reduce_only: bool = False
    estimated_fee_usd: float = 0.0
    fill_ratio: float = 1.0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionResult:
    accepted: bool
    status: str
    filled_shares: float = 0.0
    filled_notional_usd: float = 0.0
    fee_usd: float = 0.0


@dataclass(frozen=True)
class CrowdingScore:
    follower_count: int
    follower_notional: float
    median_delay_seconds: float | None
    average_price_slippage_vs_target: float | None
    repeat_follower_wallets: list[str]


@dataclass
class CopiedPosition:
    market_id: str
    asset_id: str
    outcome: str
    total_shares: float
    avg_entry_price: float
    total_cost: float
    source_wallets: set[str]
    status: PositionStatus = PositionStatus.OPEN
    realized_pnl: float = 0.0


@dataclass(frozen=True)
class SourcePositionSnapshot:
    source_wallet: str
    market_id: str
    asset_id: str
    outcome: str
    size: float
    avg_price: float | None
    raw_payload: dict[str, Any] = field(default_factory=dict)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        if value > 10_000_000_000:
            value = value / 1000
        dt = datetime.fromtimestamp(value, tz=timezone.utc)
    elif isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return parse_timestamp(int(text))
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    else:
        raise ValueError(f"unsupported timestamp value: {value!r}")
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
