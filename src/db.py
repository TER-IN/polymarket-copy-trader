from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

from models import CopiedPosition, CrowdingScore, PositionStatus, RedemptionEvent, SourcePositionSnapshot
from models import SourceTokenStatus, TradeEvent
from models import TradeSide, parse_timestamp


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS target_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dedupe_key TEXT NOT NULL UNIQUE,
  source_wallet TEXT NOT NULL,
  transaction_hash TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  market_id TEXT,
  condition_id TEXT,
  asset_id TEXT,
  token_id TEXT,
  market_title TEXT,
  outcome TEXT,
  side TEXT NOT NULL,
  price REAL NOT NULL,
  size REAL NOT NULL,
  notional_usd REAL NOT NULL,
  raw_payload TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS copied_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_trade_key TEXT NOT NULL,
  market_id TEXT,
  asset_id TEXT,
  outcome TEXT,
  side TEXT NOT NULL,
  requested_notional_usd REAL NOT NULL,
  requested_shares REAL,
  limit_price REAL,
  status TEXT NOT NULL,
  clob_order_id TEXT,
  filled_shares REAL DEFAULT 0,
  avg_fill_price REAL,
  error_message TEXT,
  raw_response TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS copied_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_id TEXT NOT NULL,
  asset_id TEXT NOT NULL,
  outcome TEXT NOT NULL,
  total_shares REAL NOT NULL,
  avg_entry_price REAL NOT NULL,
  total_cost REAL NOT NULL,
  source_wallets TEXT NOT NULL,
  status TEXT NOT NULL,
  realized_pnl REAL NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(market_id, asset_id, outcome)
);

