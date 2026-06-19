from __future__ import annotations

import json
import logging
from datetime import timedelta, timezone

import typer
from rich.console import Console
from rich.table import Table

from config import Settings
from crowding import CrowdingAnalyzer
from db import Database
from decision_engine import DecisionEngine
from execution import Executor
from funds import DryRunBalanceProvider, LiveCollateralBalanceProvider
from ingestion import PollingIngestor
from models import CopyMode, utc_now
from models import TradeSide
from outcome_selection import OutcomeSelector
from polymarket_clob import PublicClobClient
from polymarket_data import PolymarketDataClient
from polymarket_gamma import GammaClient
from redemption import RedemptionExecutor
from resolution import ResolutionScanner
from settlement_audit import SettlementAuditor

app = typer.Typer(no_args_is_help=True)
console = Console()


def build_stack(settings: Settings) -> tuple[Database, PolymarketDataClient, PublicClobClient]:
    db = Database(settings.sqlite_path())
    data_client = PolymarketDataClient(settings.data_api_base_url)
    clob_client = PublicClobClient(settings.clob_base_url)
    return db, data_client, clob_client


def run(settings: Settings, risk_flag: bool = False) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings.validate_live_ready(risk_flag)
    db, data_client, clob_client = build_stack(settings)
    db.snapshot_settings(settings.model_dump_json(exclude={"polymarket_private_key"}))
    crowding = (
        CrowdingAnalyzer(data_client, settings.crowding_lookback_seconds)
        if settings.enable_crowding_check
        else None
    )
    gamma_client = GammaClient(settings.gamma_api_base_url)
    balance_provider = None
    if settings.copy_mode == CopyMode.LIVE:
        balance_provider = LiveCollateralBalanceProvider(settings)
    elif settings.dry_run_starting_balance_usd is not None:
        balance_provider = DryRunBalanceProvider(db, settings.dry_run_starting_balance_usd)
    ingestor = PollingIngestor(
        settings,
        db,
        data_client,
        DecisionEngine(
            settings,
            db,
            clob_client,
            balance_provider=balance_provider,
            market_end_provider=gamma_client,
        ),
        Executor(settings, db),
        resolution_scanner=ResolutionScanner(settings, db, gamma_client),
        crowding_analyzer=crowding,
        outcome_selector=OutcomeSelector(
            settings.outcome_selection_mode,
            gamma_client,
            settings.inverse_down_max_source_price,
        ),
    )
    ingestor.run_forever()


@app.command("run-dry")
def run_dry() -> None:
    """Run polling and copy decisions in dry-run mode."""
    settings = Settings(copy_mode=CopyMode.DRY_RUN)
    run(settings)


@app.command("run-live")
def run_live(
    i_understand_live_trading_risk: bool = typer.Option(
        False,
        "--i-understand-live-trading-risk",
        help="Required acknowledgement for authenticated trading.",
    )
) -> None:
    """Run authenticated live copy trading with hard safety checks."""
    settings = Settings(copy_mode=CopyMode.LIVE)
    run(settings, risk_flag=i_understand_live_trading_risk)


@app.command("backfill-wallet")
def backfill_wallet(wallet: str = typer.Option(...), days: int = typer.Option(1, min=1)) -> None:
    """Fetch recent trades for a wallet and persist unseen entries."""
    settings = Settings()
    db, data_client, _ = build_stack(settings)
    start = utc_now() - timedelta(days=days)
    inserted = 0
    for trade in data_client.recent_wallet_trades(wallet, limit=1000):
        if trade.timestamp >= start and db.insert_trade(trade):
            inserted += 1
    console.print(f"Inserted {inserted} unseen trades for {wallet}.")


