"""
Experimental placeholder for official free WebSocket sources.

Polling against https://data-api.polymarket.com/trades is the reliable baseline.
Add RTDS/activity trade wiring here only after verifying the official stream shape
for the account or activity feed you intend to use.
"""

from __future__ import annotations


class ExperimentalWebSocketIngestor:
    def __init__(self) -> None:
        raise NotImplementedError("WebSocket ingestion is intentionally experimental; use polling first.")
