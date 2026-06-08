from datetime import datetime, timezone

import pytest

from models import OutcomeSelectionMode, TradeEvent, TradeSide
from outcome_selection import OutcomeSelectionError, OutcomeSelector
from polymarket_gamma import OutcomeToken


class FakeTokenProvider:
    def __init__(self, pair):
        self.pair = pair
        self.calls = 0

    def get_up_down_tokens(self, market_id: str, asset_id: str, condition_id: str | None = None):
        self.calls += 1
        return self.pair


def make_trade(asset_id: str = "down", outcome: str = "Down", price: float = 0.6) -> TradeEvent:
    return TradeEvent(
        source_wallet="0xabc",
        transaction_hash="0xtx",
        timestamp=datetime.now(timezone.utc),
        market_id="condition",
        condition_id="condition",
        asset_id=asset_id,
        token_id=asset_id,
        market_title="Ethereum Up or Down",
        outcome=outcome,
        side=TradeSide.BUY,
        price=price,
        size=100,
        notional_usd=60,
        raw_payload={},
    )


def test_inverse_up_down_selects_opposite_token_and_reference_price() -> None:
    provider = FakeTokenProvider((OutcomeToken("up", "Up"), OutcomeToken("down", "Down")))
    selector = OutcomeSelector(OutcomeSelectionMode.INVERSE_UP_DOWN, provider)

    selected = selector.select(make_trade())

    assert selected.asset_id == "up"
    assert selected.token_id == "up"
    assert selected.outcome == "Up"
    assert selected.price == pytest.approx(0.4)
    assert selected.raw_payload["_outcome_selection"]["source_outcome"] == "Down"
    assert selected.raw_payload["_outcome_selection"]["copied_outcome"] == "Up"


def test_inverse_up_down_rejects_non_pair_and_unknown_source_token() -> None:
    no_pair = OutcomeSelector(OutcomeSelectionMode.INVERSE_UP_DOWN, FakeTokenProvider(None))
    with pytest.raises(OutcomeSelectionError, match="not an authoritative"):
        no_pair.select(make_trade())

    pair = (OutcomeToken("up", "Up"), OutcomeToken("down", "Down"))
    unknown = OutcomeSelector(OutcomeSelectionMode.INVERSE_UP_DOWN, FakeTokenProvider(pair))
    with pytest.raises(OutcomeSelectionError, match="does not belong"):
        unknown.select(make_trade(asset_id="other"))


def test_inverse_up_down_caches_pair_by_condition() -> None:
    provider = FakeTokenProvider((OutcomeToken("up", "Up"), OutcomeToken("down", "Down")))
    selector = OutcomeSelector(OutcomeSelectionMode.INVERSE_UP_DOWN, provider)

    selector.select(make_trade())
    selector.select(make_trade(asset_id="up", outcome="Up", price=0.4))

    assert provider.calls == 1


def test_source_mode_returns_original_trade_without_metadata_lookup() -> None:
    provider = FakeTokenProvider(None)
    source = make_trade()

    selected = OutcomeSelector(OutcomeSelectionMode.SOURCE, provider).select(source)

    assert selected is source
    assert provider.calls == 0