@app.command("inspect-wallet")
def inspect_wallet(wallet: str = typer.Option(...)) -> None:
    """Show recent public Data API trades for a wallet without writing orders."""
    settings = Settings()
    data_client = PolymarketDataClient(settings.data_api_base_url)
    trades = data_client.recent_wallet_trades(wallet, limit=20)
    table = _trade_table()
    for trade in trades:
        _add_trade_row(table, trade.raw_payload.get("timestamp", trade.timestamp.isoformat()), trade)
    console.print(table)


@app.command("debug-wallet")
def debug_wallet(wallet: str = typer.Option(...), limit: int = typer.Option(20, min=1, max=100)) -> None:
    """Show latest Data API trades for a wallet and whether the local DB has seen them."""
    settings = Settings()
    db, data_client, _ = build_stack(settings)
    trades = data_client.recent_wallet_trades(wallet, limit=limit)
    table = Table(title=f"Data API Debug for {wallet}")
    for col in ("seen", "source", "age", "time", "side", "outcome", "price", "size", "notional", "market"):
        table.add_column(col)
    now = utc_now()
    for trade in trades:
        age = (now - trade.timestamp.astimezone(timezone.utc)).total_seconds()
        table.add_row(
            "yes" if db.has_trade(trade) else "no",
            str(trade.raw_payload.get("_api_source", "")),
            f"{age:.0f}s",
            trade.timestamp.isoformat(),
            trade.side.value,
            trade.outcome or "",
            f"{trade.price:.4f}",
            f"{trade.size:.4f}",
            f"{trade.notional_usd:.2f}",
            trade.market_title or "",
        )
    console.print(table)


@app.command("show-recent-trades")
def show_recent_trades(limit: int = typer.Option(20, min=1, max=200)) -> None:
    settings = Settings()
    db = Database(settings.sqlite_path())
    table = Table(title="Recent Target Trades")
    for col in ("time", "wallet", "side", "outcome", "price", "size", "notional", "market"):
        table.add_column(col)
    for row in db.recent_trades(limit):
        table.add_row(
            row["timestamp"],
            row["source_wallet"],
            row["side"],
            row["outcome"] or "",
            f"{row['price']:.4f}",
            f"{row['size']:.4f}",
            f"{row['notional_usd']:.2f}",
            row["market_title"] or "",
        )
    console.print(table)


@app.command("show-positions")
def show_positions() -> None:
    settings = Settings()
    db = Database(settings.sqlite_path())
    table = Table(title="Copied Positions")
    for col in ("market_id", "asset_id", "outcome", "shares", "avg", "cost", "status", "pnl", "sources"):
        table.add_column(col)
    for row in db.positions():
        table.add_row(
            row["market_id"],
            row["asset_id"],
            row["outcome"],
            f"{row['total_shares']:.4f}",
            f"{row['avg_entry_price']:.4f}",
            f"{row['total_cost']:.2f}",
            row["status"],
            f"{row['realized_pnl']:.2f}",
            ", ".join(json.loads(row["source_wallets"])),
        )
    console.print(table)


