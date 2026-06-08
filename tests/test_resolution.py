from datetime import datetime, timezone

from config import Settings
from db import Database
from models import CopyMode, PositionStatus
from polymarket_gamma import GammaClient, MarketResolution
from positions import apply_buy
from redemption import RedemptionExecutor
from resolution import ResolutionScanner
from settlement_audit import SettlementAuditor


class FakeGamma(GammaClient):
    def __init__(self, payout: float):
        self.payout = payout

    def get_resolution(self, market_id: str, asset_id: str, condition_id: str | None = None) -> MarketResolution:
        return MarketResolution(
            resolved=True,
            payout_by_token_id={asset_id: self.payout},
            market_title="Resolved Market",
            raw_payload={},
        )


class FakeLookupGamma(GammaClient):
    def __init__(self):
        self.market_id_calls: list[str] = []

    def _get_market_by_id(self, market_id: str):
        self.market_id_calls.append(market_id)
        raise AssertionError("condition ids should not be queried through /markets/{id}")

    def _get_clob_market_by_condition_id(self, condition_id: str):
        return None

    def _get_markets(self, **params: str):
        if params.get("condition_ids") or params.get("clob_token_ids"):
            return [
                {
                    "question": "Resolved Market",
                    "closed": True,
                    "clobTokenIds": '["tok1", "tok2"]',
                    "outcomePrices": '["1", "0"]',
                }
            ]
        return []


def test_gamma_client_does_not_query_condition_id_as_market_id() -> None:
    gamma = FakeLookupGamma()
    condition_id = "0x" + "a" * 64

    resolution = gamma.get_resolution(condition_id, "tok1")

    assert resolution is not None
    assert resolution.resolved
    assert resolution.payout_for_token("tok1") == 1.0
    assert gamma.market_id_calls == []


def test_gamma_client_uses_clob_market_resolution_for_closed_condition() -> None:
    class FakeClobGamma(FakeLookupGamma):
        def _get_clob_market_by_condition_id(self, condition_id: str):
            return {
                "closed": True,
                "question": "XRP Up or Down",
                "tokens": [
                    {"token_id": "up", "outcome": "Up", "price": 1, "winner": True},
                    {"token_id": "down", "outcome": "Down", "price": 0, "winner": False},
                ],
            }

        def _get_markets(self, **params: str):
            raise AssertionError("Gamma fallback should not be needed when CLOB resolves token payout")

    resolution = FakeClobGamma().get_resolution("0x" + "b" * 64, "down")

    assert resolution is not None
    assert resolution.resolved
    assert resolution.payout_for_token("down") == 0.0
    assert resolution.payout_for_token("up") == 1.0


def test_gamma_client_reads_strict_up_down_token_pair_from_clob() -> None:
    class FakePairGamma(FakeLookupGamma):
        def _get_clob_market_by_condition_id(self, condition_id: str):
            return {
                "tokens": [
                    {"token_id": "up", "outcome": "Up"},
                    {"token_id": "down", "outcome": "Down"},
                ]
            }

    pair = FakePairGamma().get_up_down_tokens("0x" + "d" * 64, "down")

    assert pair is not None
    assert [(token.token_id, token.outcome) for token in pair] == [("up", "Up"), ("down", "Down")]


def test_gamma_client_rejects_non_up_down_pair() -> None:
    class FakePairGamma(FakeLookupGamma):
        def _get_clob_market_by_condition_id(self, condition_id: str):
            return {
                "tokens": [
                    {"token_id": "yes", "outcome": "Yes"},
                    {"token_id": "no", "outcome": "No"},
                ]
            }

        def _find_market(self, market_id: str, asset_id: str, condition_id: str | None):
            return None

    assert FakePairGamma().get_up_down_tokens("0x" + "e" * 64, "no") is None


def test_gamma_client_rejects_up_down_pair_with_duplicate_token_id() -> None:
    class FakePairGamma(FakeLookupGamma):
        def _get_clob_market_by_condition_id(self, condition_id: str):
            return {
                "tokens": [
                    {"token_id": "same", "outcome": "Up"},
                    {"token_id": "same", "outcome": "Down"},
                ]
            }

        def _find_market(self, market_id: str, asset_id: str, condition_id: str | None):
            return None

    assert FakePairGamma().get_up_down_tokens("0x" + "f" * 64, "same") is None


def test_gamma_client_reads_advertised_market_end_time() -> None:
    class FakeEndTimeGamma(GammaClient):
        def _find_market(self, market_id: str, asset_id: str, condition_id: str | None):
            return {"endDate": "2030-01-02T03:04:05Z"}

    end_time = FakeEndTimeGamma().get_market_end_time("m1", "tok1", "c1")

    assert end_time == datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def test_gamma_metadata_uses_event_start_for_market_duration() -> None:
    class FakeMetadataGamma(GammaClient):
        def _find_market(self, market_id: str, asset_id: str, condition_id: str | None):
            return {
                "startDate": "2026-06-07T15:08:24Z",
                "eventStartTime": "2026-06-08T15:00:00Z",
                "endDate": "2026-06-08T15:05:00Z",
                "clobTokenIds": '["up", "down"]',
                "outcomes": '["Up", "Down"]',
            }

    metadata = FakeMetadataGamma().get_market_metadata("m1", "up", "c1")

    assert metadata is not None
    assert metadata.start_time == datetime(2026, 6, 8, 15, 0, tzinfo=timezone.utc)
    assert metadata.duration_seconds == 300