CREATE TABLE IF NOT EXISTS crowding_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_trade_key TEXT NOT NULL,
  source_wallet TEXT NOT NULL,
  follower_count INTEGER NOT NULL,
  follower_notional REAL NOT NULL,
  median_delay_seconds REAL,
  average_price_slippage_vs_target REAL,
  repeat_follower_wallets TEXT NOT NULL,
  raw_payload TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settings_snapshot (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  settings_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS errors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  context TEXT NOT NULL,
  error_message TEXT NOT NULL,
  raw_payload TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS source_redemptions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dedupe_key TEXT NOT NULL UNIQUE,
  source_wallet TEXT NOT NULL,
  transaction_hash TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  market_id TEXT,
  condition_id TEXT,
  asset_id TEXT,
  token_id TEXT,
  market_title TEXT,
  outcome TEXT,
  size REAL NOT NULL,
  payout_usd REAL NOT NULL,
  raw_payload TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS copied_redemptions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_redemption_key TEXT NOT NULL,
  market_id TEXT NOT NULL,
  asset_id TEXT NOT NULL,
  outcome TEXT NOT NULL,
  shares REAL NOT NULL,
  payout_usd REAL NOT NULL,
  realized_pnl REAL NOT NULL,
  status TEXT NOT NULL,
  tx_hash TEXT,
  error_message TEXT,
  raw_response TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS wallet_pnl_points (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  wallet TEXT NOT NULL,
  interval TEXT NOT NULL,
  fidelity TEXT NOT NULL,
  timestamp INTEGER NOT NULL,
  pnl REAL NOT NULL,
  fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(wallet, interval, fidelity, timestamp)
);

CREATE TABLE IF NOT EXISTS source_token_states (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_wallet TEXT NOT NULL,
  market_id TEXT NOT NULL,
  asset_id TEXT NOT NULL,
  outcome TEXT NOT NULL,
  status TEXT NOT NULL,
  baseline_source_shares REAL NOT NULL DEFAULT 0,
  observed_source_shares REAL NOT NULL DEFAULT 0,
  freeze_reason TEXT,
  last_source_trade_key TEXT,
  raw_payload TEXT NOT NULL DEFAULT '{}',
  initialized_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(source_wallet, market_id, asset_id, outcome)
);

CREATE TABLE IF NOT EXISTS copy_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_trade_key TEXT NOT NULL UNIQUE,
  should_copy INTEGER NOT NULL,
  reason TEXT NOT NULL,
  details TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        if self.path.parent != Path("."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def reset_source_baseline(self, wallet: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM source_token_states WHERE lower(source_wallet) = lower(?)", (wallet,))

    def insert_preexisting_source_position(self, position: SourcePositionSnapshot) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO source_token_states (
                  source_wallet, market_id, asset_id, outcome, status, baseline_source_shares,
                  observed_source_shares, freeze_reason, raw_payload, initialized_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                ON CONFLICT(source_wallet, market_id, asset_id, outcome) DO UPDATE SET
                  status = excluded.status,
                  baseline_source_shares = excluded.baseline_source_shares,
                  observed_source_shares = excluded.observed_source_shares,
                  freeze_reason = excluded.freeze_reason,
                  raw_payload = excluded.raw_payload,
                  initialized_at = excluded.initialized_at,
                  updated_at = excluded.updated_at
                """,
                (
                    position.source_wallet.lower(),
                    position.market_id,
                    position.asset_id,
                    position.outcome,
                    SourceTokenStatus.PRE_EXISTING.value,
                    position.size,
                    "source held position before bot startup",
                    json.dumps(position.raw_payload, sort_keys=True),
                    now,
                    now,
                ),
            )

    def get_source_token_state(
        self,
        source_wallet: str,
        market_id: str,
        asset_id: str,
        outcome: str,
    ) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM source_token_states
                WHERE lower(source_wallet) = lower(?)
                  AND market_id = ?
                  AND asset_id = ?
                  AND outcome = ?
                """,
                (source_wallet, market_id, asset_id, outcome),
            ).fetchone()

    def get_source_token_state_for_trade(self, trade: TradeEvent) -> sqlite3.Row | None:
        market_id = trade.market_id or trade.condition_id or ""
        asset_id = trade.asset_id or trade.token_id or ""
        outcome = trade.outcome or ""
        exact = self.get_source_token_state(trade.source_wallet, market_id, asset_id, outcome)
        if exact:
            return exact
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM source_token_states
                WHERE lower(source_wallet) = lower(?)
                  AND asset_id = ?
                  AND outcome = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (trade.source_wallet, asset_id, outcome),
            ).fetchone()

    def ensure_clean_source_token_state(self, trade: TradeEvent) -> sqlite3.Row | None:
        market_id = trade.market_id or trade.condition_id
        asset_id = trade.asset_id or trade.token_id
        outcome = trade.outcome or ""
        if not market_id or not asset_id:
            return None
        existing = self.get_source_token_state(trade.source_wallet, market_id, asset_id, outcome)
        if existing:
            return existing
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO source_token_states (
                  source_wallet, market_id, asset_id, outcome, status, baseline_source_shares,
                  observed_source_shares, raw_payload, initialized_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?, ?)
                """,
                (
                    trade.source_wallet.lower(),
                    market_id,
                    asset_id,
                    outcome,
                    SourceTokenStatus.CLEAN.value,
                    json.dumps({"created_from_trade": trade.dedupe_key}, sort_keys=True),
                    now,
                    now,
                ),
            )
        return self.get_source_token_state(trade.source_wallet, market_id, asset_id, outcome)

    def record_copied_source_trade(self, trade: TradeEvent) -> None:
        state = self.ensure_clean_source_token_state(trade)
        if not state or state["status"] != SourceTokenStatus.CLEAN.value:
            return
        delta = trade.size if trade.side == TradeSide.BUY else -trade.size
        observed = max(0.0, state["observed_source_shares"] + delta)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE source_token_states
                SET observed_source_shares = ?, last_source_trade_key = ?, updated_at = ?
                WHERE id = ?
                """,
                (observed, trade.dedupe_key, datetime.now(timezone.utc).isoformat(), state["id"]),
            )

    def freeze_source_token_for_trade(self, trade: TradeEvent, reason: str) -> None:
        state = self.ensure_clean_source_token_state(trade)
        if not state or state["status"] == SourceTokenStatus.PRE_EXISTING.value:
            return
        reason = _normalize_freeze_reason(reason)
        if state["status"] == SourceTokenStatus.FROZEN.value and state["freeze_reason"]:
            return
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE source_token_states
                SET status = ?, freeze_reason = ?, last_source_trade_key = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    SourceTokenStatus.FROZEN.value,
                    reason,
                    trade.dedupe_key,
                    datetime.now(timezone.utc).isoformat(),
                    state["id"],
                ),
            )

    def record_copy_decision(self, trade: TradeEvent, should_copy: bool, reason: str, details: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO copy_decisions (source_trade_key, should_copy, reason, details)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source_trade_key) DO UPDATE SET
                  should_copy = excluded.should_copy,
                  reason = excluded.reason,
                  details = excluded.details,
                  created_at = CURRENT_TIMESTAMP
                """,
                (trade.dedupe_key, int(should_copy), reason, json.dumps(details, sort_keys=True)),
            )

    def source_token_states(self, wallet: str | None = None, limit: int = 100) -> list[sqlite3.Row]:
        where = "" if wallet is None else "WHERE lower(st.source_wallet) = lower(?)"
        params: tuple = (limit,) if wallet is None else (wallet, limit)
        with self.connect() as conn:
            return list(
                conn.execute(
                    f"""
                    SELECT st.*, cd.details AS decision_details, cd.created_at AS decision_created_at
                    FROM source_token_states st
                    LEFT JOIN copy_decisions cd ON cd.source_trade_key = st.last_source_trade_key
                    {where}
                    ORDER BY st.updated_at DESC
                    LIMIT ?
                    """,
                    params,
                )
            )

    def insert_trade(self, trade: TradeEvent) -> bool:
        with self.connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO target_trades (
                      dedupe_key, source_wallet, transaction_hash, timestamp, market_id,
                      condition_id, asset_id, token_id, market_title, outcome, side,
                      price, size, notional_usd, raw_payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trade.dedupe_key,
                        trade.source_wallet,
                        trade.transaction_hash,
                        trade.timestamp.isoformat(),
                        trade.market_id,
                        trade.condition_id,
                        trade.asset_id,
                        trade.token_id,
                        trade.market_title,
                        trade.outcome,
                        trade.side.value,
                        trade.price,
                        trade.size,
                        trade.notional_usd,
                        json.dumps(trade.raw_payload, sort_keys=True),
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def has_trade(self, trade: TradeEvent) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM target_trades WHERE dedupe_key = ?",
                (trade.dedupe_key,),
            ).fetchone()
        return row is not None

    def insert_redemption(self, redemption: RedemptionEvent) -> bool:
        with self.connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO source_redemptions (
                      dedupe_key, source_wallet, transaction_hash, timestamp, market_id,
                      condition_id, asset_id, token_id, market_title, outcome, size,
                      payout_usd, raw_payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        redemption.dedupe_key,
                        redemption.source_wallet,
                        redemption.transaction_hash,
                        redemption.timestamp.isoformat(),
                        redemption.market_id or redemption.condition_id,
                        redemption.condition_id,
                        redemption.asset_id,
                        redemption.token_id,
                        redemption.market_title,
                        redemption.outcome,
                        redemption.size,
                        redemption.payout_usd,
                        json.dumps(redemption.raw_payload, sort_keys=True),
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def record_order(
        self,
        source_trade_key: str,
        trade: TradeEvent,
        requested_notional_usd: float,
        requested_shares: float | None,
        limit_price: float | None,
        status: str,
        clob_order_id: str | None = None,
        filled_shares: float = 0,
        avg_fill_price: float | None = None,
        error_message: str | None = None,
        raw_response: dict | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO copied_orders (
                  source_trade_key, market_id, asset_id, outcome, side, requested_notional_usd,
                  requested_shares, limit_price, status, clob_order_id, filled_shares,
                  avg_fill_price, error_message, raw_response
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_trade_key,
                    trade.market_id or trade.condition_id,
                    trade.asset_id or trade.token_id,
                    trade.outcome,
                    trade.side.value,
                    requested_notional_usd,
                    requested_shares,
                    limit_price,
                    status,
                    clob_order_id,
                    filled_shares,
                    avg_fill_price,
                    error_message,
                    json.dumps(raw_response or {}, sort_keys=True),
                ),
            )

    def get_position(self, market_id: str, asset_id: str, outcome: str) -> CopiedPosition | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM copied_positions
                WHERE market_id = ? AND asset_id = ? AND outcome = ?
                """,
                (market_id, asset_id, outcome),
            ).fetchone()
        if not row:
            return None
        return CopiedPosition(
            market_id=row["market_id"],
            asset_id=row["asset_id"],
            outcome=row["outcome"],
            total_shares=row["total_shares"],
            avg_entry_price=row["avg_entry_price"],
            total_cost=row["total_cost"],
            source_wallets=set(json.loads(row["source_wallets"])),
            status=PositionStatus(row["status"]),
            realized_pnl=row["realized_pnl"],
        )

    def upsert_position(self, position: CopiedPosition) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO copied_positions (
                  market_id, asset_id, outcome, total_shares, avg_entry_price, total_cost,
                  source_wallets, status, realized_pnl, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market_id, asset_id, outcome) DO UPDATE SET
                  total_shares = excluded.total_shares,
                  avg_entry_price = excluded.avg_entry_price,
                  total_cost = excluded.total_cost,
                  source_wallets = excluded.source_wallets,
                  status = excluded.status,
                  realized_pnl = excluded.realized_pnl,
                  updated_at = excluded.updated_at
                """,
                (
                    position.market_id,
                    position.asset_id,
                    position.outcome,
                    position.total_shares,
                    position.avg_entry_price,
                    position.total_cost,
                    json.dumps(sorted(position.source_wallets)),
                    position.status.value,
                    position.realized_pnl,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def copied_positions_for_redemption(self, redemption: RedemptionEvent) -> list[sqlite3.Row]:
        market_id = redemption.market_id or redemption.condition_id
        asset_id = redemption.asset_id or redemption.token_id
        outcome = redemption.outcome or ""
        asset_filter = "" if not asset_id else "AND asset_id = ?"
        outcome_filter = "" if not outcome else "AND outcome = ?"
        params: list[object] = [market_id, market_id]
        if asset_id:
            params.append(asset_id)
        if outcome:
            params.append(outcome)
        with self.connect() as conn:
            return list(
                conn.execute(
                    f"""
                    SELECT *
                    FROM copied_positions
                    WHERE status = 'open'
                      AND total_shares > 0
                      AND (market_id = ? OR market_id = ?)
                      {asset_filter}
                      {outcome_filter}
                    ORDER BY updated_at DESC
                    """,
                    tuple(params),
                )
            )

    def settle_position_from_redemption(
        self,
        redemption: RedemptionEvent,
        position_row: sqlite3.Row,
        status: str,
        tx_hash: str | None = None,
        error_message: str | None = None,
        raw_response: dict | None = None,
    ) -> None:
        shares = float(position_row["total_shares"])
        cost = float(position_row["total_cost"])
        payout_per_share = redemption.payout_usd / redemption.size if redemption.size > 0 else 1.0
        payout_per_share = max(0.0, min(1.0, payout_per_share))
        payout = shares * payout_per_share
        realized_pnl = payout - cost
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO copied_redemptions (
                  source_redemption_key, market_id, asset_id, outcome, shares, payout_usd,
                  realized_pnl, status, tx_hash, error_message, raw_response
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    redemption.dedupe_key,
                    position_row["market_id"],
                    position_row["asset_id"],
                    position_row["outcome"],
                    shares,
                    payout,
                    realized_pnl,
                    status,
                    tx_hash,
                    error_message,
                    json.dumps(raw_response or {}, sort_keys=True),
                ),
            )
            if status in ("dry_run", "redeemed", "live_accounted"):
                conn.execute(
                    """
                    UPDATE copied_positions
                    SET total_shares = 0,
                        total_cost = 0,
                        status = ?,
                        realized_pnl = realized_pnl + ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        PositionStatus.RESOLVED.value,
                        realized_pnl,
                        datetime.now(timezone.utc).isoformat(),
                        position_row["id"],
                    ),
                )

    def settle_position_from_resolution(
        self,
        position_row: sqlite3.Row,
        settlement_key: str,
        payout_per_share: float,
        status: str,
        market_title: str | None = None,
        error_message: str | None = None,
        raw_response: dict | None = None,
    ) -> None:
        shares = float(position_row["total_shares"])
        cost = float(position_row["total_cost"])
        payout_per_share = max(0.0, min(1.0, payout_per_share))
        payout = shares * payout_per_share
        realized_pnl = payout - cost
        raw = raw_response or {}
        if market_title:
            raw = raw | {"market_title": market_title}
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT 1
                FROM copied_redemptions
                WHERE source_redemption_key = ?
                  AND market_id = ?
                  AND asset_id = ?
                  AND outcome = ?
                """,
                (settlement_key, position_row["market_id"], position_row["asset_id"], position_row["outcome"]),
            ).fetchone()
            if existing:
                return
            conn.execute(
                """
                INSERT INTO copied_redemptions (
                  source_redemption_key, market_id, asset_id, outcome, shares, payout_usd,
                  realized_pnl, status, error_message, raw_response
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    settlement_key,
                    position_row["market_id"],
                    position_row["asset_id"],
                    position_row["outcome"],
                    shares,
                    payout,
                    realized_pnl,
                    status,
                    error_message,
                    json.dumps(raw, sort_keys=True),
                ),
            )
            if status in ("dry_run_resolution", "live_resolved_loss"):
                conn.execute(
                    """
                    UPDATE copied_positions
                    SET total_shares = 0,
                        total_cost = 0,
                        status = ?,
                        realized_pnl = realized_pnl + ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        PositionStatus.RESOLVED.value,
                        realized_pnl,
                        datetime.now(timezone.utc).isoformat(),
                        position_row["id"],
                    ),
                )
            elif status == "live_redeem_required":
                conn.execute(
                    """
                    UPDATE copied_positions
                    SET status = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        PositionStatus.REDEEM_REQUIRED.value,
                        datetime.now(timezone.utc).isoformat(),
                        position_row["id"],
                    ),
                )

    def record_crowding(self, trade: TradeEvent, score: CrowdingScore, raw_payload: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO crowding_observations (
                  source_trade_key, source_wallet, follower_count, follower_notional,
                  median_delay_seconds, average_price_slippage_vs_target,
                  repeat_follower_wallets, raw_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade.dedupe_key,
                    trade.source_wallet,
                    score.follower_count,
                    score.follower_notional,
                    score.median_delay_seconds,
                    score.average_price_slippage_vs_target,
                    json.dumps(score.repeat_follower_wallets),
                    json.dumps(raw_payload, sort_keys=True),
                ),
            )

    def log_error(self, context: str, error: Exception | str, raw_payload: dict | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO errors (context, error_message, raw_payload) VALUES (?, ?, ?)",
                (context, str(error), json.dumps(raw_payload or {}, sort_keys=True)),
            )

    def snapshot_settings(self, settings_json: str) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO settings_snapshot (settings_json) VALUES (?)", (settings_json,))

    def recent_trades(self, limit: int = 20) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM target_trades ORDER BY timestamp DESC, id DESC LIMIT ?",
                    (limit,),
                )
            )

    def positions(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute("SELECT * FROM copied_positions ORDER BY updated_at DESC"))

    def positions_for_resolution_scan(self, market_id: str | None = None) -> list[sqlite3.Row]:
        market_filter = "" if market_id is None else "AND market_id = ?"
        params: tuple[object, ...] = () if market_id is None else (market_id,)
        with self.connect() as conn:
            return list(
                conn.execute(
                    f"""
                    SELECT *
                    FROM copied_positions
                    WHERE status IN ('open', 'redeem_required')
                      AND total_shares > 0
                      {market_filter}
                    ORDER BY updated_at ASC
                    """,
                    params,
                )
            )

    def legacy_source_settlement_rows(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT
                      cr.*,
                      sr.market_title,
                      sr.asset_id AS source_asset_id,
                      sr.token_id AS source_token_id,
                      sr.outcome AS source_outcome
                    FROM copied_redemptions cr
                    JOIN source_redemptions sr ON sr.dedupe_key = cr.source_redemption_key
                    WHERE cr.status = 'dry_run'
                    ORDER BY cr.id
                    """
                )
            )

    def reconcile_legacy_source_settlement(
        self,
        settlement_id: int,
        payout_per_share: float,
        raw_response: dict,
    ) -> None:
        payout_per_share = max(0.0, min(1.0, payout_per_share))
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM copied_redemptions WHERE id = ? AND status = 'dry_run'",
                (settlement_id,),
            ).fetchone()
            if not row:
                return
            old_payout = float(row["payout_usd"])
            old_realized = float(row["realized_pnl"])
            cost = old_payout - old_realized
            corrected_payout = float(row["shares"]) * payout_per_share
            corrected_realized = corrected_payout - cost
            realized_delta = corrected_realized - old_realized
            conn.execute(
                """
                UPDATE copied_redemptions
                SET payout_usd = ?,
                    realized_pnl = ?,
                    status = 'dry_run_resolution',
                    raw_response = ?
                WHERE id = ?
                """,
                (
                    corrected_payout,
                    corrected_realized,
                    json.dumps(raw_response, sort_keys=True),
                    settlement_id,
                ),
            )
            conn.execute(
                """
                UPDATE copied_positions
                SET realized_pnl = realized_pnl + ?,
                    updated_at = ?
                WHERE market_id = ? AND asset_id = ? AND outcome = ?
                """,
                (
                    realized_delta,
                    datetime.now(timezone.utc).isoformat(),
                    row["market_id"],
                    row["asset_id"],
                    row["outcome"],
                ),
            )

    def position_dashboard_rows(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT
                      cp.*,
                      (
                        SELECT tt.market_title
                        FROM target_trades tt
                        WHERE (tt.market_id = cp.market_id OR tt.condition_id = cp.market_id)
                          AND (tt.asset_id = cp.asset_id OR tt.token_id = cp.asset_id)
                          AND COALESCE(tt.outcome, '') = COALESCE(cp.outcome, '')
                        ORDER BY tt.timestamp DESC
                        LIMIT 1
                      ) AS market_title,
                      (
                        SELECT COALESCE(
                          json_extract(tt.raw_payload, '$.eventSlug'),
                          json_extract(tt.raw_payload, '$.event_slug'),
                          json_extract(tt.raw_payload, '$.slug')
                        )
                        FROM target_trades tt
                        WHERE (tt.market_id = cp.market_id OR tt.condition_id = cp.market_id)
                          AND (tt.asset_id = cp.asset_id OR tt.token_id = cp.asset_id)
                          AND COALESCE(tt.outcome, '') = COALESCE(cp.outcome, '')
                        ORDER BY tt.timestamp DESC
                        LIMIT 1
                      ) AS event_slug
                    FROM copied_positions cp
                    ORDER BY cp.updated_at DESC
                    """
                )
            )

    def copied_source_trade_rows(self, limit: int = 50) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT
                      co.id AS copied_order_id,
                      co.status AS copied_order_status,
                      co.requested_notional_usd,
                      co.requested_shares,
                      co.limit_price,
                      co.avg_fill_price,
                      co.created_at AS copied_at,
                      tt.*
                    FROM copied_orders co
                    JOIN target_trades tt ON tt.dedupe_key = co.source_trade_key
                    WHERE co.status IN ('dry_run', 'submitted', 'filled', 'partial')
                    ORDER BY co.created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            )

    def copied_order_rows(self, limit: int = 50) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT
                      co.id AS copied_order_id,
                      co.status AS copied_order_status,
                      co.requested_notional_usd,
                      co.requested_shares,
                      co.limit_price,
                      co.avg_fill_price,
                      co.filled_shares,
                      co.error_message,
                      co.created_at AS copied_at,
                      tt.timestamp AS source_time,
                      tt.source_wallet,
                      tt.side AS source_side,
                      tt.outcome AS source_outcome,
                      tt.price AS source_price,
                      tt.size AS source_size,
                      tt.notional_usd AS source_notional_usd,
                      tt.market_title,
                      COALESCE(tt.asset_id, tt.token_id, co.asset_id) AS token_id
                    FROM copied_orders co
                    LEFT JOIN target_trades tt ON tt.dedupe_key = co.source_trade_key
                    ORDER BY co.created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            )

    def copied_redemption_rows(self, limit: int = 50) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT
                      cr.*,
                      sr.timestamp AS source_time,
                      sr.source_wallet,
                      sr.size AS source_size,
                      sr.payout_usd AS source_payout_usd,
                      sr.market_title
                    FROM copied_redemptions cr
                    LEFT JOIN source_redemptions sr ON sr.dedupe_key = cr.source_redemption_key
                    ORDER BY cr.created_at DESC, cr.id DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            )

    def recent_errors(self, limit: int = 20) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT *
                    FROM errors
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            )

    def reset_simulation(self, include_seen_trades: bool = False) -> dict[str, int]:
        tables = [
            "copied_orders",
            "copied_positions",
            "copied_redemptions",
            "crowding_observations",
            "source_token_states",
            "copy_decisions",
        ]
        if include_seen_trades:
            tables.extend(["target_trades", "source_redemptions"])
        deleted: dict[str, int] = {}
        with self.connect() as conn:
            for table in tables:
                cursor = conn.execute(f"DELETE FROM {table}")
                deleted[table] = cursor.rowcount
        return deleted

    def crowding_for_wallet(self, wallet: str, limit: int = 20, include_zero: bool = False) -> list[sqlite3.Row]:
        zero_filter = "" if include_zero else "AND follower_count > 0"
        with self.connect() as conn:
            return list(
                conn.execute(
                    f"""
                    SELECT
                      co.*,
                      tt.timestamp AS target_timestamp,
                      tt.side AS target_side,
                      tt.outcome AS target_outcome,
                      tt.price AS target_price,
                      tt.size AS target_size,
                      tt.notional_usd AS target_notional_usd,
                      tt.market_title AS target_market_title
                    FROM crowding_observations co
                    JOIN (
                      SELECT source_trade_key, MAX(id) AS latest_id
                      FROM crowding_observations
                      WHERE lower(source_wallet) = lower(?)
                      GROUP BY source_trade_key
                    ) latest ON latest.latest_id = co.id
                    LEFT JOIN target_trades tt ON tt.dedupe_key = co.source_trade_key
                    WHERE lower(co.source_wallet) = lower(?)
                    {zero_filter}
                    ORDER BY co.created_at DESC LIMIT ?
                    """,
                    (wallet, wallet, limit),
                )
            )

    def target_trades_for_wallet(self, wallet: str, since_iso: str, limit: int = 500) -> list[TradeEvent]:
        with self.connect() as conn:
            rows = list(
                conn.execute(
                    """
                    SELECT * FROM target_trades
                    WHERE lower(source_wallet) = lower(?) AND timestamp >= ?
                    ORDER BY timestamp DESC LIMIT ?
                    """,
                    (wallet, since_iso, limit),
                )
            )
        return [_trade_from_row(row) for row in rows]

    def spend_today(self, timezone_name: str = "UTC") -> float:
        start_utc, end_utc = _local_day_bounds_utc(timezone_name)
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(requested_notional_usd), 0) AS spend
                FROM copied_orders
                WHERE side = 'BUY' AND status IN ('dry_run', 'submitted', 'filled', 'partial')
                  AND created_at >= ?
                  AND created_at < ?
                """,
                (_sqlite_utc(start_utc), _sqlite_utc(end_utc)),
            ).fetchone()
        return float(row["spend"])

    def simulated_cash_balance(self, starting_balance_usd: float) -> float:
        with self.connect() as conn:
            order_rows = conn.execute(
                """
                SELECT side, requested_notional_usd
                FROM copied_orders
                WHERE status = 'dry_run'
                """
            ).fetchall()
            payout_row = conn.execute(
                """
                SELECT COALESCE(SUM(payout_usd), 0) AS payout
                FROM copied_redemptions
                WHERE status IN ('dry_run', 'dry_run_resolution')
                """
            ).fetchone()
        cash = starting_balance_usd
        for row in order_rows:
            notional = float(row["requested_notional_usd"] or 0)
            cash += notional if row["side"] == TradeSide.SELL.value else -notional
        cash += float(payout_row["payout"] or 0)
        return max(0.0, cash)

    def daily_performance_rows(self, timezone_name: str = "UTC", days: int = 30) -> list[dict[str, float | int | str]]:
        tz = ZoneInfo(timezone_name)
        today = datetime.now(tz).date()
        first_day = today - timedelta(days=max(1, days) - 1)
        start_utc = datetime.combine(first_day, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
        rows_by_day: dict[str, dict[str, float | int | str]] = {}
        for offset in range(max(1, days)):
            date_text = (first_day + timedelta(days=offset)).isoformat()
            rows_by_day[date_text] = {
                "date": date_text,
                "buy_count": 0,
                "buy_spend": 0.0,
                "sell_count": 0,
                "sell_notional": 0.0,
                "settlement_count": 0,
                "settlement_payout": 0.0,
                "settlement_pnl": 0.0,
                "net_cashflow": 0.0,
            }

        with self.connect() as conn:
            order_rows = conn.execute(
                """
                SELECT side, requested_notional_usd, created_at
                FROM copied_orders
                WHERE status IN ('dry_run', 'submitted', 'filled', 'partial')
                  AND created_at >= ?
                """,
                (_sqlite_utc(start_utc),),
            ).fetchall()
            settlement_rows = conn.execute(
                """
                SELECT payout_usd, realized_pnl, created_at
                FROM copied_redemptions
                WHERE created_at >= ?
                """,
                (_sqlite_utc(start_utc),),
            ).fetchall()

        for row in order_rows:
            day = _parse_db_utc(row["created_at"]).astimezone(tz).date().isoformat()
            bucket = rows_by_day.get(day)
            if not bucket:
                continue
            notional = float(row["requested_notional_usd"] or 0)
            if row["side"] == TradeSide.BUY.value:
                bucket["buy_count"] = int(bucket["buy_count"]) + 1
                bucket["buy_spend"] = float(bucket["buy_spend"]) + notional
                bucket["net_cashflow"] = float(bucket["net_cashflow"]) - notional
            elif row["side"] == TradeSide.SELL.value:
                bucket["sell_count"] = int(bucket["sell_count"]) + 1
                bucket["sell_notional"] = float(bucket["sell_notional"]) + notional
                bucket["net_cashflow"] = float(bucket["net_cashflow"]) + notional

        for row in settlement_rows:
            day = _parse_db_utc(row["created_at"]).astimezone(tz).date().isoformat()
            bucket = rows_by_day.get(day)
            if not bucket:
                continue
            payout = float(row["payout_usd"] or 0)
            pnl = float(row["realized_pnl"] or 0)
            bucket["settlement_count"] = int(bucket["settlement_count"]) + 1
            bucket["settlement_payout"] = float(bucket["settlement_payout"]) + payout
            bucket["settlement_pnl"] = float(bucket["settlement_pnl"]) + pnl
            bucket["net_cashflow"] = float(bucket["net_cashflow"]) + payout

        return list(reversed(list(rows_by_day.values())))

    def wallet_profile_names(self, wallets: list[str]) -> dict[str, str]:
        if not wallets:
            return {}
        placeholders = ",".join("?" for _ in wallets)
        params = [wallet.lower() for wallet in wallets]
        profiles: dict[str, str] = {}
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT source_wallet, raw_payload
                FROM target_trades
                WHERE lower(source_wallet) IN ({placeholders})
                ORDER BY timestamp DESC, id DESC
                """,
                params,
            ).fetchall()
        for row in rows:
            wallet = row["source_wallet"].lower()
            if wallet in profiles:
                continue
            try:
                payload = json.loads(row["raw_payload"])
            except json.JSONDecodeError:
                continue
            name = payload.get("name") or payload.get("username") or payload.get("profileName") or payload.get("pseudonym")
            if name:
                profiles[wallet] = str(name)
        return profiles

    def market_titles_for_trade_keys(self, dedupe_keys: list[str]) -> dict[str, str]:
        keys = [key for key in dedupe_keys if key]
        if not keys:
            return {}
        placeholders = ",".join("?" for _ in keys)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT dedupe_key, market_title
                FROM target_trades
                WHERE dedupe_key IN ({placeholders})
                  AND market_title IS NOT NULL
                  AND market_title != ''
                """,
                keys,
            ).fetchall()
        return {row["dedupe_key"]: row["market_title"] for row in rows}

    def upsert_wallet_pnl_points(
        self,
        wallet: str,
        interval: str,
        fidelity: str,
        points: list[tuple[int, float]],
    ) -> int:
        with self.connect() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT INTO wallet_pnl_points (wallet, interval, fidelity, timestamp, pnl, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(wallet, interval, fidelity, timestamp) DO UPDATE SET
                  pnl = excluded.pnl,
                  fetched_at = excluded.fetched_at
                """,
                [
                    (
                        wallet.lower(),
                        interval,
                        fidelity,
                        int(timestamp),
                        float(pnl),
                        datetime.now(timezone.utc).isoformat(),
                    )
                    for timestamp, pnl in points
                ],
            )
            return conn.total_changes - before

    def wallet_pnl_points(self, wallet: str, interval: str, fidelity: str, before_ts: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT timestamp, pnl
                    FROM wallet_pnl_points
                    WHERE lower(wallet) = lower(?)
                      AND interval = ?
                      AND fidelity = ?
                      AND timestamp < ?
                    ORDER BY timestamp ASC
                    """,
                    (wallet, interval, fidelity, before_ts),
                )
            )

    def has_wallet_pnl_timestamp(self, wallet: str, interval: str, fidelity: str, timestamp: int) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM wallet_pnl_points
                WHERE lower(wallet) = lower(?)
                  AND interval = ?
                  AND fidelity = ?
                  AND timestamp = ?
                """,
                (wallet, interval, fidelity, timestamp),
            ).fetchone()
        return row is not None


def _normalize_freeze_reason(reason: str) -> str:
    prefix = "source token frozen:"
    normalized = reason.strip()
    while normalized.lower().startswith(prefix):
        normalized = normalized[len(prefix) :].strip()
    return normalized or "risk mismatch"


def _local_day_bounds_utc(timezone_name: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(timezone_name)
    now = datetime.now(tz)
    start_local = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _sqlite_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_db_utc(value: str) -> datetime:
    text = value.strip()
    if "T" in text:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    else:
        parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _trade_from_row(row: sqlite3.Row) -> TradeEvent:
    return TradeEvent(
        source_wallet=row["source_wallet"],
        transaction_hash=row["transaction_hash"],
        timestamp=parse_timestamp(row["timestamp"]),
        market_id=row["market_id"],
        condition_id=row["condition_id"],
        asset_id=row["asset_id"],
        token_id=row["token_id"],
        market_title=row["market_title"],
        outcome=row["outcome"],
        side=TradeSide(row["side"]),
        price=row["price"],
        size=row["size"],
        notional_usd=row["notional_usd"],
        raw_payload=json.loads(row["raw_payload"]),
    )
