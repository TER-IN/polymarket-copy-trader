from __future__ import annotations

from dataclasses import dataclass

from db import Database
from polymarket_gamma import GammaClient


@dataclass(frozen=True)
class SettlementCorrection:
    settlement_id: int
    market_id: str
    asset_id: str
    outcome: str
    market_title: str
    old_payout_usd: float
    corrected_payout_usd: float
    old_realized_pnl: float
    corrected_realized_pnl: float
    payout_per_share: float


class SettlementAuditor:
    def __init__(self, db: Database, gamma_client: GammaClient):
        self.db = db
        self.gamma_client = gamma_client

    def audit_legacy_source_settlements(self, apply: bool = False) -> tuple[list[SettlementCorrection], list[int]]:
        corrections: list[SettlementCorrection] = []
        unresolved_ids: list[int] = []
        resolutions = {}
        for row in self.db.legacy_source_settlement_rows():
            market_id = str(row["market_id"])
            if market_id not in resolutions:
                resolutions[market_id] = self.gamma_client.get_resolution(
                    market_id=market_id,
                    asset_id=str(row["asset_id"]),
                    condition_id=market_id,
                )
            resolution = resolutions[market_id]
            payout_per_share = resolution.payout_for_token(str(row["asset_id"])) if resolution else None
            if not resolution or not resolution.resolved or payout_per_share is None:
                unresolved_ids.append(int(row["id"]))
                continue

            old_payout = float(row["payout_usd"])
            old_realized = float(row["realized_pnl"])
            cost = old_payout - old_realized
            corrected_payout = float(row["shares"]) * payout_per_share
            corrected_realized = corrected_payout - cost
            correction = SettlementCorrection(
                settlement_id=int(row["id"]),
                market_id=market_id,
                asset_id=str(row["asset_id"]),
                outcome=str(row["outcome"]),
                market_title=str(row["market_title"] or market_id),
                old_payout_usd=old_payout,
                corrected_payout_usd=corrected_payout,
                old_realized_pnl=old_realized,
                corrected_realized_pnl=corrected_realized,
                payout_per_share=payout_per_share,
            )
            corrections.append(correction)
            if apply:
                self.db.reconcile_legacy_source_settlement(
                    correction.settlement_id,
                    payout_per_share,
                    {
                        "source": "authoritative_resolution_reconciliation",
                        "market": resolution.raw_payload,
                    },
                )
        return corrections, unresolved_ids
