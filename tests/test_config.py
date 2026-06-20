import pytest

from config import Settings
from models import CopyMode, OutcomeSelectionMode, RiskMismatchScope


def test_env_csv_wallets_parse(monkeypatch) -> None:
    monkeypatch.setenv("TARGET_WALLETS", "0xabc,0xdef")
    monkeypatch.setenv("ALLOW_MARKET_TITLE_KEYWORDS", "bitcoin, Ethereum")
    monkeypatch.setenv("BLOCK_MARKET_KEYWORDS", "sports, test market")

    settings = Settings()

    assert settings.target_wallets == ["0xabc", "0xdef"]
    assert settings.allow_market_title_keywords == ["bitcoin", "Ethereum"]
    assert settings.block_market_keywords == ["sports", "test market"]


def test_empty_daily_spend_cap_means_unlimited(monkeypatch) -> None:
    monkeypatch.setenv("DAILY_SPEND_CAP_USD", "")

    assert Settings().daily_spend_cap_usd is None


def test_wallet_market_risk_mismatch_scope_parses(monkeypatch) -> None:
    monkeypatch.setenv("RISK_MISMATCH_SCOPE", "wallet_market")

    assert Settings().risk_mismatch_scope == RiskMismatchScope.WALLET_MARKET


def test_inverse_share_copy_ratio_parses_and_validates(monkeypatch) -> None:
    monkeypatch.setenv("INVERSE_SHARE_COPY_RATIO", "0.05")
    assert Settings().inverse_share_copy_ratio == 0.05

    with pytest.raises(ValueError, match="INVERSE_SHARE_COPY_RATIO must be positive"):
        Settings(inverse_share_copy_ratio=0)


def test_inverse_down_underdog_settings_parse_and_validate(monkeypatch) -> None:
    monkeypatch.setenv("OUTCOME_SELECTION_MODE", "inverse_down_underdog")
    monkeypatch.setenv("INVERSE_DOWN_MAX_SOURCE_PRICE", "0.48")
    monkeypatch.setenv("MAX_COPIED_BUYS_PER_WALLET_MARKET", "5")
    monkeypatch.setenv("CONDITION_EXPOSURE_CAP_USD", "25")

    settings = Settings()

    assert settings.outcome_selection_mode == OutcomeSelectionMode.INVERSE_DOWN_UNDERDOG
    assert settings.inverse_down_max_source_price == 0.48
    assert settings.max_copied_buys_per_wallet_market == 5
    assert settings.condition_exposure_cap_usd == 25

    with pytest.raises(ValueError, match="INVERSE_DOWN_MAX_SOURCE_PRICE"):
        Settings(inverse_down_max_source_price=1)
    with pytest.raises(ValueError, match="MAX_COPIED_BUYS_PER_WALLET_MARKET"):
        Settings(max_copied_buys_per_wallet_market=0)
    with pytest.raises(ValueError, match="CONDITION_EXPOSURE_CAP_USD"):
        Settings(condition_exposure_cap_usd=0)


def test_shadow_regime_settings_parse_and_validate(monkeypatch) -> None:
    monkeypatch.setenv("OUTCOME_SELECTION_MODE", "shadow_regime_down_underdog")
    monkeypatch.setenv("SHADOW_REGIME_WINDOW", "50")
    monkeypatch.setenv("SHADOW_REGIME_CONFIRMATION_MARKETS", "10")

    settings = Settings()

    assert (
        settings.outcome_selection_mode
        == OutcomeSelectionMode.SHADOW_REGIME_DOWN_UNDERDOG
    )
    assert settings.shadow_regime_window == 50
    assert settings.shadow_regime_confirmation_markets == 10

    with pytest.raises(ValueError, match="SHADOW_REGIME_WINDOW"):
        Settings(shadow_regime_window=0)
    with pytest.raises(ValueError, match="SHADOW_REGIME_CONFIRMATION_MARKETS"):
        Settings(shadow_regime_confirmation_markets=0)

    with pytest.raises(ValueError, match="restricted to dry-run"):
        Settings(
            copy_mode=CopyMode.LIVE,
            outcome_selection_mode=OutcomeSelectionMode.SHADOW_REGIME_DOWN_UNDERDOG,
        ).validate_live_ready(True)
