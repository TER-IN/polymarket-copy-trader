from datetime import datetime, timezone

import dashboard
from db import Database


class ExplodingPnlClient:
    def get_user_pnl(self, wallet: str, interval: str = "all", fidelity: str = "1d"):
        raise AssertionError("remote PnL API should not be called when yesterday is cached")


def test_source_performance_uses_db_when_yesterday_cached(tmp_path, monkeypatch) -> None:
    db = Database(tmp_path / "db.sqlite3")
    fake_now = datetime(2026, 6, 3, 12, tzinfo=timezone.utc)
    yesterday = int(datetime(2026, 6, 2, tzinfo=timezone.utc).timestamp())
    db.upsert_wallet_pnl_points("0xabc", "all", "1d", [(yesterday, 12.5)])
    monkeypatch.setattr(dashboard, "utc_now", lambda: fake_now)

    data = dashboard._source_performance(["0xabc"], db, ExplodingPnlClient())

    assert data[0]["fetched"] is False
    assert data[0]["current_day_excluded"] is True
    assert data[0]["latest_pnl"] == 12.5