@app.command("show-dashboard")
def show_dashboard(limit: int = typer.Option(20, min=1, max=100)) -> None:
    """Show mark-to-market estimates for simulated/live copied positions."""
    settings = Settings()
    db, _, clob_client = build_stack(settings)

    position_table = Table(title="Copied Position Mark-to-Market")
    for col in ("outcome", "shares", "avg", "cost", "bid", "est value", "unrealized", "realized", "total", "market"):
        position_table.add_column(col)

    total_cost = 0.0
    total_value = 0.0
    total_realized = 0.0
    for row in db.position_dashboard_rows():
        bid = None
        if row["status"] == "open":
            try:
                quote = clob_client.get_quote(row["asset_id"])
                bid = quote.best_bid
            except Exception:
                bid = None
        if row["status"] == "redeem_required":
            est_value = row["total_shares"]
            unrealized = est_value - row["total_cost"]
        else:
            est_value = row["total_shares"] * bid if bid is not None else 0.0
            unrealized = est_value - row["total_cost"] if row["status"] == "open" else 0.0
        realized = row["realized_pnl"]
        total = unrealized + realized
        total_cost += row["total_cost"]
        total_value += est_value
        total_realized += realized
        position_table.add_row(
            row["outcome"],
            f"{row['total_shares']:.4f}",
            f"{row['avg_entry_price']:.4f}",
            f"{row['total_cost']:.2f}",
            "" if bid is None else f"{bid:.4f}",
            f"{est_value:.2f}",
            f"{unrealized:.2f}",
            f"{realized:.2f}",
            f"{total:.2f}",
            row["market_title"] or row["market_id"],
        )
    console.print(position_table)
    console.print(
        f"Copied totals: cost={total_cost:.2f}, est_value={total_value:.2f}, "
        f"unrealized={total_value - total_cost:.2f}, realized={total_realized:.2f}"
    )
    if settings.copy_mode == CopyMode.DRY_RUN and settings.dry_run_starting_balance_usd is not None:
        console.print(
            f"Available simulated cash: "
            f"{db.simulated_cash_balance(settings.dry_run_starting_balance_usd):.2f}"
        )

    source_table = Table(title="Copied Trades: Source vs Our Mark-to-Market Estimate")
    for col in (
        "time",
        "side",
        "outcome",
        "source px",
        "source shares",
        "source $",
        "our px",
        "our shares",
        "our $",
        "bid",
        "source pnl",
        "our pnl",
        "market",
    ):
        source_table.add_column(col)
    for row in db.copied_source_trade_rows(limit):
        if row["side"] != TradeSide.BUY.value:
            continue
        token_id = row["asset_id"] or row["token_id"]
        our_price = _order_entry_price(row)
        try:
            quote = clob_client.get_quote(token_id)
            bid = quote.best_bid
        except Exception:
            bid = None
        source_pnl = (row["size"] * bid) - row["notional_usd"] if bid is not None else None
        our_pnl = (
            ((bid - our_price) * row["requested_shares"])
            if bid is not None and our_price is not None and row["requested_shares"] is not None
            else None
        )
        source_table.add_row(
            row["timestamp"],
            row["side"],
            row["outcome"] or "",
            f"{row['price']:.4f}",
            f"{row['size']:.4f}",
            f"{row['notional_usd']:.2f}",
            "" if our_price is None else f"{our_price:.4f}",
            "" if row["requested_shares"] is None else f"{row['requested_shares']:.4f}",
            f"{row['requested_notional_usd']:.2f}",
            "" if bid is None else f"{bid:.4f}",
            "" if source_pnl is None else f"{source_pnl:.2f}",
            "" if our_pnl is None else f"{our_pnl:.2f}",
            row["market_title"] or "",
        )
    console.print(source_table)
    console.print(
        "Source columns describe the target wallet trade. Our columns describe the copied/simulated order. "
        "PnL is current-bid mark-to-market, not guaranteed execution or full realized PnL."
    )


