import csv
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from perp_signal_analysis import (
    Candle,
    CandleConflictError,
    PerpSignal,
    analyze_signals,
    build_summary,
    load_filled_btc_signals,
    load_tradingview_csvs,
)


def _write_csv(path: Path, rows: list[list[object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time", "open", "high", "low", "close", "Volume"])
        writer.writerows(rows)


def test_imports_iso_and_unix_candles_and_deduplicates_overlap(tmp_path) -> None:
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    _write_csv(
        first,
        [
            ["2026-07-28T13:01:20+02:00", 100, 101, 99, 100.5, 2],
            ["2026-07-28T13:01:30+02:00", 101, 102, 100, 101.5, 3],
        ],
    )
    _write_csv(
        second,
        [
            [1785236490, 101, 102, 100, 101.5, 3],
            [1785236500, 102, 103, 101, 102.5, 4],
        ],
    )

    candles, summary = load_tradingview_csvs([first, second])

    assert len(candles) == 3
    assert candles[0].time == datetime(2026, 7, 28, 11, 1, 20, tzinfo=timezone.utc)
    assert summary.interval_seconds == 10
    assert summary.duplicate_count == 1


def test_rejects_conflicting_overlapping_candles(tmp_path) -> None:
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    _write_csv(
        first,
        [
            [1785236480, 100, 101, 99, 100, 1],
            [1785236490, 101, 102, 100, 101, 1],
            [1785236500, 102, 103, 101, 102, 1],
        ],
    )
    _write_csv(
        second,
        [
            [1785236490, 999, 999, 999, 999, 1],
            [1785236500, 102, 103, 101, 102, 1],
            [1785236510, 103, 104, 102, 103, 1],
        ],
    )

    with pytest.raises(CandleConflictError):
        load_tradingview_csvs([first, second])


def test_newer_export_finalizes_older_exports_last_candle(tmp_path) -> None:
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    _write_csv(
        first,
        [
            [1785236480, 100, 101, 99, 100, 1],
            [1785236490, 101, 102, 100, 101, 1],
        ],
    )
    _write_csv(
        second,
        [
            [1785236490, 101, 105, 100, 104, 2],
            [1785236500, 104, 106, 103, 105, 1],
        ],
    )

    candles, summary = load_tradingview_csvs([first, second])

    corrected = next(candle for candle in candles if candle.time.timestamp() == 1785236490)
    assert corrected.high == 105
    assert corrected.close == 104
    assert summary.duplicate_count == 1
    assert summary.finalized_boundary_correction_count == 1


def test_detects_45_second_tradingview_interval(tmp_path) -> None:
    export = tmp_path / "45s.csv"
    _write_csv(
        export,
        [
            ["2026-07-28T11:00:00+00:00", 100, 101, 99, 100, 1],
            ["2026-07-28T11:00:45+00:00", 101, 102, 100, 101, 1],
            ["2026-07-28T11:01:30+00:00", 102, 103, 101, 102, 1],
        ],
    )

    _, summary = load_tradingview_csvs([export])

    assert summary.interval_seconds == 45


def test_matches_next_candle_open_and_classifies_up_down_and_tie() -> None:
    candles = [
        Candle(datetime(2026, 7, 28, 11, 1, 20, tzinfo=timezone.utc), 100, 100, 100, 100),
        Candle(datetime(2026, 7, 28, 11, 1, 30, tzinfo=timezone.utc), 101, 101, 101, 101),
        Candle(datetime(2026, 7, 28, 11, 1, 40, tzinfo=timezone.utc), 100, 100, 100, 100),
        Candle(datetime(2026, 7, 28, 11, 5, 0, tzinfo=timezone.utc), 103, 103, 103, 103),
    ]
    signals = [
        _signal(1, "Up", second=18),
        _signal(2, "Down", second=20),
        _signal(3, "Up", second=20, end_minute=1, end_second=40),
    ]

    matched, excluded = analyze_signals(signals, candles, interval_seconds=10)

    assert not excluded
    assert matched[0]["entry_candle_time"].endswith("11:01:20+00:00")
    assert matched[0]["result"] == "correct"
    assert matched[1]["result"] == "incorrect"
    assert matched[2]["result"] == "tie"
    assert matched[0]["invert_result"] == "incorrect"
    assert matched[0]["invert_gross_return_percent"] == -matched[0]["follow_gross_return_percent"]


def test_applies_round_trip_cost_to_both_strategy_directions() -> None:
    candles = [
        Candle(datetime(2026, 7, 28, 11, 1, 20, tzinfo=timezone.utc), 100, 100, 100, 100),
        Candle(datetime(2026, 7, 28, 11, 5, 0, tzinfo=timezone.utc), 101, 101, 101, 101),
    ]

    matched, _ = analyze_signals(
        [_signal(1, "Up", second=18)], candles, interval_seconds=10, round_trip_cost_bps=2
    )

    assert matched[0]["follow_gross_return_percent"] == pytest.approx(1)
    assert matched[0]["follow_net_return_percent"] == pytest.approx(0.98)
    assert matched[0]["invert_gross_return_percent"] == pytest.approx(-1)
    assert matched[0]["invert_net_return_percent"] == pytest.approx(-1.02)
    assert matched[0]["pnl_if_pro"] == pytest.approx(95.98)
    assert matched[0]["pnl_if_against"] == pytest.approx(-104.02)


def test_perp_pnl_maps_down_signal_to_short(tmp_path) -> None:
    candles = [
        Candle(datetime(2026, 7, 28, 11, 1, 20, tzinfo=timezone.utc), 100, 100, 100, 100),
        Candle(datetime(2026, 7, 28, 11, 5, 0, tzinfo=timezone.utc), 99, 99, 99, 99),
    ]

    matched, _ = analyze_signals([_signal(1, "Down", second=18)], candles, interval_seconds=10)

    assert matched[0]["pnl_if_pro"] == pytest.approx(96.02)
    assert matched[0]["pnl_if_against"] == pytest.approx(-103.98)


def test_excludes_gap_and_non_tradeable_holding_period() -> None:
    candles = [
        Candle(datetime(2026, 7, 28, 11, 1, 20, tzinfo=timezone.utc), 99, 99, 99, 99),
        Candle(datetime(2026, 7, 28, 11, 2, 0, tzinfo=timezone.utc), 100, 100, 100, 100),
        Candle(datetime(2026, 7, 28, 11, 5, 0, tzinfo=timezone.utc), 101, 101, 101, 101),
    ]
    gap_signal = replace(
        _signal(1, "Up", second=18),
        execution_time=datetime(2026, 7, 28, 11, 0, 18, tzinfo=timezone.utc),
    )
    signals = [gap_signal, _signal(2, "Up", second=11, end_minute=1, end_second=20)]

    _, excluded = analyze_signals(signals, candles, interval_seconds=10)

    assert {row["exclusion_reason"] for row in excluded} == {
        "entry_candle_too_late",
        "no_tradeable_holding_period",
    }


def test_loads_only_qualifying_signals_without_writing_database(tmp_path) -> None:
    database = tmp_path / "bot.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE target_trades (
          dedupe_key TEXT PRIMARY KEY, market_title TEXT
        );
        CREATE TABLE copied_orders (
          id INTEGER PRIMARY KEY, source_trade_key TEXT, recorded_at TEXT, created_at TEXT,
          outcome TEXT, market_id TEXT, status TEXT, side TEXT, filled_shares REAL
        );
        CREATE TABLE copy_decisions (
          source_trade_key TEXT PRIMARY KEY, created_at TEXT, details TEXT
        );
        """
    )
    details = json.dumps(
        {
            "decision_completed_at": "2026-07-28T11:01:18.859175+00:00",
            "market_end_time": "2026-07-28T11:05:00+00:00",
            "market_duration_seconds": 300,
        }
    )
    connection.execute("INSERT INTO target_trades VALUES (?, ?)", ("good", "Bitcoin Up or Down - test"))
    connection.execute("INSERT INTO target_trades VALUES (?, ?)", ("eth", "Ethereum Up or Down - test"))
    connection.execute(
        "INSERT INTO copied_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (1, "good", "2026-07-28T11:01:18.862743+00:00", "", "Up", "m1", "dry_run", "BUY", 2),
    )
    connection.execute(
        "INSERT INTO copied_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (2, "eth", "2026-07-28T11:01:18+00:00", "", "Up", "m2", "dry_run", "BUY", 2),
    )
    connection.execute("INSERT INTO copy_decisions VALUES (?, ?, ?)", ("good", "", details))
    connection.execute("INSERT INTO copy_decisions VALUES (?, ?, ?)", ("eth", "", details))
    connection.commit()
    connection.close()
    before = database.read_bytes()

    signals = load_filled_btc_signals(database)

    assert len(signals) == 1
    assert signals[0].execution_time.microsecond == 862743
    assert database.read_bytes() == before


def test_summary_reports_directional_accuracy_and_duration() -> None:
    signals = [_signal(1, "Up", second=20), _signal(2, "Down", second=20)]
    matched = [
        {"result": "correct", "signed_return_percent": 1.0, "market_duration_seconds": 300},
        {"result": "incorrect", "signed_return_percent": -0.5, "market_duration_seconds": 300},
    ]

    summary = build_summary(signals, matched, [])

    assert summary["directional_accuracy_percent"] == 50
    assert summary["by_market_duration"]["5m"]["accuracy_percent"] == 50
    assert summary["outcomes"] == {"Up": 1, "Down": 1}
    assert summary["strategies"]["follow_signal"]["directional_accuracy_percent"] == 50
    assert summary["strategies"]["invert_signal"]["directional_accuracy_percent"] == 50


def test_strategy_summary_reports_costs_daily_drawdown_and_holdout() -> None:
    signals = [_signal(index, "Up", second=20) for index in range(1, 11)]
    matched = []
    for index in range(10):
        gross = -0.02 if index < 7 else 0.03
        matched.append(
            {
                "execution_time": f"2026-07-{25 + index // 2:02d}T11:00:00+00:00",
                "result": "correct" if gross > 0 else "incorrect",
                "signed_return_percent": gross,
                "market_duration_seconds": 300,
                "entry_delay_into_market_seconds": 90,
            }
        )

    summary = build_summary(signals, matched, [], round_trip_cost_bps=1)
    follow = summary["strategies"]["follow_signal"]
    invert = summary["strategies"]["invert_signal"]

    assert follow["chronological_split"]["training"]["signals"] == 7
    assert follow["chronological_split"]["holdout"]["signals"] == 3
    assert follow["chronological_split"]["training"]["directional_accuracy_percent"] == 0
    assert follow["chronological_split"]["holdout"]["directional_accuracy_percent"] == 100
    assert invert["directional_accuracy_percent"] == 70
    assert follow["break_even_round_trip_cost_bps"] == pytest.approx(-0.5)
    assert follow["average_net_return_percent"] == pytest.approx(-0.015)
    assert follow["sequential_max_drawdown_net_percent"] > 0
    assert len(follow["daily_stability"]) == 5


def _signal(
    order_id: int,
    outcome: str,
    second: int,
    end_minute: int = 5,
    end_second: int = 0,
) -> PerpSignal:
    return PerpSignal(
        order_id=order_id,
        execution_time=datetime(2026, 7, 28, 11, 1, second, tzinfo=timezone.utc),
        market_end_time=datetime(2026, 7, 28, 11, end_minute, end_second, tzinfo=timezone.utc),
        outcome=outcome,
        market_title="Bitcoin Up or Down - test",
        market_id=f"m{order_id}",
        market_duration_seconds=300,
    )
