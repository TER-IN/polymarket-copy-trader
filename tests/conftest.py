import pytest


@pytest.fixture(autouse=True)
def isolate_runtime_controls(monkeypatch, tmp_path):
    monkeypatch.setenv("STOP_TRADING_FILE", str(tmp_path / "STOP_TRADING"))
    monkeypatch.setenv("MAX_SECONDS_UNTIL_MARKET_END", "")
    monkeypatch.setenv("MARKET_TYPE_FILTER", "all")
