from __future__ import annotations

import logging
import signal
import time
from dataclasses import replace
from datetime import datetime
from datetime import timezone

from config import Settings
from crowding import CrowdingAnalyzer
from db import Database
from decision_engine import DecisionEngine
from execution import Executor
from models import (
    OutcomeSelectionMode,
    RedemptionEvent,
    ShadowRealTradePolicy,
    TradeEvent,
    TradeSide,
    utc_now,
)
from outcome_selection import OutcomeSelectionError, OutcomeSelectionSkip, OutcomeSelector
from polymarket_data import PolymarketDataClient
from redemption import RedemptionExecutor
from resolution import ResolutionScanner
from shadow_regime import (
    ShadowRegimeOverride,
    ShadowRegimePath,
    calculate_shadow_regime,
    effective_shadow_regime_path,
)

logger = logging.getLogger(__name__)


class PollingIngestor:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        data_client: PolymarketDataClient,
        decision_engine: DecisionEngine,
        executor: Executor,
        redemption_executor: RedemptionExecutor | None = None,
        resolution_scanner: ResolutionScanner | None = None,
        crowding_analyzer: CrowdingAnalyzer | None = None,
        outcome_selector: OutcomeSelector | None = None,
    ):
        self.settings = settings
        self.db = db
        self.data_client = data_client
        self.decision_engine = decision_engine
        self.executor = executor
        self.resolution_scanner = resolution_scanner
        self.redemption_executor = redemption_executor or RedemptionExecutor(resolution_scanner)
        self.crowding_analyzer = crowding_analyzer
        self.outcome_selector = outcome_selector
        self.running = True
        self.seeded_wallets: set[str] = set()
        self._last_resolution_scan = 0.0

    def stop(self, *_args: object) -> None:
        self.running = False

    def run_forever(self) -> None:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        logger.info("starting polling loop for %d wallets", len(self.settings.target_wallets))
        self.initialize_source_position_baselines()
        while self.running:
            started = time.monotonic()
            self.poll_once()
            self.scan_resolutions_if_due(started)
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, self.settings.poll_interval_seconds - elapsed))
        logger.info("polling loop stopped")

    def scan_resolutions_if_due(self, now_monotonic: float | None = None) -> None:
        if not self.settings.enable_resolution_scanner or not self.resolution_scanner:
            return
        now_monotonic = now_monotonic if now_monotonic is not None else time.monotonic()
        if now_monotonic - self._last_resolution_scan < self.settings.resolution_scan_interval_seconds:
            return
        self._last_resolution_scan = now_monotonic
        try:
            settled = self.resolution_scanner.scan_once()
            if settled:
                logger.info("resolution scanner settled_positions=%d", settled)
        except Exception as exc:
            logger.exception("resolution scanner failed")
            self.db.log_error("resolution_scanner", exc)

    def initialize_source_position_baselines(self) -> None:
        for wallet in self.settings.target_wallets:
            try:
                positions = self.data_client.current_wallet_positions(
                    wallet,
                    size_threshold=self.settings.source_position_size_threshold,
                )
                self.db.reset_source_baseline(wallet)
                for position in positions:
                    if position.market_id and position.asset_id and position.outcome and position.size > 0:
                        self.db.insert_preexisting_source_position(position)
                logger.info(
                    "baselined %d pre-existing source positions for wallet=%s",
                    len(positions),
                    wallet,
                )
            except Exception as exc:
                logger.exception("failed to baseline source positions for %s", wallet)
                self.db.log_error("source_position_baseline", exc, {"wallet": wallet})
                raise RuntimeError(f"failed to baseline source positions for {wallet}") from exc

    def poll_once(self) -> None:
        for wallet in self.settings.target_wallets:
            try:
                trades = self.data_client.recent_wallet_trades(wallet)
                redemptions = self.data_client.recent_wallet_redemptions(wallet)
            except Exception as exc:
                logger.exception("failed to fetch wallet activity for %s", wallet)
                self.db.log_error("recent_wallet_activity", exc, {"wallet": wallet})
                continue
            if self.settings.seed_existing_trades_on_startup and wallet.lower() not in self.seeded_wallets:
                inserted = sum(1 for trade in trades if self.db.insert_trade(trade))
                inserted_redemptions = sum(1 for redemption in redemptions if self.db.insert_redemption(redemption))
                self.seeded_wallets.add(wallet.lower())
                logger.info(
                    "seeded %d existing trades and %d redemptions for wallet=%s; "
                    "future polls will process only newly observed activity",
                    inserted,
                    inserted_redemptions,
                    wallet,
                )
                continue
            activity = [(item.timestamp, "trade", item) for item in trades]
            activity.extend((item.timestamp, "redemption", item) for item in redemptions)
            for _timestamp, activity_type, item in sorted(activity, key=lambda entry: entry[0]):
                if activity_type == "trade":
                    self.process_trade(item)
                else:
                    self.process_redemption(item)

    def process_trade(self, trade: TradeEvent) -> bool:
        observed_at = utc_now()
        processing_started = time.perf_counter()
        inserted = self.db.insert_trade(trade, observed_at=observed_at)
        if not inserted:
            return False
        logger.info(
            "detected trade wallet=%s side=%s outcome=%s price=%.4f size=%.4f notional=%.2f market=%s",
            trade.source_wallet,
            trade.side.value,
            trade.outcome,
            trade.price,
            trade.size,
            trade.notional_usd,
            trade.market_title,
        )
        crowding_score = None
        if self._should_analyze_crowding(trade) and self.settings.enable_crowding_check and self.crowding_analyzer:
            try:
                crowding_score, raw = self.crowding_analyzer.analyze(trade)
                self.db.record_crowding(trade, crowding_score, raw)
            except Exception as exc:
                logger.exception("crowding check failed")
                self.db.log_error("crowding", exc, trade.raw_payload)

        try:
            selected_trade = self._select_outcome(trade)
        except OutcomeSelectionSkip as exc:
            reason = str(exc)
            details = {
                "outcome_selection_mode": self.settings.outcome_selection_mode.value,
                "source_asset_id": trade.asset_id or trade.token_id,
                "source_outcome": trade.outcome,
                "source_price": trade.price,
                "inverse_down_max_source_price": self.settings.inverse_down_max_source_price,
            }
            _add_timing_details(details, trade, observed_at, processing_started)
            logger.info("copy decision=False reason=%s", reason)
            self.db.record_copy_decision(trade, False, reason, details)
            return True
        except OutcomeSelectionError as exc:
            reason = str(exc)
            details = {
                "outcome_selection_mode": self.settings.outcome_selection_mode.value,
                "source_asset_id": trade.asset_id or trade.token_id,
                "source_outcome": trade.outcome,
                "source_price": trade.price,
            }
            _add_timing_details(details, trade, observed_at, processing_started)
            logger.info("copy decision=False reason=%s", reason)
            self.db.record_copy_decision(trade, False, reason, details)
            self.db.freeze_source_token_for_trade(trade, reason)
            return True
        except Exception as exc:
            logger.exception("outcome selection failed")
            self.db.freeze_source_token_for_trade(trade, str(exc))
            self.db.log_error("outcome_selection", exc, trade.raw_payload)
            return True

        if (
            self.settings.outcome_selection_mode
            == OutcomeSelectionMode.SHADOW_REGIME_DOWN_UNDERDOG
        ):
            return self._process_shadow_regime_trade(
                trade,
                selected_trade,
                crowding_score,
                observed_at,
                processing_started,
            )

        try:
            decision = self.decision_engine.decide(
                selected_trade,
                crowding_score,
                source_trade=trade,
            )
            _add_timing_details(decision.details, trade, observed_at, processing_started)
            logger.info("copy decision=%s reason=%s", decision.should_copy, decision.reason)
            self.db.record_copy_decision(trade, decision.should_copy, decision.reason, decision.details)
            if decision.should_copy:
                result = self.executor.execute(selected_trade, decision, source_trade_key=trade.dedupe_key)
                if result is None or result.accepted:
                    self.db.record_copied_source_trade(trade)
            elif _should_freeze_rejection(decision.reason):
                self.db.freeze_source_token_for_trade(trade, decision.reason)
        except Exception as exc:
            logger.exception("trade processing failed")
            self.db.freeze_source_token_for_trade(trade, str(exc))
            self.db.log_error("process_trade", exc, trade.raw_payload)
        return True

    def _process_shadow_regime_trade(
        self,
        source_trade: TradeEvent,
        shadow_trade: TradeEvent,
        crowding_score,
        observed_at,
        processing_started: float,
    ) -> bool:
        if source_trade.side == TradeSide.SELL:
            return self._process_shadow_regime_sell(
                source_trade,
                shadow_trade,
                crowding_score,
                observed_at,
                processing_started,
            )

        market_id = shadow_trade.market_id or shadow_trade.condition_id or ""
        if self.db.shadow_order_exists(source_trade.source_wallet, market_id):
            details = {
                "outcome_selection_mode": self.settings.outcome_selection_mode.value,
                "shadow_market_id": market_id,
            }
            _add_timing_details(details, source_trade, observed_at, processing_started)
            reason = "shadow order already recorded for source wallet/market"
            self.db.record_copy_decision(source_trade, False, reason, details)
            logger.info("copy decision=False reason=%s", reason)
            return True

        try:
            shadow_decision = self.decision_engine.decide(
                shadow_trade,
                crowding_score,
                source_trade=source_trade,
            )
            if not shadow_decision.should_copy:
                _add_timing_details(
                    shadow_decision.details,
                    source_trade,
                    observed_at,
                    processing_started,
                )
                reason = f"shadow rejected: {shadow_decision.reason}"
                self.db.record_copy_decision(
                    source_trade,
                    False,
                    reason,
                    shadow_decision.details,
                )
                logger.info("copy decision=False reason=%s", reason)
                if _should_freeze_rejection(shadow_decision.reason):
                    self.db.freeze_source_token_for_trade(
                        source_trade,
                        shadow_decision.reason,
                    )
                return True

            selection = shadow_trade.raw_payload.get("_outcome_selection", {})
            opposite_asset_id = str(selection.get("source_asset_id") or "")
            if not opposite_asset_id:
                raise ValueError("shadow signal is missing the source/opposite asset id")
            inverted_trade = self._inverted_shadow_trade(source_trade, shadow_trade)
            opposite_decision = self.decision_engine.decide(
                inverted_trade,
                crowding_score,
                source_trade=source_trade,
            )
            self.db.record_shadow_order(
                source_trade,
                shadow_trade,
                opposite_asset_id,
                source_trade.outcome or "Down",
                shadow_decision,
                opposite_decision,
            )

            snapshot = calculate_shadow_regime(
                self.db.resolved_shadow_order_rows(source_trade.source_wallet),
                self.settings.shadow_regime_window,
                self.settings.shadow_regime_confirmation_markets,
            )
            override = ShadowRegimeOverride(
                self.db.shadow_regime_override(source_trade.source_wallet)
            )
            effective_path = effective_shadow_regime_path(
                snapshot,
                self.settings.shadow_regime_initial_path,
                override,
            )
            regime_details = snapshot.as_dict() | {
                "initial_path": self.settings.shadow_regime_initial_path.value,
                "override": override.value,
                "effective_path": effective_path.value if effective_path else None,
                "real_trade_policy": self.settings.shadow_real_trade_policy.value,
            }
            logger.info(
                "shadow market recorded market=%s resolved=%d/%d win_rate=%s "
                "calculated=%s effective=%s override=%s desired=%s pending=%s "
                "confirmation=%d/%d",
                market_id,
                snapshot.resolved_markets,
                snapshot.window_size,
                (
                    f"{snapshot.shadow_win_rate:.2%}"
                    if snapshot.shadow_win_rate is not None
                    else "n/a"
                ),
                snapshot.active_path.value if snapshot.active_path else "warmup",
                effective_path.value if effective_path else "warmup",
                override.value,
                snapshot.desired_path.value if snapshot.desired_path else "tie",
                snapshot.pending_path.value if snapshot.pending_path else "none",
                snapshot.confirmation_count,
                snapshot.confirmation_required,
            )

            policy_path, policy_reason = self._shadow_policy_path(
                effective_path,
                shadow_decision,
                opposite_decision,
            )
            regime_details["policy_path"] = policy_path.value if policy_path else None
            regime_details["policy_reason"] = policy_reason

            if policy_path is None:
                details = shadow_decision.details | {
                    "shadow_order_recorded": True,
                    "shadow_regime": regime_details,
                    "real_execution_path": None,
                    "real_trade_policy": self.settings.shadow_real_trade_policy.value,
                    "real_trade_policy_reason": policy_reason,
                    "shadow_trade": {
                        "asset_id": shadow_trade.asset_id,
                        "outcome": shadow_trade.outcome,
                        "copy_shares": shadow_decision.copy_shares,
                        "copy_notional_usd": shadow_decision.copy_notional_usd,
                        "executable_price": shadow_decision.current_price,
                        "estimated_fee_usd": shadow_decision.estimated_fee_usd,
                    },
                    "opposite_trade": {
                        "asset_id": inverted_trade.asset_id,
                        "outcome": inverted_trade.outcome,
                        "should_copy": opposite_decision.should_copy,
                        "reason": opposite_decision.reason,
                        "copy_shares": opposite_decision.copy_shares,
                        "copy_notional_usd": opposite_decision.copy_notional_usd,
                        "executable_price": opposite_decision.current_price,
                        "estimated_fee_usd": opposite_decision.estimated_fee_usd,
                    },
                }
                _add_timing_details(details, source_trade, observed_at, processing_started)
                reason = "shadow order recorded; " + (
                    policy_reason
                    if self.settings.shadow_real_trade_policy
                    == ShadowRealTradePolicy.PRICE_FILTER
                    else (
                        "regime warm-up "
                        f"{snapshot.resolved_markets}/{snapshot.window_size} resolved markets"
                    )
                )
                self.db.record_copy_decision(source_trade, False, reason, details)
                logger.info("copy decision=False reason=%s", reason)
                return True

            if policy_path == ShadowRegimePath.FOLLOW:
                real_trade = self._with_shadow_path(shadow_trade, "follow_shadow")
                real_decision = shadow_decision
            else:
                real_trade = inverted_trade
                real_decision = opposite_decision

            real_decision.details.update(
                {
                    "shadow_order_recorded": True,
                    "shadow_regime": regime_details,
                    "real_execution_path": policy_path.value,
                    "real_trade_policy": self.settings.shadow_real_trade_policy.value,
                    "real_trade_policy_reason": policy_reason,
                    "shadow_trade": {
                        "asset_id": shadow_trade.asset_id,
                        "outcome": shadow_trade.outcome,
                        "reference_price": shadow_trade.price,
                        "copy_shares": shadow_decision.copy_shares,
                        "copy_notional_usd": shadow_decision.copy_notional_usd,
                        "executable_price": shadow_decision.current_price,
                        "estimated_fee_usd": shadow_decision.estimated_fee_usd,
                    },
                    "opposite_trade": {
                        "asset_id": inverted_trade.asset_id,
                        "outcome": inverted_trade.outcome,
                        "should_copy": opposite_decision.should_copy,
                        "reason": opposite_decision.reason,
                        "copy_shares": opposite_decision.copy_shares,
                        "copy_notional_usd": opposite_decision.copy_notional_usd,
                        "executable_price": opposite_decision.current_price,
                        "estimated_fee_usd": opposite_decision.estimated_fee_usd,
                    },
                }
            )
            _add_timing_details(
                real_decision.details,
                source_trade,
                observed_at,
                processing_started,
            )
            reason = (
                f"{real_decision.reason}; real path={policy_path.value}; {policy_reason}"
                if real_decision.should_copy
                else f"real {policy_path.value} rejected: {real_decision.reason}; {policy_reason}"
            )
            self.db.record_copy_decision(
                source_trade,
                real_decision.should_copy,
                reason,
                real_decision.details,
            )
            logger.info(
                "copy decision=%s path=%s shadow_win_rate=%.2f%% reason=%s",
                real_decision.should_copy,
                policy_path.value,
                (snapshot.shadow_win_rate or 0.0) * 100,
                reason,
            )
            if real_decision.should_copy:
                result = self.executor.execute(
                    real_trade,
                    real_decision,
                    source_trade_key=source_trade.dedupe_key,
                )
                if result is None or result.accepted:
                    self.db.record_copied_source_trade(source_trade)
            elif _should_freeze_rejection(real_decision.reason):
                self.db.freeze_source_token_for_trade(
                    source_trade,
                    real_decision.reason,
                )
        except Exception as exc:
            logger.exception("shadow-regime processing failed")
            self.db.freeze_source_token_for_trade(source_trade, str(exc))
            self.db.log_error("shadow_regime", exc, source_trade.raw_payload)
        return True

    def _shadow_policy_path(
        self,
        regime_path: ShadowRegimePath | None,
        shadow_decision,
        opposite_decision,
    ) -> tuple[ShadowRegimePath | None, str]:
        if self.settings.shadow_real_trade_policy == ShadowRealTradePolicy.AUTO_REGIME:
            return regime_path, "auto regime policy"

        shadow_price = shadow_decision.current_price
        follow_max = self.settings.shadow_follow_max_price
        if (
            shadow_price is not None
            and shadow_price >= self.settings.shadow_follow_min_price
            and (follow_max is None or shadow_price < follow_max)
        ):
            follow_range = (
                f">= {self.settings.shadow_follow_min_price:.4f}"
                if follow_max is None
                else (
                    f"in [{self.settings.shadow_follow_min_price:.4f}, "
                    f"{follow_max:.4f})"
                )
            )
            return (
                ShadowRegimePath.FOLLOW,
                (
                    "price filter selected follow_shadow: "
                    f"shadow executable {shadow_price:.4f} {follow_range}"
                ),
            )

        opposite_price = opposite_decision.current_price
        if (
            self.settings.shadow_enable_invert_branch
            and
            opposite_decision.should_copy
            and opposite_price is not None
            and self.settings.shadow_invert_min_price
            <= opposite_price
            < self.settings.shadow_invert_max_price
        ):
            return (
                ShadowRegimePath.INVERT,
                (
                    "price filter selected invert_shadow: "
                    f"opposite executable {opposite_price:.4f} in "
                    f"[{self.settings.shadow_invert_min_price:.4f}, "
                    f"{self.settings.shadow_invert_max_price:.4f})"
                ),
            )

        reason = (
            "price filter skipped: "
            f"shadow executable={_fmt_price(shadow_price)} requires "
            f"{self._shadow_follow_price_requirement()}; "
        )
        if not opposite_decision.should_copy:
            reason += f"opposite rejected: {opposite_decision.reason}"
        elif not self.settings.shadow_enable_invert_branch:
            reason += "invert branch disabled"
        else:
            reason += (
                f"opposite executable={_fmt_price(opposite_price)} requires "
                f"[{self.settings.shadow_invert_min_price:.4f}, "
                f"{self.settings.shadow_invert_max_price:.4f})"
            )
        return None, reason

    def _shadow_follow_price_requirement(self) -> str:
        follow_max = self.settings.shadow_follow_max_price
        if follow_max is None:
            return f">= {self.settings.shadow_follow_min_price:.4f}"
        return (
            f"[{self.settings.shadow_follow_min_price:.4f}, "
            f"{follow_max:.4f})"
        )

    def _process_shadow_regime_sell(
        self,
        source_trade: TradeEvent,
        shadow_trade: TradeEvent,
        crowding_score,
        observed_at,
        processing_started: float,
    ) -> bool:
        copied = self.db.copied_target_for_wallet_market(source_trade)
        if copied is None:
            details = {
                "outcome_selection_mode": self.settings.outcome_selection_mode.value,
                "real_execution_path": None,
            }
            _add_timing_details(details, source_trade, observed_at, processing_started)
            reason = "shadow-regime sell skipped: no real copied position for market"
            self.db.record_copy_decision(source_trade, False, reason, details)
            return True

        target_is_shadow = str(copied["asset_id"]) == str(shadow_trade.asset_id)
        target_trade = (
            self._with_shadow_path(shadow_trade, "follow_shadow")
            if target_is_shadow
            else self._inverted_shadow_trade(source_trade, shadow_trade)
        )
        decision = self.decision_engine.decide(
            target_trade,
            crowding_score,
            source_trade=source_trade,
        )
        decision.details["real_execution_path"] = (
            "follow_shadow" if target_is_shadow else "invert_shadow"
        )
        _add_timing_details(decision.details, source_trade, observed_at, processing_started)
        self.db.record_copy_decision(
            source_trade,
            decision.should_copy,
            decision.reason,
            decision.details,
        )
        if decision.should_copy:
            self.executor.execute(
                target_trade,
                decision,
                source_trade_key=source_trade.dedupe_key,
            )
        return True

    @staticmethod
    def _with_shadow_path(trade: TradeEvent, path: str) -> TradeEvent:
        selection = trade.raw_payload.get("_outcome_selection", {}) | {
            "real_execution_path": path,
        }
        return replace(
            trade,
            raw_payload=trade.raw_payload | {"_outcome_selection": selection},
        )

    @staticmethod
    def _inverted_shadow_trade(
        source_trade: TradeEvent,
        shadow_trade: TradeEvent,
    ) -> TradeEvent:
        source_asset_id = source_trade.asset_id or source_trade.token_id
        selection = shadow_trade.raw_payload.get("_outcome_selection", {}) | {
            "copied_asset_id": source_asset_id,
            "copied_outcome": source_trade.outcome,
            "reference_price": source_trade.price,
            "real_execution_path": "invert_shadow",
        }
        return replace(
            source_trade,
            asset_id=source_asset_id,
            token_id=source_asset_id,
            raw_payload=source_trade.raw_payload | {"_outcome_selection": selection},
        )

    def _select_outcome(self, trade: TradeEvent) -> TradeEvent:
        if self.outcome_selector:
            return self.outcome_selector.select(trade)
        if self.settings.outcome_selection_mode != OutcomeSelectionMode.SOURCE:
            raise OutcomeSelectionError("inverse outcome selector is not configured")
        return trade

    def process_redemption(self, redemption: RedemptionEvent) -> bool:
        inserted = self.db.insert_redemption(redemption)
        if not inserted:
            return False
        logger.info(
            "detected redemption wallet=%s size=%.4f payout=%.2f market=%s",
            redemption.source_wallet,
            redemption.size,
            redemption.payout_usd,
            redemption.market_title,
        )
        try:
            settled = self.redemption_executor.process_source_redemption(redemption)
            logger.info("redemption settlement matched_positions=%d", settled)
        except Exception as exc:
            logger.exception("redemption processing failed")
            self.db.log_error("process_redemption", exc, redemption.raw_payload)
        return True

    def _should_analyze_crowding(self, trade: TradeEvent) -> bool:
        age = (time.time() - trade.timestamp.astimezone(timezone.utc).timestamp())
        if age > self.settings.max_trade_age_seconds:
            return False
        if trade.notional_usd < self.settings.min_trade_usd:
            return False
        return True


def _should_freeze_rejection(reason: str) -> bool:
    transient_prefixes = (
        "available balance exhausted",
        "copy size capped to zero",
        "daily spend cap exhausted",
        "maximum buy price exceeded",
        "maximum net upside",
        "market duration unavailable",
        "market ends too late",
        "market end time",
        "market is not an authoritative",
        "maximum copied buys per wallet/market reached",
        "per-market exposure cap exhausted",
        "condition exposure cap exhausted",
        "source wallet market frozen",
        "trade too old",
        "Up/Down market duration",
    )
    return not reason.startswith(transient_prefixes)


def _fmt_price(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def _add_timing_details(
    details: dict,
    trade: TradeEvent,
    observed_at: datetime,
    processing_started: float,
) -> None:
    completed_at = utc_now()
    details.update(
        {
            "source_trade_time": trade.timestamp.astimezone(timezone.utc).isoformat(),
            "observed_at": observed_at.isoformat(),
            "observation_delay_seconds": (
                observed_at - trade.timestamp.astimezone(timezone.utc)
            ).total_seconds(),
            "decision_completed_at": completed_at.isoformat(),
            "decision_processing_ms": (time.perf_counter() - processing_started) * 1000,
        }
    )