def test_gamma_metadata_falls_back_to_start_date() -> None:
    class FakeMetadataGamma(GammaClient):
        def _find_market(self, market_id: str, asset_id: str, condition_id: str | None):
            return {
                "startDate": "2026-06-08T15:00:00Z",
                "endDate": "2026-06-08T15:15:00Z",
                "clobTokenIds": '["up", "down"]',
                "outcomes": '["Up", "Down"]',
            }

    metadata = FakeMetadataGamma().get_market_metadata("m1", "up", "c1")

    assert metadata is not None
    assert metadata.duration_seconds == 900


def test_gamma_client_does_not_fallback_when_clob_market_is_still_open() -> None:
    class FakeOpenClobGamma(FakeLookupGamma):
        def _get_clob_market_by_condition_id(self, condition_id: str):
            return {
                "closed": False,
                "question": "Still Open",
                "tokens": [{"token_id": "up", "outcome": "Up", "price": 0.5}],
            }

        def _get_markets(self, **params: str):
            raise AssertionError("Open CLOB market should not trigger slow Gamma fallback")

    resolution = FakeOpenClobGamma().get_resolution("0x" + "c" * 64, "up")

    assert resolution is not None
    assert not resolution.resolved


def test_resolution_scanner_settles_dry_run_win(tmp_path) -> None:
    settings = Settings(copy_mode=CopyMode.DRY_RUN)
    db = Database(tmp_path / "db.sqlite3")
    db.upsert_position(apply_buy(None, "m1", "tok1", "Yes", 10, 0.9, "0xabc"))

    settled = ResolutionScanner(settings, db, FakeGamma(1.0)).scan_once()

    assert settled == 1
    position = db.get_position("m1", "tok1", "Yes")
    assert position is not None
    assert position.status == PositionStatus.RESOLVED
    assert position.total_shares == 0
    assert position.realized_pnl == 1


def test_resolution_scanner_settles_dry_run_loss(tmp_path) -> None:
    settings = Settings(copy_mode=CopyMode.DRY_RUN)
    db = Database(tmp_path / "db.sqlite3")
    db.upsert_position(apply_buy(None, "m1", "tok1", "Yes", 10, 0.9, "0xabc"))

    settled = ResolutionScanner(settings, db, FakeGamma(0.0)).scan_once()

    assert settled == 1
    position = db.get_position("m1", "tok1", "Yes")
    assert position is not None
    assert position.status == PositionStatus.RESOLVED
    assert position.total_shares == 0
    assert position.realized_pnl == -9


def test_resolution_scanner_records_outcome_for_rejected_decision(tmp_path) -> None:
    from models import TradeEvent, TradeSide

    settings = Settings(copy_mode=CopyMode.DRY_RUN)
    db = Database(tmp_path / "db.sqlite3")
    trade = TradeEvent(
        source_wallet="0xabc",
        transaction_hash="0xrejected",
        timestamp=datetime.now(timezone.utc),
        market_id="m1",
        condition_id="m1",
        asset_id="tok1",
        token_id="tok1",
        market_title="Rejected Market",
        outcome="Yes",
        side=TradeSide.BUY,
        price=0.5,
        size=10,
        notional_usd=5,
        raw_payload={},
    )
    db.insert_trade(trade)
    db.record_copy_decision(
        trade,
        False,
        "maximum net upside below minimum",
        {"market_end_time": "2020-01-01T00:00:00+00:00"},
    )

    assert ResolutionScanner(settings, db, FakeGamma(1.0)).scan_once() == 0

    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM market_resolution_observations WHERE market_id = 'm1'"
        ).fetchone()
    assert row is not None
    assert row["resolved"] == 1
    assert '"tok1": 1.0' in row["payout_by_token_id"]


def test_source_redemption_uses_authoritative_token_resolution(tmp_path) -> None:
    from models import RedemptionEvent

    settings = Settings(copy_mode=CopyMode.DRY_RUN)
    db = Database(tmp_path / "db.sqlite3")
    db.upsert_position(apply_buy(None, "m1", "up", "Up", 10, 0.4, "0xabc"))
    db.upsert_position(apply_buy(None, "m1", "down", "Down", 10, 0.6, "0xabc"))

    class TwoOutcomeGamma(GammaClient):
        def get_resolution(self, market_id: str, asset_id: str, condition_id: str | None = None):
            return MarketResolution(
                resolved=True,
                payout_by_token_id={"up": 1.0, "down": 0.0},
                market_title="Market",
                raw_payload={},
            )

    redemption = RedemptionEvent(
        source_wallet="0xabc",
        transaction_hash="0xredeem",
        timestamp=datetime.now(timezone.utc),
        market_id="m1",
        condition_id="m1",
        asset_id=None,
        token_id=None,
        market_title="Market",
        outcome=None,
        size=100,
        payout_usd=100,
        raw_payload={},
    )

    settled = RedemptionExecutor(ResolutionScanner(settings, db, TwoOutcomeGamma())).process_source_redemption(redemption)

    assert settled == 2
    up = db.get_position("m1", "up", "Up")
    down = db.get_position("m1", "down", "Down")
    assert up is not None and up.realized_pnl == 6
    assert down is not None and down.realized_pnl == -6


