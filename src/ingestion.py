from __future__ import annotations

import logging
import signal
import time
from datetime import timezone

from config import Settings
from crowding import CrowdingAnalyzer
from db import Database
from decision_engine import DecisionEngine
from execution import Executor
from models import RedemptionEvent, TradeEvent
from polymarket_data import PolymarketDataClient
from redemption import RedemptionExecutor
from resolution import ResolutionScanner

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
    ):
        self.settings = settings
        self.db = db
        self.data_client = data_client
        self.decision_engine = decision_engine
        self.executor = executor
        self.resolution_scanner = resolution_scanner
        self.redemption_executor = redemption_executor or RedemptionExecutor(resolution_scanner)
        self.crowding_analyzer = crowding_analyzer
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
        inserted = self.db.insert_trade(trade)
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
            decision = self.decision_engine.decide(trade, crowding_score)
            logger.info("copy decision=%s reason=%s", decision.should_copy, decision.reason)
            self.db.record_copy_decision(trade, decision.should_copy, decision.reason, decision.details)
            if decision.should_copy:
                self.executor.execute(trade, decision)
                self.db.record_copied_source_trade(trade)
            else:
                self.db.freeze_source_token_for_trade(trade, decision.reason)
        except Exception as exc:
            logger.exception("trade processing failed")
            self.db.freeze_source_token_for_trade(trade, str(exc))
            self.db.log_error("process_trade", exc, trade.raw_payload)
        return True

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
