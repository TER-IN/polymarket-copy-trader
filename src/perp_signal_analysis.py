from __future__ import annotations

import csv
import json
import math
import sqlite3
from bisect import bisect_left
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Iterable

from models import parse_timestamp


@dataclass(frozen=True)
class Candle:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None


@dataclass(frozen=True)
class PerpSignal:
    order_id: int
    execution_time: datetime
    market_end_time: datetime
    outcome: str
    market_title: str
    market_id: str
    market_duration_seconds: float | None


@dataclass(frozen=True)
class ImportSummary:
    files: list[str]
    candle_count: int
    first_time: str
    last_time: str
    interval_seconds: float
    duplicate_count: int
    finalized_boundary_correction_count: int
    gap_count: int


class CandleConflictError(ValueError):
    pass


def load_tradingview_csvs(paths: Iterable[Path]) -> tuple[list[Candle], ImportSummary]:
    files = sorted({Path(path).resolve() for path in paths})
    if not files:
        raise ValueError("no TradingView CSV files were supplied")

    by_time: dict[datetime, Candle] = {}
    source_end_by_time: dict[datetime, datetime] = {}
    duplicates = 0
    boundary_corrections = 0
    for path in files:
        if not path.is_file():
            raise ValueError(f"TradingView CSV does not exist: {path}")
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            headers = {name.strip().lower(): name for name in (reader.fieldnames or [])}
            required = {"time", "open", "high", "low", "close"}
            missing = required - headers.keys()
            if missing:
                raise ValueError(f"{path}: missing columns: {', '.join(sorted(missing))}")
            volume_header = headers.get("volume")
            parsed_candles: list[Candle] = []
            for line_number, row in enumerate(reader, start=2):
                try:
                    timestamp = _parse_csv_timestamp(row[headers["time"]])
                    candle = Candle(
                        time=timestamp,
                        open=float(row[headers["open"]]),
                        high=float(row[headers["high"]]),
                        low=float(row[headers["low"]]),
                        close=float(row[headers["close"]]),
                        volume=(
                            float(row[volume_header])
                            if volume_header and row.get(volume_header, "").strip()
                            else None
                        ),
                    )
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{path}:{line_number}: invalid candle: {exc}") from exc
                parsed_candles.append(candle)
        if not parsed_candles:
            raise ValueError(f"{path}: contains no candles")
        file_end = max(candle.time for candle in parsed_candles)
        for candle in parsed_candles:
            timestamp = candle.time
            previous = by_time.get(timestamp)
            if previous is None:
                by_time[timestamp] = candle
                source_end_by_time[timestamp] = file_end
                continue
            duplicates += 1
            previous_source_end = source_end_by_time[timestamp]
            if previous == candle:
                source_end_by_time[timestamp] = max(previous_source_end, file_end)
                continue
            if timestamp == previous_source_end and file_end > timestamp:
                by_time[timestamp] = candle
                source_end_by_time[timestamp] = file_end
                boundary_corrections += 1
                continue
            if timestamp == file_end and previous_source_end > timestamp:
                boundary_corrections += 1
                continue
            raise CandleConflictError(
                f"conflicting completed candle at {timestamp.isoformat()} in {path}"
            )

    candles = sorted(by_time.values(), key=lambda item: item.time)
    if len(candles) < 2:
        raise ValueError("at least two distinct candles are required")
    differences = [
        (right.time - left.time).total_seconds()
        for left, right in zip(candles, candles[1:])
    ]
    interval = float(median(differences))
    if interval <= 0:
        raise ValueError("could not infer a positive candle interval")
    gaps = sum(difference > interval * 1.5 for difference in differences)
    return candles, ImportSummary(
        files=[str(path) for path in files],
        candle_count=len(candles),
        first_time=candles[0].time.isoformat(),
        last_time=candles[-1].time.isoformat(),
        interval_seconds=interval,
        duplicate_count=duplicates,
        finalized_boundary_correction_count=boundary_corrections,
        gap_count=gaps,
    )


