from __future__ import annotations

from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from models import (
    CopyMode,
    MarketTypeFilter,
    OutcomeSelectionMode,
    RiskMismatchPolicy,
    RiskMismatchScope,
    SellSizingMode,
    SourcePositionPolicy,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
        enable_decoding=False,
    )

    target_wallets: list[str] = Field(default_factory=list, alias="TARGET_WALLETS")
    copy_mode: CopyMode = Field(default=CopyMode.DRY_RUN, alias="COPY_MODE")
    poll_interval_seconds: int = Field(default=10, alias="POLL_INTERVAL_SECONDS")
    database_url: str = Field(default="sqlite:///polymarket_copy_trader.sqlite3", alias="DATABASE_URL")
    trading_day_timezone: str = Field(default="Europe/Prague", alias="TRADING_DAY_TIMEZONE")
    seed_existing_trades_on_startup: bool = Field(default=True, alias="SEED_EXISTING_TRADES_ON_STARTUP")
    source_position_policy: SourcePositionPolicy = Field(
        default=SourcePositionPolicy.SKIP_PREEXISTING,
        alias="SOURCE_POSITION_POLICY",
    )
    sell_sizing_mode: SellSizingMode = Field(
        default=SellSizingMode.SOURCE_POSITION_RATIO,
        alias="SELL_SIZING_MODE",
    )
    on_risk_mismatch: RiskMismatchPolicy = Field(
        default=RiskMismatchPolicy.FREEZE_TOKEN,
        alias="ON_RISK_MISMATCH",
    )
    risk_mismatch_scope: RiskMismatchScope = Field(
        default=RiskMismatchScope.TOKEN,
        alias="RISK_MISMATCH_SCOPE",
    )
    source_position_size_threshold: float = Field(default=0.01, alias="SOURCE_POSITION_SIZE_THRESHOLD")
    outcome_selection_mode: OutcomeSelectionMode = Field(
        default=OutcomeSelectionMode.SOURCE,
        alias="OUTCOME_SELECTION_MODE",
    )

    max_trade_usd: float | None = Field(default=None, alias="MAX_TRADE_USD")
    copy_ratio: float = Field(default=0.25, alias="COPY_RATIO")
    inverse_share_copy_ratio: float = Field(default=0.1, alias="INVERSE_SHARE_COPY_RATIO")
    inverse_down_max_source_price: float = Field(
        default=0.5,
        alias="INVERSE_DOWN_MAX_SOURCE_PRICE",
    )
    shadow_regime_window: int = Field(default=50, alias="SHADOW_REGIME_WINDOW")
    shadow_regime_confirmation_markets: int = Field(
        default=10,
        alias="SHADOW_REGIME_CONFIRMATION_MARKETS",
    )
    max_copied_buys_per_wallet_market: int | None = Field(
        default=None,
        alias="MAX_COPIED_BUYS_PER_WALLET_MARKET",
    )
    max_slippage_cents: float = Field(default=2.0, alias="MAX_SLIPPAGE_CENTS")
    max_buy_price: float | None = Field(default=None, alias="MAX_BUY_PRICE")
    max_seconds_until_market_end: int | None = Field(default=None, alias="MAX_SECONDS_UNTIL_MARKET_END")
    market_type_filter: MarketTypeFilter = Field(default=MarketTypeFilter.ALL, alias="MARKET_TYPE_FILTER")
    up_down_min_duration_seconds: int = Field(default=300, alias="UP_DOWN_MIN_DURATION_SECONDS")
    up_down_max_duration_seconds: int = Field(default=900, alias="UP_DOWN_MAX_DURATION_SECONDS")
    min_net_upside_usd: float | None = Field(default=None, alias="MIN_NET_UPSIDE_USD")
    min_net_upside_percent: float | None = Field(default=None, alias="MIN_NET_UPSIDE_PERCENT")
    net_upside_safety_margin_usd: float = Field(default=0.0, alias="NET_UPSIDE_SAFETY_MARGIN_USD")
    include_exit_fee_in_upside: bool = Field(default=False, alias="INCLUDE_EXIT_FEE_IN_UPSIDE")
    min_trade_usd: float = Field(default=1.0, alias="MIN_TRADE_USD")
    max_trade_age_seconds: int = Field(default=120, alias="MAX_TRADE_AGE_SECONDS")
    allow_market_categories: list[str] = Field(default_factory=list, alias="ALLOW_MARKET_CATEGORIES")
    allow_market_title_keywords: list[str] = Field(
        default_factory=list,
        alias="ALLOW_MARKET_TITLE_KEYWORDS",
    )
    block_market_keywords: list[str] = Field(default_factory=list, alias="BLOCK_MARKET_KEYWORDS")

    enable_crowding_check: bool = Field(default=True, alias="ENABLE_CROWDING_CHECK")
    crowding_lookback_seconds: int = Field(default=60, alias="CROWDING_LOOKBACK_SECONDS")
    crowding_max_followers: int = Field(default=5, alias="CROWDING_MAX_FOLLOWERS")

    enable_resolution_scanner: bool = Field(default=True, alias="ENABLE_RESOLUTION_SCANNER")
    resolution_scan_interval_seconds: int = Field(default=60, alias="RESOLUTION_SCAN_INTERVAL_SECONDS")

    daily_spend_cap_usd: float | None = Field(default=100.0, alias="DAILY_SPEND_CAP_USD")
    per_market_exposure_cap_usd: float = Field(default=50.0, alias="PER_MARKET_EXPOSURE_CAP_USD")
    condition_exposure_cap_usd: float | None = Field(
        default=None,
        alias="CONDITION_EXPOSURE_CAP_USD",
    )
    dry_run_starting_balance_usd: float | None = Field(default=None, alias="DRY_RUN_STARTING_BALANCE_USD")
    allow_copy_ratio_gt_one: bool = Field(default=False, alias="ALLOW_COPY_RATIO_GT_ONE")
    allow_short_sells: bool = Field(default=False, alias="ALLOW_SHORT_SELLS")
    stop_trading_file: Path = Field(default=Path("STOP_TRADING"), alias="STOP_TRADING_FILE")

    polymarket_private_key: str | None = Field(default=None, alias="POLYMARKET_PRIVATE_KEY")
    polymarket_funder: str | None = Field(default=None, alias="POLYMARKET_FUNDER")
    polymarket_signature_type: int = Field(default=2, alias="POLYMARKET_SIGNATURE_TYPE")
    chain_id: int = Field(default=137, alias="CHAIN_ID")

    data_api_base_url: str = "https://data-api.polymarket.com"
    gamma_api_base_url: str = "https://gamma-api.polymarket.com"
    user_pnl_api_base_url: str = Field(
        default="https://user-pnl-api.polymarket.com",
        alias="USER_PNL_API_BASE_URL",
    )
    clob_base_url: str = "https://clob.polymarket.com"

    @field_validator(
        "target_wallets",
        "allow_market_categories",
        "allow_market_title_keywords",
        "block_market_keywords",
        mode="before",
    )
    @classmethod
    def parse_csv_list(cls, value: Any) -> list[str]:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return list(value)

    @field_validator("trading_day_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator(
        "daily_spend_cap_usd",
        "max_buy_price",
        "max_copied_buys_per_wallet_market",
        "max_seconds_until_market_end",
        "max_trade_usd",
        "min_net_upside_usd",
        "min_net_upside_percent",
        "condition_exposure_cap_usd",
        mode="before",
    )
    @classmethod
    def parse_optional_number(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip().lower() in {"", "none", "null", "unlimited"}:
            return None
        return value

    @model_validator(mode="after")
    def validate_values(self) -> "Settings":
        if self.copy_ratio <= 0:
            raise ValueError("COPY_RATIO must be positive")
        if self.copy_ratio > 1 and not self.allow_copy_ratio_gt_one:
            raise ValueError("COPY_RATIO > 1 requires ALLOW_COPY_RATIO_GT_ONE=true")
        if self.inverse_share_copy_ratio <= 0:
            raise ValueError("INVERSE_SHARE_COPY_RATIO must be positive")
        if self.inverse_share_copy_ratio > 1 and not self.allow_copy_ratio_gt_one:
            raise ValueError(
                "INVERSE_SHARE_COPY_RATIO > 1 requires ALLOW_COPY_RATIO_GT_ONE=true"
            )
        if not 0 < self.inverse_down_max_source_price < 1:
            raise ValueError("INVERSE_DOWN_MAX_SOURCE_PRICE must be greater than 0 and less than 1")
        if self.shadow_regime_window <= 0:
            raise ValueError("SHADOW_REGIME_WINDOW must be positive")
        if self.shadow_regime_confirmation_markets <= 0:
            raise ValueError("SHADOW_REGIME_CONFIRMATION_MARKETS must be positive")
        if (
            self.max_copied_buys_per_wallet_market is not None
            and self.max_copied_buys_per_wallet_market <= 0
        ):
            raise ValueError("MAX_COPIED_BUYS_PER_WALLET_MARKET must be positive")
        if self.max_slippage_cents < 0:
            raise ValueError("MAX_SLIPPAGE_CENTS cannot be negative")
        if self.max_buy_price is not None and not 0 < self.max_buy_price <= 1:
            raise ValueError("MAX_BUY_PRICE must be greater than 0 and at most 1")
        if self.max_seconds_until_market_end is not None and self.max_seconds_until_market_end <= 0:
            raise ValueError("MAX_SECONDS_UNTIL_MARKET_END must be positive")
        if self.up_down_min_duration_seconds <= 0:
            raise ValueError("UP_DOWN_MIN_DURATION_SECONDS must be positive")
        if self.up_down_max_duration_seconds < self.up_down_min_duration_seconds:
            raise ValueError("UP_DOWN_MAX_DURATION_SECONDS must be at least UP_DOWN_MIN_DURATION_SECONDS")
        if self.min_net_upside_usd is not None and self.min_net_upside_usd < 0:
            raise ValueError("MIN_NET_UPSIDE_USD cannot be negative")
        if self.min_net_upside_percent is not None and self.min_net_upside_percent < 0:
            raise ValueError("MIN_NET_UPSIDE_PERCENT cannot be negative")
        if self.net_upside_safety_margin_usd < 0:
            raise ValueError("NET_UPSIDE_SAFETY_MARGIN_USD cannot be negative")
        if self.daily_spend_cap_usd is not None and self.daily_spend_cap_usd < 0:
            raise ValueError("DAILY_SPEND_CAP_USD cannot be negative")
        if self.condition_exposure_cap_usd is not None and self.condition_exposure_cap_usd <= 0:
            raise ValueError("CONDITION_EXPOSURE_CAP_USD must be positive")
        if self.dry_run_starting_balance_usd is not None and self.dry_run_starting_balance_usd < 0:
            raise ValueError("DRY_RUN_STARTING_BALANCE_USD cannot be negative")
        return self

    def sqlite_path(self) -> Path:
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            raise ValueError("Only sqlite:/// DATABASE_URL values are supported")
        return Path(self.database_url.removeprefix(prefix))

    def validate_live_ready(self, risk_flag: bool) -> None:
        if self.copy_mode != CopyMode.LIVE:
            return
        if (
            self.outcome_selection_mode
            == OutcomeSelectionMode.SHADOW_REGIME_DOWN_UNDERDOG
        ):
            raise ValueError(
                "shadow_regime_down_underdog is experimental and restricted to dry-run mode"
            )
        if not risk_flag:
            raise ValueError("live mode requires --i-understand-live-trading-risk")
        if not self.polymarket_private_key:
            raise ValueError("live mode requires POLYMARKET_PRIVATE_KEY")
        if self.max_trade_usd is None:
            raise ValueError("live mode requires MAX_TRADE_USD")
        if self.copy_ratio > 1 and not self.allow_copy_ratio_gt_one:
            raise ValueError("COPY_RATIO > 1 is refused in live mode")
        if self.inverse_share_copy_ratio > 1 and not self.allow_copy_ratio_gt_one:
            raise ValueError("INVERSE_SHARE_COPY_RATIO > 1 is refused in live mode")