@app.command("show-orders")
def show_orders(limit: int = typer.Option(20, min=1, max=200)) -> None:
    """Show source trader entry versus our copied/simulated entry for each copied order."""
    settings = Settings()
    db, _, clob_client = build_stack(settings)
    table = Table(title="Copied Orders: Source Entry vs Our Entry")
    for col in (
        "copied at",
        "status",
        "side",
        "source outcome",
        "copied outcome",
        "source px",
        "reference px",
        "our px",
        "diff",
        "source $",
        "our $",
        "our shares",
        "bid",
        "our mtm pnl",
        "market",
    ):
        table.add_column(col)

    for row in db.copied_order_rows(limit):
        our_price = _order_entry_price(row)
        source_price = row["source_price"]
        reference_price = row["reference_price"]
        diff = None if our_price is None or reference_price is None else our_price - reference_price
        token_id = row["token_id"]
        bid = None
        mtm_pnl = None
        if token_id and row["source_side"] == TradeSide.BUY.value and our_price is not None:
            try:
                bid = clob_client.get_quote(token_id).best_bid
            except Exception:
                bid = None
            if bid is not None and row["requested_shares"] is not None:
                mtm_pnl = (bid - our_price) * row["requested_shares"]
        table.add_row(
            row["copied_at"],
            row["copied_order_status"],
            row["source_side"] or "",
            row["source_outcome"] or "",
            row["copied_outcome"] or "",
            "" if source_price is None else f"{source_price:.4f}",
            "" if reference_price is None else f"{reference_price:.4f}",
            "" if our_price is None else f"{our_price:.4f}",
            "" if diff is None else f"{diff:+.4f}",
            "" if row["source_notional_usd"] is None else f"{row['source_notional_usd']:.2f}",
            f"{row['requested_notional_usd']:.2f}",
            "" if row["requested_shares"] is None else f"{row['requested_shares']:.4f}",
            "" if bid is None else f"{bid:.4f}",
            "" if mtm_pnl is None else f"{mtm_pnl:.2f}",
            row["market_title"] or "",
        )
    console.print(table)


@app.command("show-redemptions")
def show_redemptions(limit: int = typer.Option(20, min=1, max=200)) -> None:
    """Show copied dry-run/live redemption settlement records."""
    settings = Settings()
    db = Database(settings.sqlite_path())
    table = Table(title="Copied Redemptions")
    for col in ("created", "status", "shares", "payout", "realized", "source payout", "market", "error"):
        table.add_column(col)
    for row in db.copied_redemption_rows(limit):
        table.add_row(
            row["created_at"],
            row["status"],
            f"{row['shares']:.4f}",
            f"{row['payout_usd']:.2f}",
            f"{row['realized_pnl']:.2f}",
            "" if row["source_payout_usd"] is None else f"{row['source_payout_usd']:.2f}",
            row["market_title"] or row["market_id"],
            row["error_message"] or "",
        )
    console.print(table)


@app.command("refresh-redemptions")
def refresh_redemptions(
    wallet: str = typer.Option(...),
    limit: int = typer.Option(100, min=1, max=500),
) -> None:
    """Fetch recent source REDEEM activity and settle matching copied positions."""
    settings = Settings()
    db, data_client, _ = build_stack(settings)
    scanner = ResolutionScanner(settings, db, GammaClient(settings.gamma_api_base_url))
    executor = RedemptionExecutor(scanner)
    inserted = 0
    matched = 0
    for redemption in sorted(data_client.recent_wallet_redemptions(wallet, limit=limit), key=lambda item: item.timestamp):
        if db.insert_redemption(redemption):
            inserted += 1
        matched += executor.process_source_redemption(redemption)
    console.print(f"Inserted {inserted} source redemptions; authoritatively settled {matched} copied positions.")


@app.command("reconcile-settlements")
def reconcile_settlements(
    apply: bool = typer.Option(False, "--apply", help="Write authoritative corrections to the local database."),
    i_understand_this_updates_local_accounting: bool = typer.Option(
        False,
        "--i-understand-this-updates-local-accounting",
        help="Required together with --apply.",
    ),
) -> None:
    """Audit legacy source-redemption settlements against token-level market resolution."""
    if apply and not i_understand_this_updates_local_accounting:
        raise typer.BadParameter("--apply requires --i-understand-this-updates-local-accounting")

    settings = Settings()
    db = Database(settings.sqlite_path())
    auditor = SettlementAuditor(db, GammaClient(settings.gamma_api_base_url))
    corrections, unresolved_ids = auditor.audit_legacy_source_settlements(apply=apply)
    table = Table(title="Legacy Settlement Reconciliation")
    for col in ("id", "outcome", "old payout", "correct payout", "old pnl", "correct pnl", "delta", "market"):
        table.add_column(col)
    for correction in corrections:
        delta = correction.corrected_realized_pnl - correction.old_realized_pnl
        table.add_row(
            str(correction.settlement_id),
            correction.outcome,
            f"{correction.old_payout_usd:.2f}",
            f"{correction.corrected_payout_usd:.2f}",
            f"{correction.old_realized_pnl:.2f}",
            f"{correction.corrected_realized_pnl:.2f}",
            f"{delta:+.2f}",
            correction.market_title,
        )
    console.print(table)
    total_delta = sum(item.corrected_realized_pnl - item.old_realized_pnl for item in corrections)
    action = "Applied" if apply else "Previewed"
    console.print(
        f"{action} {len(corrections)} authoritative settlement results; "
        f"PnL adjustment={total_delta:+.2f}; unresolved={len(unresolved_ids)}."
    )
    if unresolved_ids:
        console.print(f"Unresolved settlement ids: {', '.join(map(str, unresolved_ids))}")


