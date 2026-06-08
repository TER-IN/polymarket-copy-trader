from datetime import datetime, timezone

import dashboard


class FakeDb:
    def __init__(self, rows):
        self.rows = rows

    def position_dashboard_rows(self):
        return self.rows


class NoQuoteClient:
    def get_quote(self, token_id):
        raise RuntimeError(f"no order book for {token_id}")


def position_row(**overrides):
    row = {
        "market_id": "m1",
        "asset_id": "tok1",
        "outcome": "Up",
        "total_shares": 10.0,
        "avg_entry_price": 0.5,
        "total_cost": 5.0,
        "realized_pnl": 0.0,
        "status": "open",
        "market_title": "Short Market",
        "event_slug": "short-market",
        "market_end_time": "2026-06-08T15:00:00+00:00",
        "resolution_resolved": 0,
        "resolution_checked_at": "2026-06-08T15:01:00+00:00",
    }
    row.update(overrides)
    return row


def test_polymarket_market_url_accepts_event_slug() -> None:
    assert (
        dashboard._polymarket_market_url("eth-updown-5m-1780776900")
        == "https://polymarket.com/event/eth-updown-5m-1780776900"
    )


def test_polymarket_market_url_rejects_untrusted_value() -> None:
    assert dashboard._polymarket_market_url("market/../../other") is None
    assert dashboard._polymarket_market_url(None) is None


def test_ended_position_without_quote_awaits_resolution_instead_of_showing_full_loss(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        dashboard,
        "utc_now",
        lambda: datetime(2026, 6, 8, 15, 2, tzinfo=timezone.utc),
    )

    rows, totals = dashboard._position_rows(
        FakeDb([position_row()]),
        NoQuoteClient(),
    )

    assert rows[0]["status"] == "awaiting_resolution"
    assert rows[0]["est_value"] is None
    assert rows[0]["unrealized"] is None
    assert rows[0]["total"] is None
    assert totals["unrealized"] == 0
    assert totals["total"] == 0
    assert totals["pending_valuation_cost"] == 5


def test_position_without_quote_before_end_remains_open_with_unknown_value(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        dashboard,
        "utc_now",
        lambda: datetime(2026, 6, 8, 14, 59, tzinfo=timezone.utc),
    )

    rows, _ = dashboard._position_rows(
        FakeDb([position_row()]),
        NoQuoteClient(),
    )

    assert rows[0]["status"] == "open"
    assert rows[0]["unrealized"] is None
