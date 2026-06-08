from __future__ import annotations

from config import Settings
from db import Database
from models import CopyMode
from polymarket_gamma import GammaClient


class ResolutionScanner:
    def __init__(self, settings: Settings, db: Database, gamma_client: GammaClient):
        self.settings = settings
        self.db = db
        self.gamma_client = gamma_client

    def scan_once(self) -> int:
        settled = self._scan_positions(self.db.positions_for_resolution_scan())
        self._scan_decision_markets()
        return settled

    def scan_market(self, market_id: str) -> int:
        return self._scan_positions(self.db.positions_for_resolution_scan(market_id))

    def _scan_positions(self, positions) -> int:
        settled = 0
        resolutions = {}
        for position in positions:
            cache_key = position["market_id"]
            if cache_key not in resolutions:
                resolutions[cache_key] = self.gamma_client.get_resolution(
                    market_id=position["market_id"],
                    asset_id=position["asset_id"],
                    condition_id=position["market_id"],
                )
            resolution = resolutions[cache_key]
            if not resolution or not resolution.resolved:
                if resolution:
                    self._record_resolution(cache_key, resolution)
                continue
            self._record_resolution(cache_key, resolution)
            payout = resolution.payout_for_token(position["asset_id"])
            if payout is None:
                continue
            settlement_key = f"resolution:{position['market_id']}:{position['asset_id']}"
            if self.settings.copy_mode == CopyMode.DRY_RUN:
                self.db.settle_position_from_resolution(
                    position,
                    settlement_key,
                    payout,
                    "dry_run_resolution",
                    market_title=resolution.market_title,
                    raw_response={"source": "gamma_resolution_scanner", "market": resolution.raw_payload},
                )
                settled += 1
            elif payout > 0:
                self.db.settle_position_from_resolution(
                    position,
                    settlement_key,
                    payout,
                    "live_redeem_required",
                    market_title=resolution.market_title,
                    error_message="Winning live position resolved; redeem manually in your own Polymarket wallet.",
                    raw_response={"source": "gamma_resolution_scanner", "market": resolution.raw_payload},
                )
                settled += 1
            else:
                self.db.settle_position_from_resolution(
                    position,
                    settlement_key,
                    payout,
                    "live_resolved_loss",
                    market_title=resolution.market_title,
                    raw_response={"source": "gamma_resolution_scanner", "market": resolution.raw_payload},
                )
                settled += 1
        return settled

    def _scan_decision_markets(self) -> None:
        resolutions = {}
        for market in self.db.decision_markets_for_resolution_scan():
            market_id = market["market_id"]
            if market_id not in resolutions:
                resolutions[market_id] = self.gamma_client.get_resolution(
                    market_id=market_id,
                    asset_id=market["asset_id"],
                    condition_id=market["condition_id"] or market_id,
                )
            resolution = resolutions[market_id]
            if resolution:
                self._record_resolution(market_id, resolution)

    def _record_resolution(self, market_id: str, resolution) -> None:
        self.db.record_market_resolution_observation(
            market_id,
            resolution.resolved,
            resolution.payout_by_token_id,
            resolution.market_title,
            resolution.raw_payload,
        )
