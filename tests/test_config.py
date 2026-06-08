from config import Settings


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
