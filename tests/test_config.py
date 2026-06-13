import pytest

from config import Settings
from models import RiskMismatchScope


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
