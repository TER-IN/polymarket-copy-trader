from config import Settings


def test_env_csv_wallets_parse(monkeypatch) -> None:
    monkeypatch.setenv("TARGET_WALLETS", "0xabc,0xdef")
    monkeypatch.setenv("BLOCK_MARKET_KEYWORDS", "sports, test market")

    settings = Settings()

    assert settings.target_wallets == ["0xabc", "0xdef"]
    assert settings.block_market_keywords == ["sports", "test market"]