def load_filled_btc_signals(database_path: Path) -> list[PerpSignal]:
    path = Path(database_path).resolve()
    if not path.is_file():
        raise ValueError(f"source database does not exist: {path}")
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            """
            SELECT
              co.id AS order_id,
              co.recorded_at,
              co.created_at AS order_created_at,
              co.outcome,
              co.market_id,
              tt.market_title,
              cd.created_at AS decision_created_at,
              json_extract(cd.details, '$.decision_completed_at') AS decision_completed_at,
              json_extract(cd.details, '$.market_end_time') AS market_end_time,
              json_extract(cd.details, '$.market_duration_seconds') AS market_duration_seconds
            FROM copied_orders co
            JOIN target_trades tt ON tt.dedupe_key = co.source_trade_key
            JOIN copy_decisions cd ON cd.source_trade_key = co.source_trade_key
            WHERE co.status = 'dry_run'
              AND co.side = 'BUY'
              AND co.filled_shares > 0
              AND lower(co.outcome) IN ('up', 'down')
              AND tt.market_title LIKE 'Bitcoin Up or Down%'
            ORDER BY co.id
            """
        ).fetchall()
    finally:
        connection.close()

    signals: list[PerpSignal] = []
    for row in rows:
        execution_value = (
            row["recorded_at"]
            or row["decision_completed_at"]
            or row["order_created_at"]
            or row["decision_created_at"]
        )
        if not execution_value or not row["market_end_time"]:
            continue
        execution = _utc(parse_timestamp(execution_value))
        market_end = _utc(parse_timestamp(row["market_end_time"]))
        signals.append(
            PerpSignal(
                order_id=int(row["order_id"]),
                execution_time=execution,
                market_end_time=market_end,
                outcome=str(row["outcome"]).title(),
                market_title=str(row["market_title"] or ""),
                market_id=str(row["market_id"] or ""),
                market_duration_seconds=(
                    float(row["market_duration_seconds"])
                    if row["market_duration_seconds"] is not None
                    else None
                ),
            )
        )
    return signals


def analyze_signals(
    signals: Iterable[PerpSignal],
    candles: list[Candle],
    interval_seconds: float,
    round_trip_cost_bps: float = 0.0,
    perp_margin_usd: float = 100.0,
    perp_leverage: float = 100.0,
    taker_fee_percent: float = 0.02,
    tolerance_seconds: float = 0.5,
) -> tuple[list[dict], list[dict]]:
    if round_trip_cost_bps < 0:
        raise ValueError("round-trip cost cannot be negative")
    if perp_margin_usd <= 0 or perp_leverage <= 0:
        raise ValueError("perp margin and leverage must be positive")
    if taker_fee_percent < 0:
        raise ValueError("taker fee cannot be negative")
    candle_times = [candle.time for candle in candles]
    matched: list[dict] = []
    excluded: list[dict] = []
    maximum_delay = interval_seconds + tolerance_seconds
    for signal in signals:
        base = _signal_fields(signal)
        if signal.execution_time >= signal.market_end_time:
            excluded.append(base | {"exclusion_reason": "execution_not_before_market_end"})
            continue
        entry = _first_candle_at_or_after(candles, candle_times, signal.execution_time)
        end = _first_candle_at_or_after(candles, candle_times, signal.market_end_time)
        if entry is None:
            excluded.append(base | {"exclusion_reason": "missing_entry_candle"})
            continue
        if end is None:
            excluded.append(base | {"exclusion_reason": "missing_end_candle"})
            continue
        entry_delay = (entry.time - signal.execution_time).total_seconds()
        end_delay = (end.time - signal.market_end_time).total_seconds()
        if entry_delay > maximum_delay:
            excluded.append(base | {"exclusion_reason": "entry_candle_too_late"})
            continue
        if end_delay > maximum_delay:
            excluded.append(base | {"exclusion_reason": "end_candle_too_late"})
            continue
        if entry.time >= end.time:
            excluded.append(base | {"exclusion_reason": "no_tradeable_holding_period"})
            continue

        change = end.open - entry.open
        follow_change = change if signal.outcome == "Up" else -change
        follow_result = (
            "tie" if change == 0 else ("correct" if follow_change > 0 else "incorrect")
        )
        follow_return = (follow_change / entry.open) * 100
        invert_return = -follow_return
        cost_percent = round_trip_cost_bps / 100
        pro_side = "long" if signal.outcome == "Up" else "short"
        against_side = "short" if pro_side == "long" else "long"
        pnl_if_pro = _perp_endpoint_pnl(
            entry.open,
            end.open,
            pro_side,
            perp_margin_usd,
            perp_leverage,
            taker_fee_percent,
        )
        pnl_if_against = _perp_endpoint_pnl(
            entry.open,
            end.open,
            against_side,
            perp_margin_usd,
            perp_leverage,
            taker_fee_percent,
        )
        matched.append(
            base
            | {
                "entry_candle_time": entry.time.isoformat(),
                "entry_delay_seconds": entry_delay,
                "entry_price": entry.open,
                "end_candle_time": end.time.isoformat(),
                "end_delay_seconds": end_delay,
                "end_price": end.open,
                "price_change": change,
                "return_percent": (change / entry.open) * 100,
                "signed_return_percent": follow_return,
                "result": follow_result,
                "directionally_compatible_with_perp": follow_result == "correct",
                "follow_result": follow_result,
                "follow_gross_return_percent": follow_return,
                "follow_net_return_percent": follow_return - cost_percent,
                "invert_result": _invert_result(follow_result),
                "invert_gross_return_percent": invert_return,
                "invert_net_return_percent": invert_return - cost_percent,
                "round_trip_cost_bps": round_trip_cost_bps,
                "pnl_if_against": pnl_if_against,
                "pnl_if_pro": pnl_if_pro,
            }
        )
    return matched, excluded