@app.command("refresh-resolutions")
def refresh_resolutions() -> None:
    """Scan local copied positions for resolved markets and settle local accounting."""
    settings = Settings()
    db = Database(settings.sqlite_path())
    settled = ResolutionScanner(settings, db, GammaClient(settings.gamma_api_base_url)).scan_once()
    console.print(f"Resolution scanner settled {settled} copied positions.")


@app.command("dashboard")
def serve_dashboard(
    host: str = typer.Option("127.0.0.1", help="Host/interface for the local dashboard server."),
    port: int = typer.Option(8765, min=1, max=65535, help="Port for the local dashboard server."),
) -> None:
    """Serve the read-only browser dashboard."""
    console.print(f"Starting read-only dashboard at http://{host}:{port}")
    import uvicorn

    uvicorn.run("dashboard:app", host=host, port=port)


@app.command("reset-simulation")
def reset_simulation(
    i_understand_this_deletes_local_simulation: bool = typer.Option(
        False,
        "--i-understand-this-deletes-local-simulation",
        help="Required acknowledgement before deleting local dry-run orders, positions, and crowding rows.",
    ),
    include_seen_trades: bool = typer.Option(
        False,
        "--include-seen-trades",
        help="Also delete stored target trades. Next run may treat old visible wallet history as new/seedable again.",
    ),
) -> None:
    """Delete local simulation state so the next dry-run starts clean."""
    if not i_understand_this_deletes_local_simulation:
        raise typer.BadParameter("pass --i-understand-this-deletes-local-simulation to confirm")
    settings = Settings()
    db = Database(settings.sqlite_path())
    deleted = db.reset_simulation(include_seen_trades=include_seen_trades)
    console.print("Deleted local rows: " + ", ".join(f"{table}={count}" for table, count in deleted.items()))


@app.command("show-crowding")
def show_crowding(
    wallet: str = typer.Option(...),
    limit: int = typer.Option(20, min=1, max=200),
    include_zero: bool = typer.Option(False, "--include-zero", help="Show zero-follower observations too."),
) -> None:
    settings = Settings()
    db = Database(settings.sqlite_path())
    table = Table(title=f"Suspected Copy Pressure for {wallet}")
    for col in (
        "created",
        "target time",
        "target",
        "target $",
        "followers",
        "follower $",
        "median delay",
        "avg slippage",
        "repeat wallets",
    ):
        table.add_column(col)
    for row in db.crowding_for_wallet(wallet, limit, include_zero=include_zero):
        target = " ".join(
            part
            for part in (
                row["target_side"] or "",
                row["target_outcome"] or "",
                "" if row["target_price"] is None else f"@{row['target_price']:.4f}",
                row["target_market_title"] or "",
            )
            if part
        )
        table.add_row(
            row["created_at"],
            row["target_timestamp"] or "",
            target,
            "" if row["target_notional_usd"] is None else f"{row['target_notional_usd']:.2f}",
            str(row["follower_count"]),
            f"{row['follower_notional']:.2f}",
            "" if row["median_delay_seconds"] is None else f"{row['median_delay_seconds']:.1f}s",
            "" if row["average_price_slippage_vs_target"] is None else f"{row['average_price_slippage_vs_target']:.4f}",
            ", ".join(json.loads(row["repeat_follower_wallets"])),
        )
    console.print(table)


