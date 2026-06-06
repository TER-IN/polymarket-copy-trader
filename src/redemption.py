from __future__ import annotations

from models import RedemptionEvent
from resolution import ResolutionScanner


class RedemptionExecutor:
    def __init__(self, resolution_scanner: ResolutionScanner | None):
        self.resolution_scanner = resolution_scanner

    def process_source_redemption(self, redemption: RedemptionEvent) -> int:
        market_id = redemption.condition_id or redemption.market_id
        if not market_id or not self.resolution_scanner:
            return 0
        return self.resolution_scanner.scan_market(market_id)