def build_summary(
    signals: list[PerpSignal],
    matched: list[dict],
    excluded: list[dict],
    round_trip_cost_bps: float = 0.0,
) -> dict:
    correct = sum(row["result"] == "correct" for row in matched)
    incorrect = sum(row["result"] == "incorrect" for row in matched)
    ties = sum(row["result"] == "tie" for row in matched)
    directional = correct + incorrect
    low, high = _wilson_interval(correct, directional)
    returns = [float(row["signed_return_percent"]) for row in matched]
    by_duration: dict[str, dict[str, int | float | None]] = {}
    for row in matched:
        label = _duration_label(row["market_duration_seconds"])
        bucket = by_duration.setdefault(label, {"matched": 0, "correct": 0, "incorrect": 0, "ties": 0})
        bucket["matched"] += 1
        bucket[row["result"]] += 1
    for bucket in by_duration.values():
        denominator = int(bucket["correct"]) + int(bucket["incorrect"])
        bucket["accuracy_percent"] = (
            float(bucket["correct"]) / denominator * 100 if denominator else None
        )
    exclusions: dict[str, int] = {}
    for row in excluded:
        reason = str(row["exclusion_reason"])
        exclusions[reason] = exclusions.get(reason, 0) + 1
    by_entry_delay: dict[str, dict[str, int | float | None]] = {}
    for row in matched:
        label = _entry_delay_label(row.get("entry_delay_into_market_seconds"))
        bucket = by_entry_delay.setdefault(
            label, {"matched": 0, "correct": 0, "incorrect": 0, "ties": 0}
        )
        bucket["matched"] += 1
        bucket[row["result"]] += 1
    for bucket in by_entry_delay.values():
        denominator = int(bucket["correct"]) + int(bucket["incorrect"])
        bucket["accuracy_percent"] = (
            float(bucket["correct"]) / denominator * 100 if denominator else None
        )
    return {
        "qualifying_signals": len(signals),
        "matched_signals": len(matched),
        "excluded_signals": len(excluded),
        "correct": correct,
        "incorrect": incorrect,
        "ties": ties,
        "directional_accuracy_percent": correct / directional * 100 if directional else None,
        "accuracy_95_percent_confidence_interval": (
            [low * 100, high * 100] if directional else None
        ),
        "average_signed_return_percent": sum(returns) / len(returns) if returns else None,
        "median_signed_return_percent": median(returns) if returns else None,
        "outcomes": {
            "Up": sum(signal.outcome == "Up" for signal in signals),
            "Down": sum(signal.outcome == "Down" for signal in signals),
        },
        "exclusions_by_reason": exclusions,
        "by_market_duration": by_duration,
        "by_entry_delay_into_market": by_entry_delay,
        "round_trip_cost_bps": round_trip_cost_bps,
        "strategies": {
            "follow_signal": _strategy_summary(matched, "follow", round_trip_cost_bps),
            "invert_signal": _strategy_summary(matched, "invert", round_trip_cost_bps),
        },
    }