@app.command("show-source-states")
def show_source_states(
    wallet: str | None = typer.Option(None),
    limit: int = typer.Option(100, min=1, max=500),
) -> None:
    """Show source token lifecycle states used by the risk-matching policy."""
    settings = Settings()
    db = Database(settings.sqlite_path())
    table = Table(title="Source Token Lifecycle States")
    for col in ("wallet", "status", "baseline", "observed", "outcome", "asset", "market", "reason", "details"):
        table.add_column(col)
    for row in db.source_token_states(wallet=wallet, limit=limit):
        table.add_row(
            row["source_wallet"],
            row["status"],
            f"{row['baseline_source_shares']:.4f}",
            f"{row['observed_source_shares']:.4f}",
            row["outcome"],
            row["asset_id"],
            row["market_id"],
            row["freeze_reason"] or "",
            _decision_detail_summary(json.loads(row["decision_details"] or "{}")),
        )
    console.print(table)


def _decision_detail_summary(details: dict) -> str:
    parts = []
    labels = (
        ("source_price", "source"),
        ("executable_price", "executable"),
        ("slippage_cents", "slippage_cents"),
        ("allowed_slippage_price", "allowed"),
        ("available_balance_usd", "balance"),
        ("market_end_time", "market_end"),
        ("seconds_until_market_end", "seconds_until_end"),
    )
    for key, label in labels:
        value = details.get(key)
        if value is not None:
            parts.append(f"{label}={value}")
    return "; ".join(parts)


@app.command("refresh-crowding")
def refresh_crowding(
    wallet: str = typer.Option(...),
    hours: int = typer.Option(24, min=1),
    limit: int = typer.Option(200, min=1, max=1000),
) -> None:
    """Recompute suspected copy pressure for stored target trades after their lookback window has closed."""
    settings = Settings()
    db, data_client, _ = build_stack(settings)
    analyzer = CrowdingAnalyzer(data_client, settings.crowding_lookback_seconds)
    since = utc_now() - timedelta(hours=hours)
    refreshed = 0
    skipped = 0

    for trade in db.target_trades_for_wallet(wallet, since.isoformat(), limit):
        age = (utc_now() - trade.timestamp.astimezone(timezone.utc)).total_seconds()
        if age < settings.crowding_lookback_seconds:
            skipped += 1
            continue
        if trade.notional_usd < settings.min_trade_usd:
            skipped += 1
            continue
        score, raw = analyzer.analyze(trade)
        db.record_crowding(trade, score, raw | {"refreshed": True})
        refreshed += 1

    console.print(f"Refreshed {refreshed} crowding observations for {wallet}; skipped {skipped}.")


def _trade_table() -> Table:
    table = Table(title="Wallet Trades")
    for col in ("time", "side", "outcome", "price", "size", "notional", "market"):
        table.add_column(col)
    return table


def _add_trade_row(table: Table, time_value: str, trade) -> None:
    table.add_row(
        str(time_value),
        trade.side.value,
        trade.outcome or "",
        f"{trade.price:.4f}",
        f"{trade.size:.4f}",
        f"{trade.notional_usd:.2f}",
        trade.market_title or "",
    )


def _order_entry_price(row) -> float | None:
    if row["avg_fill_price"] is not None:
        return float(row["avg_fill_price"])
    if row["requested_shares"] and row["requested_shares"] > 0:
        return float(row["requested_notional_usd"]) / float(row["requested_shares"])
    return row["limit_price"]


if __name__ == "__main__":
    app()
