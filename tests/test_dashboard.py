import dashboard


def test_polymarket_market_url_accepts_event_slug() -> None:
    assert (
        dashboard._polymarket_market_url("eth-updown-5m-1780776900")
        == "https://polymarket.com/event/eth-updown-5m-1780776900"
    )


def test_polymarket_market_url_rejects_untrusted_value() -> None:
    assert dashboard._polymarket_market_url("market/../../other") is None
    assert dashboard._polymarket_market_url(None) is None