def write_analysis_outputs(
    output_directory: Path,
    configuration: dict,
    import_summary: ImportSummary,
    matched: list[dict],
    excluded: list[dict],
    summary: dict,
) -> None:
    output_directory.mkdir(parents=True, exist_ok=False)
    _write_json(output_directory / "configuration.json", configuration)
    _write_json(output_directory / "imported_candles_summary.json", asdict(import_summary))
    _write_json(output_directory / "summary.json", summary)
    _write_csv(output_directory / "signals.csv", matched)
    _write_csv(output_directory / "excluded_signals.csv", excluded)
    (output_directory / "report.md").write_text(
        _markdown_report(configuration, import_summary, summary, matched), encoding="utf-8"
    )


def _parse_csv_timestamp(value: str) -> datetime:
    text = str(value).strip()
    if not text:
        raise ValueError("empty timestamp")
    if _looks_numeric(text):
        number = float(text)
        if not math.isfinite(number):
            raise ValueError(f"invalid numeric timestamp: {text}")
        return _utc(parse_timestamp(number))
    return _utc(parse_timestamp(text))


def _looks_numeric(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _first_candle_at_or_after(
    candles: list[Candle], times: list[datetime], target: datetime
) -> Candle | None:
    index = bisect_left(times, target)
    return candles[index] if index < len(candles) else None


def _signal_fields(signal: PerpSignal) -> dict:
    seconds_until_end = (signal.market_end_time - signal.execution_time).total_seconds()
    delay_into_market = (
        signal.market_duration_seconds - seconds_until_end
        if signal.market_duration_seconds is not None
        else None
    )
    return {
        "order_id": signal.order_id,
        "execution_time": signal.execution_time.isoformat(),
        "market_end_time": signal.market_end_time.isoformat(),
        "outcome": signal.outcome,
        "market_title": signal.market_title,
        "market_id": signal.market_id,
        "market_duration_seconds": signal.market_duration_seconds,
        "seconds_until_market_end": seconds_until_end,
        "entry_delay_into_market_seconds": delay_into_market,
    }


def _duration_label(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    minutes = seconds / 60
    for expected in (5, 15, 30, 60):
        if abs(minutes - expected) <= 1:
            return f"{expected}m"
    return f"{minutes:g}m"


def _entry_delay_label(seconds: object) -> str:
    if seconds is None:
        return "unknown"
    value = max(0.0, float(seconds))
    if value < 30:
        return "0-30s"
    if value < 60:
        return "30-60s"
    if value < 120:
        return "1-2m"
    return "2m+"


def _invert_result(result: str) -> str:
    if result == "correct":
        return "incorrect"
    if result == "incorrect":
        return "correct"
    return "tie"


def _strategy_summary(rows: list[dict], prefix: str, cost_bps: float) -> dict:
    cost_percent = cost_bps / 100
    observations = [_strategy_observation(row, prefix, cost_percent) for row in rows]
    results = [item[0] for item in observations]
    gross = [item[1] for item in observations]
    net = [item[2] for item in observations]
    correct = results.count("correct")
    incorrect = results.count("incorrect")
    ties = results.count("tie")
    directional = correct + incorrect
    low, high = _wilson_interval(correct, directional)
    split_index = int(len(observations) * 0.7)
    if len(observations) > 1:
        split_index = min(max(split_index, 1), len(observations) - 1)
    chronological = sorted(
        zip(rows, observations), key=lambda item: str(item[0].get("execution_time", ""))
    )
    training = [item[1] for item in chronological[:split_index]]
    holdout = [item[1] for item in chronological[split_index:]]
    sensitivity_costs = sorted({0.0, 1.0, 2.0, 4.0, 6.0, 10.0, float(cost_bps)})
    return {
        "correct": correct,
        "incorrect": incorrect,
        "ties": ties,
        "directional_accuracy_percent": correct / directional * 100 if directional else None,
        "accuracy_95_percent_confidence_interval": [low * 100, high * 100] if directional else None,
        "average_gross_return_percent": _mean(gross),
        "median_gross_return_percent": median(gross) if gross else None,
        "average_net_return_percent": _mean(net),
        "median_net_return_percent": median(net) if net else None,
        "break_even_round_trip_cost_bps": _mean(gross) * 100 if gross else None,
        "profitable_after_cost_count": sum(value > 0 for value in net),
        "nonprofitable_after_cost_count": sum(value <= 0 for value in net),
        "fixed_notional_total_gross_return_percent": sum(gross),
        "fixed_notional_total_net_return_percent": sum(net),
        "sequential_compounded_gross_return_percent": _compound(gross),
        "sequential_compounded_net_return_percent": _compound(net),
        "sequential_max_drawdown_gross_percent": _maximum_drawdown(gross),
        "sequential_max_drawdown_net_percent": _maximum_drawdown(net),
        "daily_stability": _daily_strategy_summary(rows, observations),
        "by_market_duration": _strategy_breakdown(rows, observations, "market_duration_seconds"),
        "by_entry_delay_into_market": _strategy_breakdown(
            rows, observations, "entry_delay_into_market_seconds"
        ),
        "chronological_split": {
            "training_fraction": 0.7,
            "training": _observation_summary(training),
            "holdout": _observation_summary(holdout),
        },
        "cost_sensitivity": {
            f"{sensitivity:g}": _cost_sensitivity(gross, sensitivity)
            for sensitivity in sensitivity_costs
        },
    }


def _strategy_observation(row: dict, prefix: str, cost_percent: float) -> tuple[str, float, float]:
    follow_result = str(row.get("follow_result", row["result"]))
    follow_gross = float(row.get("follow_gross_return_percent", row["signed_return_percent"]))
    if prefix == "follow":
        return follow_result, follow_gross, float(
            row.get("follow_net_return_percent", follow_gross - cost_percent)
        )
    invert_result = str(row.get("invert_result", _invert_result(follow_result)))
    invert_gross = float(row.get("invert_gross_return_percent", -follow_gross))
    return invert_result, invert_gross, float(
        row.get("invert_net_return_percent", invert_gross - cost_percent)
    )


def _observation_summary(observations: list[tuple[str, float, float]]) -> dict:
    correct = sum(result == "correct" for result, _, _ in observations)
    incorrect = sum(result == "incorrect" for result, _, _ in observations)
    ties = sum(result == "tie" for result, _, _ in observations)
    directional = correct + incorrect
    gross = [gross_value for _, gross_value, _ in observations]
    net = [net_value for _, _, net_value in observations]
    return {
        "signals": len(observations),
        "correct": correct,
        "incorrect": incorrect,
        "ties": ties,
        "directional_accuracy_percent": correct / directional * 100 if directional else None,
        "average_gross_return_percent": _mean(gross),
        "average_net_return_percent": _mean(net),
        "fixed_notional_total_net_return_percent": sum(net),
    }


def _daily_strategy_summary(
    rows: list[dict], observations: list[tuple[str, float, float]]
) -> dict[str, dict]:
    grouped: dict[str, list[tuple[str, float, float]]] = {}
    for row, observation in zip(rows, observations):
        day = str(row.get("execution_time", "unknown"))[:10]
        grouped.setdefault(day, []).append(observation)
    return {day: _observation_summary(items) for day, items in sorted(grouped.items())}


def _strategy_breakdown(
    rows: list[dict], observations: list[tuple[str, float, float]], field: str
) -> dict[str, dict]:
    grouped: dict[str, list[tuple[str, float, float]]] = {}
    for row, observation in zip(rows, observations):
        value = row.get(field)
        label = _duration_label(value) if field == "market_duration_seconds" else _entry_delay_label(value)
        grouped.setdefault(label, []).append(observation)
    return {label: _observation_summary(items) for label, items in grouped.items()}


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _compound(returns_percent: list[float]) -> float:
    equity = 1.0
    for value in returns_percent:
        equity *= 1 + value / 100
    return (equity - 1) * 100


def _maximum_drawdown(returns_percent: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    maximum = 0.0
    for value in returns_percent:
        equity *= 1 + value / 100
        peak = max(peak, equity)
        maximum = max(maximum, (peak - equity) / peak * 100)
    return maximum


def _perp_endpoint_pnl(
    entry_price: float,
    exit_price: float,
    side: str,
    margin_usd: float,
    leverage: float,
    taker_fee_percent: float,
) -> float:
    notional_usd = margin_usd * leverage
    quantity = notional_usd / entry_price
    gross_pnl = quantity * (
        exit_price - entry_price if side == "long" else entry_price - exit_price
    )
    fee_rate = taker_fee_percent / 100
    entry_fee = quantity * entry_price * fee_rate
    exit_fee = quantity * exit_price * fee_rate
    return gross_pnl - entry_fee - exit_fee


def _cost_sensitivity(gross_returns_percent: list[float], cost_bps: float) -> dict:
    net = [value - cost_bps / 100 for value in gross_returns_percent]
    return {
        "round_trip_cost_bps": cost_bps,
        "average_net_return_percent": _mean(net),
        "profitable_after_cost_count": sum(value > 0 for value in net),
        "fixed_notional_total_net_return_percent": sum(net),
        "sequential_compounded_net_return_percent": _compound(net),
        "sequential_max_drawdown_net_percent": _maximum_drawdown(net),
    }


def _wilson_interval(successes: int, total: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if total == 0:
        return 0.0, 0.0
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    spread = z * math.sqrt(
        proportion * (1 - proportion) / total + z * z / (4 * total * total)
    ) / denominator
    return center - spread, center + spread


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _markdown_report(
    configuration: dict, imported: ImportSummary, summary: dict, matched: list[dict]
) -> str:
    accuracy = summary["directional_accuracy_percent"]
    confidence = summary["accuracy_95_percent_confidence_interval"]
    lines = [
        "# Perp Signal Direction Analysis",
        "",
        f"- Symbol: `{configuration['symbol']}`",
        f"- Candle coverage: `{imported.first_time}` to `{imported.last_time}`",
        f"- Detected interval: `{imported.interval_seconds:g}` seconds",
        f"- Duplicate candles deduplicated: `{imported.duplicate_count}`",
        f"- Finalized boundary candles corrected: `{imported.finalized_boundary_correction_count}`",
        f"- Detected candle gaps: `{imported.gap_count}`",
        f"- Qualifying signals: `{summary['qualifying_signals']}`",
        f"- Matched signals: `{summary['matched_signals']}`",
        f"- Excluded signals: `{summary['excluded_signals']}`",
        f"- Correct / incorrect / ties: `{summary['correct']} / {summary['incorrect']} / {summary['ties']}`",
    ]
    if accuracy is not None and confidence is not None:
        lines.extend(
            [
                f"- Directional accuracy: `{accuracy:.2f}%`",
                f"- 95% Wilson interval: `{confidence[0]:.2f}%–{confidence[1]:.2f}%`",
            ]
        )
    lines.extend(
        [
        f"- Modeled round-trip cost: `{summary['round_trip_cost_bps']:.2f}` bps",
        f"- Hypothetical perp position: `${configuration['perp_margin_usd']:.2f}` margin × "
        f"`{configuration['perp_leverage']:g}` leverage = `${configuration['perp_notional_usd']:.2f}` notional",
        f"- Market-order taker fee: `{configuration['taker_fee_percent_per_fill']:.4f}%` per fill; slippage: `0%`",
            "",
            "## Follow versus invert",
            "",
            "| Strategy | Accuracy | Avg gross | Avg net | Break-even cost | Fixed-notional net total | Compounded net | Max drawdown |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for name, strategy in summary["strategies"].items():
        lines.append(
            f"| {name} | {_percent(strategy['directional_accuracy_percent'])} | "
            f"{_percent(strategy['average_gross_return_percent'])} | "
            f"{_percent(strategy['average_net_return_percent'])} | "
            f"{_bps(strategy['break_even_round_trip_cost_bps'])} | "
            f"{_percent(strategy['fixed_notional_total_net_return_percent'])} | "
            f"{_percent(strategy['sequential_compounded_net_return_percent'])} | "
            f"{_percent(strategy['sequential_max_drawdown_net_percent'])} |"
        )
    lines.extend(["", "## Round-trip cost sensitivity", ""])
    for name, strategy in summary["strategies"].items():
        lines.extend(
            [
                f"### {name}",
                "",
                "| Cost | Avg net | Profitable trades | Fixed-notional net total | Compounded net | Max drawdown |",
                "|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for sensitivity in strategy["cost_sensitivity"].values():
            lines.append(
                f"| {sensitivity['round_trip_cost_bps']:.2f} bps | "
                f"{_percent(sensitivity['average_net_return_percent'])} | "
                f"{sensitivity['profitable_after_cost_count']} | "
                f"{_percent(sensitivity['fixed_notional_total_net_return_percent'])} | "
                f"{_percent(sensitivity['sequential_compounded_net_return_percent'])} | "
                f"{_percent(sensitivity['sequential_max_drawdown_net_percent'])} |"
            )
        lines.append("")
    lines.extend(["", "## Outcome coverage", ""])
    for outcome, count in summary["outcomes"].items():
        lines.append(f"- {outcome}: `{count}`")
    if summary["outcomes"]["Down"] == 0:
        lines.append("- Warning: this dataset contains no Down signals, so it cannot validate short signals.")
    lines.extend(["", "## By market duration", ""])
    lines.extend(
        [
            "| Strategy | Duration | Signals | Accuracy | Avg gross | Avg net |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for name, strategy in summary["strategies"].items():
        for duration, bucket in sorted(strategy["by_market_duration"].items()):
            lines.append(
                f"| {name} | {duration} | {bucket['signals']} | "
                f"{_percent(bucket['directional_accuracy_percent'])} | "
                f"{_percent(bucket['average_gross_return_percent'])} | "
                f"{_percent(bucket['average_net_return_percent'])} |"
            )
    lines.extend(["", "## By entry delay into market", ""])
    lines.extend(
        [
            "| Strategy | Delay | Signals | Accuracy | Avg gross | Avg net |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for name, strategy in summary["strategies"].items():
        for delay, bucket in strategy["by_entry_delay_into_market"].items():
            lines.append(
                f"| {name} | {delay} | {bucket['signals']} | "
                f"{_percent(bucket['directional_accuracy_percent'])} | "
                f"{_percent(bucket['average_gross_return_percent'])} | "
                f"{_percent(bucket['average_net_return_percent'])} |"
            )
    if summary["exclusions_by_reason"]:
        lines.extend(["", "## Exclusions", ""])
        for reason, count in sorted(summary["exclusions_by_reason"].items()):
            lines.append(f"- `{reason}`: {count}")
    lines.extend(["", "## Chronological 70/30 split", ""])
    for name, strategy in summary["strategies"].items():
        training = strategy["chronological_split"]["training"]
        holdout = strategy["chronological_split"]["holdout"]
        lines.extend(
            [
                f"### {name}",
                "",
                f"- Training: {training['signals']} signals, "
                f"{_percent(training['directional_accuracy_percent'])} accuracy, "
                f"{_percent(training['average_net_return_percent'])} average net return.",
                f"- Holdout: {holdout['signals']} signals, "
                f"{_percent(holdout['directional_accuracy_percent'])} accuracy, "
                f"{_percent(holdout['average_net_return_percent'])} average net return.",
                "",
            ]
        )
    lines.extend(["## Daily stability", ""])
    for name, strategy in summary["strategies"].items():
        lines.extend(
            [
                f"### {name}",
                "",
                "| UTC day | Signals | Accuracy | Avg gross | Avg net | Fixed-notional net total |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for day, daily in strategy["daily_stability"].items():
            lines.append(
                f"| {day} | {daily['signals']} | {_percent(daily['directional_accuracy_percent'])} | "
                f"{_percent(daily['average_gross_return_percent'])} | "
                f"{_percent(daily['average_net_return_percent'])} | "
                f"{_percent(daily['fixed_notional_total_net_return_percent'])} |"
            )
        lines.append("")
    lines.extend(
        [
            "",
            "## Chronological results",
            "",
            "| Execution UTC | Outcome | Entry | End | PnL pro | PnL against | Follow gross/net | Follow | Invert gross/net | Invert | Market |",
            "|---|---:|---:|---:|---:|---:|---:|---|---:|---|---|",
        ]
    )
    for row in sorted(matched, key=lambda item: item["execution_time"]):
        title = str(row["market_title"]).replace("|", "\\|")
        lines.append(
            f"| {row['execution_time']} | {row['outcome']} | {row['entry_price']:.2f} | "
            f"{row['end_price']:.2f} | ${row['pnl_if_pro']:+.2f} | ${row['pnl_if_against']:+.2f} | "
            f"{row['follow_gross_return_percent']:+.4f}% / "
            f"{row['follow_net_return_percent']:+.4f}% | {row['follow_result']} | "
            f"{row['invert_gross_return_percent']:+.4f}% / {row['invert_net_return_percent']:+.4f}% | "
            f"{row['invert_result']} | {title} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "A correct result means the MEXC BTC perpetual moved in the Polymarket signal's direction "
            "between the first available candle after bot execution and the market-end candle. "
            "The PnL columns subtract both taker fees and assume zero slippage, but they are endpoint estimates. "
            "Funding, maintenance margin, and intratrade liquidation are not modeled; at 100x leverage, a position may "
            "liquidate before the recorded exit even when endpoint PnL appears profitable.",
            "",
        ]
    )
    return "\n".join(lines)


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.4f}%"


def _bps(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.3f} bps"