def test_source_redemption_waits_when_authoritative_resolution_is_unavailable(tmp_path) -> None:
    from models import RedemptionEvent

    settings = Settings(copy_mode=CopyMode.DRY_RUN)
    db = Database(tmp_path / "db.sqlite3")
    db.upsert_position(apply_buy(None, "m1", "down", "Down", 10, 0.6, "0xabc"))

    class UnresolvedGamma(GammaClient):
        def get_resolution(self, market_id: str, asset_id: str, condition_id: str | None = None):
            return MarketResolution(resolved=False)

    redemption = RedemptionEvent(
        source_wallet="0xabc",
        transaction_hash="0xredeem",
        timestamp=datetime.now(timezone.utc),
        market_id="m1",
        condition_id="m1",
        asset_id=None,
        token_id=None,
        market_title="Market",
        outcome=None,
        size=100,
        payout_usd=100,
        raw_payload={},
    )

    settled = RedemptionExecutor(ResolutionScanner(settings, db, UnresolvedGamma())).process_source_redemption(redemption)

    assert settled == 0
    position = db.get_position("m1", "down", "Down")
    assert position is not None
    assert position.status == PositionStatus.OPEN
    assert position.total_shares == 10


def test_legacy_settlement_reconciliation_corrects_opposite_outcomes(tmp_path) -> None:
    from models import RedemptionEvent

    db = Database(tmp_path / "db.sqlite3")
    db.upsert_position(apply_buy(None, "m1", "up", "Up", 10, 0.4, "0xabc"))
    db.upsert_position(apply_buy(None, "m1", "down", "Down", 10, 0.6, "0xabc"))
    redemption = RedemptionEvent(
        source_wallet="0xabc",
        transaction_hash="0xredeem",
        timestamp=datetime.now(timezone.utc),
        market_id="m1",
        condition_id="m1",
        asset_id=None,
        token_id=None,
        market_title="Market",
        outcome=None,
        size=100,
        payout_usd=100,
        raw_payload={},
    )
    db.insert_redemption(redemption)
    for position in db.copied_positions_for_redemption(redemption):
        db.settle_position_from_redemption(redemption, position, "dry_run")

    class TwoOutcomeGamma(GammaClient):
        def get_resolution(self, market_id: str, asset_id: str, condition_id: str | None = None):
            return MarketResolution(
                resolved=True,
                payout_by_token_id={"up": 1.0, "down": 0.0},
                market_title="Market",
                raw_payload={"tokens": []},
            )

    corrections, unresolved = SettlementAuditor(db, TwoOutcomeGamma()).audit_legacy_source_settlements(apply=True)

    assert len(corrections) == 2
    assert unresolved == []
    up = db.get_position("m1", "up", "Up")
    down = db.get_position("m1", "down", "Down")
    assert up is not None and up.realized_pnl == 6
    assert down is not None and down.realized_pnl == -6
    rows = db.copied_redemption_rows()
    assert {row["status"] for row in rows} == {"dry_run_resolution"}


def test_resolution_scanner_marks_live_winner_redeem_required(tmp_path) -> None:
    settings = Settings(copy_mode=CopyMode.LIVE)
    db = Database(tmp_path / "db.sqlite3")
    db.upsert_position(apply_buy(None, "m1", "tok1", "Yes", 10, 0.9, "0xabc"))

    settled = ResolutionScanner(settings, db, FakeGamma(1.0)).scan_once()

    assert settled == 1
    position = db.get_position("m1", "tok1", "Yes")
    assert position is not None
    assert position.status == PositionStatus.REDEEM_REQUIRED
    assert position.total_shares == 10
    assert position.realized_pnl == 0


def test_resolution_scanner_settles_live_loss(tmp_path) -> None:
    settings = Settings(copy_mode=CopyMode.LIVE)
    db = Database(tmp_path / "db.sqlite3")
    db.upsert_position(apply_buy(None, "m1", "tok1", "Yes", 10, 0.9, "0xabc"))

    settled = ResolutionScanner(settings, db, FakeGamma(0.0)).scan_once()

    assert settled == 1
    position = db.get_position("m1", "tok1", "Yes")
    assert position is not None
    assert position.status == PositionStatus.RESOLVED
    assert position.total_shares == 0
    assert position.realized_pnl == -9
