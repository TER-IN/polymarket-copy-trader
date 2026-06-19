from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from config import Settings
from db import Database
from models import TradeSide, utc_now
from polymarket_clob import PublicClobClient
from polymarket_pnl import PnlPoint, UserPnlClient, daily_candles


app = FastAPI(title="Polymarket Copy Trader Dashboard")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return HTML


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "time": utc_now().isoformat()}


@app.get("/api/summary")
def summary() -> dict[str, Any]:
    settings = Settings()
    db = Database(settings.sqlite_path())
    clob_client = PublicClobClient(settings.clob_base_url)
    pnl_client = UserPnlClient(settings.user_pnl_api_base_url)

    positions, position_totals = _position_rows(db, clob_client)
    copied_orders = _copied_order_rows(db, clob_client)
    copied_redemptions = _copied_redemption_rows(db)
    recent_trades = [_row_dict(row) for row in db.recent_trades(25)]
    source_states = [_row_dict(row) for row in db.source_token_states(limit=50)]
    errors = [_row_dict(row) for row in db.recent_errors(20)]
    wallet_profiles = db.wallet_profile_names(settings.target_wallets)
    _apply_wallet_profiles(recent_trades, "source_wallet", wallet_profiles)
    _apply_wallet_profiles(source_states, "source_wallet", wallet_profiles)
    _apply_source_state_market_labels(source_states, db)
    source_performance = _source_performance(settings.target_wallets, db, pnl_client)
    for entry in source_performance:
        entry["wallet_name"] = wallet_profiles.get(str(entry["wallet"]).lower(), _short_wallet(entry["wallet"]))

    return {
        "generated_at": utc_now().isoformat(),
        "mode": settings.copy_mode.value,
        "target_wallets": settings.target_wallets,
        "stop_trading": Path(settings.stop_trading_file).exists(),
        "settings": {
            "copy_ratio": settings.copy_ratio,
            "inverse_share_copy_ratio": settings.inverse_share_copy_ratio,
            "inverse_down_max_source_price": settings.inverse_down_max_source_price,
            "max_copied_buys_per_wallet_market": (
                settings.max_copied_buys_per_wallet_market
            ),
            "max_trade_usd": settings.max_trade_usd,
            "min_trade_usd": settings.min_trade_usd,
            "max_trade_age_seconds": settings.max_trade_age_seconds,
            "max_slippage_cents": settings.max_slippage_cents,
            "max_buy_price": settings.max_buy_price,
            "max_seconds_until_market_end": settings.max_seconds_until_market_end,
            "market_type_filter": settings.market_type_filter.value,
            "up_down_min_duration_seconds": settings.up_down_min_duration_seconds,
            "up_down_max_duration_seconds": settings.up_down_max_duration_seconds,
            "min_net_upside_usd": settings.min_net_upside_usd,
            "min_net_upside_percent": settings.min_net_upside_percent,
            "allow_market_title_keywords": settings.allow_market_title_keywords,
            "dry_run_starting_balance_usd": settings.dry_run_starting_balance_usd,
            "outcome_selection_mode": settings.outcome_selection_mode.value,
            "daily_spend_cap_usd": settings.daily_spend_cap_usd,
            "trading_day_timezone": settings.trading_day_timezone,
            "per_market_exposure_cap_usd": settings.per_market_exposure_cap_usd,
            "condition_exposure_cap_usd": settings.condition_exposure_cap_usd,
            "enable_resolution_scanner": settings.enable_resolution_scanner,
            "resolution_scan_interval_seconds": settings.resolution_scan_interval_seconds,
            "source_position_policy": settings.source_position_policy.value,
            "sell_sizing_mode": settings.sell_sizing_mode.value,
            "on_risk_mismatch": settings.on_risk_mismatch.value,
            "risk_mismatch_scope": settings.risk_mismatch_scope.value,
        },
        "totals": {
            **position_totals,
            "spend_today": db.spend_today(settings.trading_day_timezone),
            "available_cash": (
                db.simulated_cash_balance(settings.dry_run_starting_balance_usd)
                if settings.copy_mode.value == "dry_run" and settings.dry_run_starting_balance_usd is not None
                else None
            ),
            "open_positions": len(positions),
            "copied_orders": len(copied_orders),
            "redemptions": len(copied_redemptions),
            "tracked_tokens": len(source_states),
            "recent_errors": len(errors),
        },
        "positions": positions,
        "daily_performance": db.daily_performance_rows(settings.trading_day_timezone, days=45),
        "copied_orders": copied_orders,
        "copied_redemptions": copied_redemptions,
        "recent_trades": recent_trades,
        "source_states": source_states,
        "source_performance": source_performance,
        "wallet_profiles": wallet_profiles,
        "errors": errors,
    }


def _apply_wallet_profiles(rows: list[dict[str, Any]], key: str, profiles: dict[str, str]) -> None:
    for row in rows:
        wallet = str(row.get(key) or "")
        row["wallet_name"] = profiles.get(wallet.lower(), _short_wallet(wallet))


def _short_wallet(wallet: str) -> str:
    if len(wallet) > 14:
        return f"{wallet[:8]}...{wallet[-6:]}"
    return wallet


def _polymarket_market_url(event_slug: Any) -> str | None:
    slug = str(event_slug or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", slug):
        return None
    return f"https://polymarket.com/event/{slug}"


def _apply_source_state_market_labels(rows: list[dict[str, Any]], db: Database) -> None:
    titles = db.market_titles_for_trade_keys([str(row.get("last_source_trade_key") or "") for row in rows])
    for row in rows:
        title = titles.get(str(row.get("last_source_trade_key") or ""))
        row["market_label"] = title or row.get("market_id") or ""


def _copied_redemption_rows(db: Database) -> list[dict[str, Any]]:
    rows = [_row_dict(row) for row in db.copied_redemption_rows(None)]
    for row in rows:
        row["market_title"] = row.get("market_title") or "Unknown market"
        row["market_url"] = _polymarket_market_url(row.get("event_slug"))
    return rows


def _source_performance(wallets: list[str], db: Database, pnl_client: UserPnlClient) -> list[dict[str, Any]]:
    performance = []
    interval = "all"
    fidelity = "1d"
    today_start = _utc_day_start(utc_now())
    yesterday_start = int((datetime.fromtimestamp(today_start, tz=timezone.utc) - timedelta(days=1)).timestamp())
    for wallet in wallets:
        try:
            fetched = False
            if not db.has_wallet_pnl_timestamp(wallet, interval, fidelity, yesterday_start):
                remote_points = pnl_client.get_user_pnl(wallet, interval=interval, fidelity=fidelity)
                completed_points = [
                    (point.timestamp, point.pnl)
                    for point in remote_points
                    if point.timestamp < today_start
                ]
                db.upsert_wallet_pnl_points(wallet, interval, fidelity, completed_points)
                fetched = True
            points = [
                PnlPoint(timestamp=int(row["timestamp"]), pnl=float(row["pnl"]))
                for row in db.wallet_pnl_points(wallet, interval, fidelity, before_ts=today_start)
            ]
            candles = daily_candles(points)
            latest = candles[-1] if candles else None
            performance.append(
                {
                    "wallet": wallet,
                    "points": [{"t": point.timestamp, "p": point.pnl} for point in points],
                    "candles": candles,
                    "latest_pnl": latest["close"] if latest else None,
                    "latest_daily_pnl": latest["daily_pnl"] if latest else None,
                    "fetched": fetched,
                    "current_day_excluded": True,
                    "error": None,
                }
            )
        except Exception as exc:
            performance.append(
                {
                    "wallet": wallet,
                    "points": [],
                    "candles": [],
                    "latest_pnl": None,
                    "latest_daily_pnl": None,
                    "fetched": False,
                    "current_day_excluded": True,
                    "error": str(exc),
                }
            )
    return performance


def _utc_day_start(dt: datetime) -> int:
    day = dt.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(day.timestamp())


def _position_rows(db: Database, clob_client: PublicClobClient) -> tuple[list[dict[str, Any]], dict[str, float]]:
    rows: list[dict[str, Any]] = []
    total_cost = 0.0
    total_value = 0.0
    total_unrealized = 0.0
    total_realized = 0.0
    pending_valuation_cost = 0.0

    for row in db.position_dashboard_rows():
        quote = _safe_quote(clob_client, row["asset_id"]) if row["status"] == "open" else {}
        bid = quote.get("best_bid")
        display_status = row["status"]
        if (
            row["status"] == "open"
            and _market_has_ended(row["market_end_time"])
            and row["resolution_resolved"] != 1
        ):
            display_status = "awaiting_resolution"

        if row["status"] == "redeem_required":
            est_value = row["total_shares"]
            unrealized = est_value - row["total_cost"]
        elif row["status"] == "open" and bid is not None:
            est_value = row["total_shares"] * bid
            unrealized = est_value - row["total_cost"]
        else:
            est_value = None if row["status"] == "open" else 0.0
            unrealized = None if row["status"] == "open" else 0.0
        realized = float(row["realized_pnl"] or 0)
        total = unrealized + realized if unrealized is not None else None
        total_cost += float(row["total_cost"])
        if est_value is not None:
            total_value += est_value
        if unrealized is not None:
            total_unrealized += unrealized
        elif row["status"] == "open":
            pending_valuation_cost += float(row["total_cost"])
        total_realized += realized

        rows.append(
            {
                "market_id": row["market_id"],
                "asset_id": row["asset_id"],
                "outcome": row["outcome"],
                "shares": row["total_shares"],
                "avg_entry_price": row["avg_entry_price"],
                "cost": row["total_cost"],
                "bid": bid,
                "ask": quote.get("best_ask"),
                "est_value": est_value,
                "unrealized": unrealized,
                "realized": realized,
                "total": total,
                "status": display_status,
                "position_created_at": row["position_created_at"],
                "market_end_time": row["market_end_time"],
                "resolution_checked_at": row["resolution_checked_at"],
                "market_title": row["market_title"] or row["market_id"],
                "market_url": _polymarket_market_url(row["event_slug"]),
            }
        )

    return rows, {
        "cost": total_cost,
        "est_value": total_value,
        "unrealized": total_unrealized,
        "realized": total_realized,
        "total": total_unrealized + total_realized,
        "pending_valuation_cost": pending_valuation_cost,
    }


def _market_has_ended(value: Any) -> bool:
    if not value:
        return False
    try:
        end_time = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    if end_time.tzinfo is None:
        end_time = end_time.replace(tzinfo=timezone.utc)
    return end_time <= utc_now()


def _copied_order_rows(db: Database, clob_client: PublicClobClient) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in db.copied_order_rows(40):
        data = _row_dict(row)
        our_price = _order_entry_price(data)
        reference_price = data.get("reference_price")
        token_id = data.get("token_id")
        quote = _safe_quote(clob_client, token_id) if token_id else {}
        bid = quote.get("best_bid")
        diff = our_price - reference_price if our_price is not None and reference_price is not None else None
        mtm_pnl = None
        if bid is not None and data.get("source_side") == TradeSide.BUY.value and our_price is not None:
            shares = data.get("filled_shares") or data.get("requested_shares")
            if shares is not None:
                fee = data.get("actual_fee_usd") or data.get("estimated_fee_usd") or 0
                mtm_pnl = (bid - our_price) * shares - fee
        data.update(
            {
                "our_price": our_price,
                "entry_diff": diff,
                "bid": bid,
                "ask": quote.get("best_ask"),
                "our_mtm_pnl": mtm_pnl,
            }
        )
        rows.append(data)
    return rows


def _safe_quote(clob_client: PublicClobClient, token_id: str) -> dict[str, float | None]:
    try:
        quote = clob_client.get_quote(token_id)
        return {"best_bid": quote.best_bid, "best_ask": quote.best_ask}
    except Exception:
        return {"best_bid": None, "best_ask": None}


def _row_dict(row: Any) -> dict[str, Any]:
    data = {key: _coerce_json(row[key]) for key in row.keys()}
    if data.get("freeze_reason"):
        data["freeze_reason"] = _normalize_freeze_reason(str(data["freeze_reason"]))
    if isinstance(data.get("decision_details"), dict):
        data["decision_summary"] = _decision_detail_summary(data["decision_details"])
    return data


def _coerce_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if not value:
        return value
    if value[0] not in "[{":
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _normalize_freeze_reason(reason: str) -> str:
    prefix = "source token frozen:"
    normalized = reason.strip()
    while normalized.lower().startswith(prefix):
        normalized = normalized[len(prefix) :].strip()
    return normalized or "risk mismatch"


def _decision_detail_summary(details: dict[str, Any]) -> str:
    parts = []
    labels = (
        ("source_price", "source"),
        ("reference_price", "reference"),
        ("executable_price", "executable"),
        ("slippage_cents", "slippage"),
        ("allowed_slippage_price", "allowed"),
        ("available_balance_usd", "balance"),
        ("market_end_time", "market end"),
        ("seconds_until_market_end", "seconds left"),
    )
    for key, label in labels:
        value = details.get(key)
        if value is not None:
            parts.append(f"{label}: {value}")
    return "; ".join(parts)


def _order_entry_price(row: dict[str, Any]) -> float | None:
    if row.get("avg_fill_price") is not None:
        return float(row["avg_fill_price"])
    requested_shares = row.get("requested_shares")
    requested_notional = row.get("requested_notional_usd")
    if requested_shares and requested_shares > 0 and requested_notional is not None:
        return float(requested_notional) / float(requested_shares)
    return row.get("limit_price")


HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Polymarket Copy Trader</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0d1117;
      --panel: #151b23;
      --panel-soft: #10161d;
      --text: #e6edf3;
      --muted: #8b949e;
      --line: #30363d;
      --green: #3fb950;
      --red: #f85149;
      --yellow: #d29922;
      --blue: #58a6ff;
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 14px;
    }

    header {
      position: sticky;
      top: 0;
      z-index: 3;
      border-bottom: 1px solid var(--line);
      background: rgba(13, 17, 23, 0.96);
      backdrop-filter: blur(10px);
    }

    .bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      max-width: 1480px;
      margin: 0 auto;
      padding: 16px 20px;
    }

    h1 {
      margin: 0;
      font-size: 18px;
      font-weight: 700;
      letter-spacing: 0;
    }

    .status {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 10px;
      color: var(--muted);
      font-size: 12px;
    }

    .pill {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 4px 8px;
      background: var(--panel-soft);
      color: var(--muted);
      white-space: nowrap;
    }

    .pill.good { color: var(--green); border-color: rgba(63, 185, 80, 0.45); }
    .pill.warn { color: var(--yellow); border-color: rgba(210, 153, 34, 0.5); }
    .pill.bad { color: var(--red); border-color: rgba(248, 81, 73, 0.5); }

    main {
      max-width: 1480px;
      margin: 0 auto;
      padding: 20px;
    }

    .metrics {
      display: grid;
      grid-template-columns: repeat(6, minmax(120px, 1fr));
      gap: 10px;
      margin-bottom: 16px;
    }

    .metric {
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 12px;
    }

    .metric label {
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 8px;
    }

    .metric strong {
      display: block;
      font-size: 20px;
      font-weight: 700;
      overflow-wrap: anywhere;
    }

    .pos { color: var(--green); }
    .neg { color: var(--red); }
    .muted { color: var(--muted); }

    .tabs {
      display: flex;
      gap: 8px;
      border-bottom: 1px solid var(--line);
      margin-bottom: 14px;
      overflow-x: auto;
    }

    button.tab {
      appearance: none;
      border: 0;
      border-bottom: 2px solid transparent;
      background: transparent;
      color: var(--muted);
      padding: 10px 4px;
      margin-right: 14px;
      cursor: pointer;
      font: inherit;
      white-space: nowrap;
    }

    button.tab.active {
      color: var(--text);
      border-bottom-color: var(--blue);
    }

    section.view { display: none; }
    section.view.active { display: block; }

    .table-wrap {
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: auto;
      background: var(--panel);
    }

    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 920px;
    }

    th, td {
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }

    th {
      position: sticky;
      top: 0;
      z-index: 2;
      background: var(--panel);
      color: var(--muted);
      font-size: 12px;
      font-weight: 600;
    }

    tr:last-child td { border-bottom: 0; }
    td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
    .market { max-width: 440px; min-width: 260px; }
    td.compact-id {
      position: relative;
      max-width: 120px;
      width: 120px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
      font-size: 12px;
    }

    td.compact-id:hover {
      overflow: visible;
      z-index: 8;
    }

    td.compact-id:hover::after {
      content: attr(data-full);
      position: absolute;
      left: 8px;
      top: calc(100% - 4px);
      max-width: min(720px, 70vw);
      padding: 8px 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #0f141b;
      color: var(--text);
      box-shadow: 0 10px 28px rgba(0, 0, 0, 0.35);
      white-space: normal;
      overflow-wrap: anywhere;
      line-height: 1.4;
    }
    .empty { padding: 24px; color: var(--muted); }

    .settings {
      display: grid;
      grid-template-columns: repeat(3, minmax(220px, 1fr));
      gap: 10px;
    }

    .kv {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 12px;
      background: var(--panel);
    }

    .kv span { display: block; color: var(--muted); font-size: 12px; margin-bottom: 4px; }
    .kv code { color: var(--text); overflow-wrap: anywhere; }

    .performance-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
      margin-bottom: 12px;
    }

    .wallet-switcher {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }

    button.wallet-chip {
      appearance: none;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--panel-soft);
      color: var(--muted);
      padding: 6px 10px;
      cursor: pointer;
      font: inherit;
      font-size: 12px;
      max-width: 220px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    button.wallet-chip.active {
      color: var(--text);
      border-color: var(--blue);
    }

    .chart-wrap {
      position: relative;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 12px;
      margin-bottom: 14px;
      overflow-x: auto;
    }

    .chart-canvas {
      display: block;
      height: 520px;
    }

    .chart-tooltip {
      position: absolute;
      display: none;
      pointer-events: none;
      min-width: 190px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #0f141b;
      color: var(--text);
      padding: 8px 10px;
      box-shadow: 0 10px 28px rgba(0, 0, 0, 0.35);
      font-size: 12px;
      line-height: 1.45;
      z-index: 9;
    }

    .chart-note {
      color: var(--muted);
      font-size: 12px;
    }

    .table-controls {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
      margin-bottom: 10px;
    }

    .filter-group, .sort-group {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }

    .control {
      display: flex;
      align-items: center;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
    }

    .control input, .control select {
      min-width: 120px;
      max-width: 220px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-soft);
      color: var(--text);
      padding: 6px 8px;
      font: inherit;
      font-size: 12px;
    }

    button.sort-dir {
      width: 34px;
      height: 32px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-soft);
      color: var(--text);
      cursor: pointer;
    }

    @media (max-width: 980px) {
      .bar { align-items: flex-start; flex-direction: column; }
      .status { justify-content: flex-start; }
      .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .settings { grid-template-columns: 1fr; }
      main { padding: 14px; }
    }
  </style>
</head>
<body>
  <header>
    <div class="bar">
      <h1>Polymarket Copy Trader</h1>
      <div class="status">
        <span id="mode" class="pill">mode</span>
        <span id="stop" class="pill">STOP_TRADING</span>
        <span id="updated" class="pill">loading</span>
      </div>
    </div>
  </header>

  <main>
    <div class="metrics" id="metrics"></div>

    <nav class="tabs" aria-label="Dashboard views">
      <button class="tab active" data-view="positions">Positions</button>
      <button class="tab" data-view="our-performance">Our Performance</button>
      <button class="tab" data-view="performance">Source Performance</button>
      <button class="tab" data-view="orders">Copied Orders</button>
      <button class="tab" data-view="redemptions">Settlements</button>
      <button class="tab" data-view="trades">Recent Source Trades</button>
      <button class="tab" data-view="states">Source States</button>
      <button class="tab" data-view="errors">Errors</button>
      <button class="tab" data-view="settings">Settings</button>
    </nav>

    <section id="positions" class="view active"></section>
    <section id="our-performance" class="view"></section>
    <section id="performance" class="view"></section>
    <section id="orders" class="view"></section>
    <section id="redemptions" class="view"></section>
    <section id="trades" class="view"></section>
    <section id="states" class="view"></section>
    <section id="errors" class="view"></section>
    <section id="settings" class="view"></section>
  </main>

  <script>
    const state = {
      data: null,
      lastError: null,
      performanceWallet: null,
      performanceHitboxes: [],
      filters: {},
      sorts: {},
    };

    const tableConfigs = {
      positions: {
        filters: [
          { key: "status", label: "status", type: "select" },
          { key: "market_title", label: "market", type: "text" },
        ],
        sorters: [
          { key: "position_created_at", label: "created", type: "date" },
          { key: "shares", label: "shares", type: "number" },
          { key: "avg_entry_price", label: "avg", type: "number" },
          { key: "cost", label: "cost", type: "number" },
          { key: "est_value", label: "est value", type: "number" },
          { key: "unrealized", label: "unrealized", type: "number" },
          { key: "realized", label: "realized", type: "number" },
          { key: "total", label: "total", type: "number" },
        ],
      },
      orders: {
        filters: [
          { key: "copied_order_status", label: "status", type: "select" },
          { key: "source_side", label: "side", type: "select" },
          { key: "source_outcome", label: "source outcome", type: "select" },
          { key: "copied_outcome", label: "copied outcome", type: "select" },
          { key: "market_title", label: "market", type: "text" },
        ],
        sorters: [
          { key: "copied_at", label: "copied at", type: "date" },
          { key: "source_price", label: "source px", type: "number" },
          { key: "reference_price", label: "reference px", type: "number" },
          { key: "our_price", label: "our px", type: "number" },
          { key: "entry_diff", label: "diff", type: "number" },
          { key: "source_notional_usd", label: "source $", type: "number" },
          { key: "requested_notional_usd", label: "our $", type: "number" },
          { key: "requested_shares", label: "our shares", type: "number" },
        ],
      },
      redemptions: {
        filters: [
          { key: "status", label: "status", type: "select" },
          { key: "market_title", label: "market", type: "text" },
        ],
        sorters: [
          { key: "created_at", label: "created", type: "date" },
          { key: "shares", label: "shares", type: "number" },
          { key: "payout_usd", label: "payout", type: "number" },
          { key: "realized_pnl", label: "realized", type: "number" },
          { key: "source_payout_usd", label: "source payout", type: "number" },
        ],
      },
      trades: {
        filters: [
          { key: "wallet_name", label: "profile", type: "select" },
          { key: "market_title", label: "market", type: "text" },
          { key: "side", label: "side", type: "select" },
        ],
        sorters: [
          { key: "timestamp", label: "time", type: "date" },
          { key: "outcome", label: "outcome", type: "text" },
          { key: "price", label: "price", type: "number" },
          { key: "size", label: "shares", type: "number" },
          { key: "notional_usd", label: "notional", type: "number" },
        ],
      },
      states: {
        filters: [
          { key: "wallet_name", label: "profile", type: "select" },
          { key: "market_label", label: "market", type: "text" },
          { key: "freeze_reason", label: "reason", type: "text" },
          { key: "decision_summary", label: "details", type: "text" },
          { key: "status", label: "status", type: "select" },
        ],
        sorters: [
          { key: "baseline_source_shares", label: "baseline", type: "number" },
          { key: "observed_source_shares", label: "observed", type: "number" },
        ],
      },
    };

    const fmt = {
      money(value) {
        return value === null || value === undefined || Number.isNaN(Number(value))
          ? ""
          : Number(value).toFixed(2);
      },
      price(value) {
        return value === null || value === undefined || Number.isNaN(Number(value))
          ? ""
          : Number(value).toFixed(4);
      },
      shares(value) {
        return value === null || value === undefined || Number.isNaN(Number(value))
          ? ""
          : Number(value).toFixed(4);
      },
      text(value) {
        return String(value ?? "")
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;")
          .replaceAll('"', "&quot;")
          .replaceAll("'", "&#039;");
      },
      time(value) {
        if (!value) return "";
        const date = parseDashboardTime(value);
        if (!date || Number.isNaN(date.getTime())) return "";
        const timeZone = state.data?.settings?.trading_day_timezone || "UTC";
        const parts = new Intl.DateTimeFormat("en-GB", {
          timeZone,
          year: "numeric",
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
          hour12: false,
        }).formatToParts(date).reduce((acc, part) => {
          acc[part.type] = part.value;
          return acc;
        }, {});
        return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
      },
    };

    function parseDashboardTime(value) {
      if (!value) return null;
      if (value instanceof Date) return value;
      const text = String(value).trim();
      if (/^\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2}$/.test(text)) {
        return new Date(`${text.replace(" ", "T")}Z`);
      }
      return new Date(text);
    }

    function pnlClass(value) {
      if (value === null || value === undefined) return "";
      return Number(value) >= 0 ? "pos" : "neg";
    }

    function priceDiffClass(row) {
      const value = row.entry_diff;
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "";
      if (Number(value) === 0) return "";
      if (row.source_side === "SELL") {
        return Number(value) > 0 ? "pos" : "neg";
      }
      return Number(value) < 0 ? "pos" : "neg";
    }

    function cell(value, cls = "") {
      return `<td class="${cls}">${fmt.text(value)}</td>`;
    }

    function compactCell(value, fullValue = value) {
      const text = fmt.text(value);
      const full = fmt.text(fullValue);
      return `<td class="compact-id" data-full="${full}" title="${full}">${text}</td>`;
    }

    function num(value, formatter = fmt.money, cls = "") {
      return `<td class="num ${cls}">${formatter(value)}</td>`;
    }

    function table(headers, rows, emptyText) {
      if (!rows.length) return `<div class="table-wrap"><div class="empty">${fmt.text(emptyText)}</div></div>`;
      const head = headers.map(h => `<th class="${h.cls || ""}">${fmt.text(h.label)}</th>`).join("");
      return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
    }

    function ensureControls(view) {
      if (!state.filters[view]) state.filters[view] = {};
      if (!state.sorts[view]) state.sorts[view] = { key: "", dir: "desc" };
    }

    function rowValue(row, key) {
      const value = row[key];
      if (value === null || value === undefined) return "";
      return value;
    }

    function uniqueOptions(rows, key) {
      return [...new Set(rows.map(row => String(rowValue(row, key))).filter(Boolean))]
        .sort((a, b) => a.localeCompare(b));
    }

    function tableControls(view, rows) {
      const config = tableConfigs[view];
      if (!config) return "";
      ensureControls(view);
      const filters = config.filters.map(filter => {
        const value = state.filters[view][filter.key] || "";
        if (filter.type === "select") {
          const options = uniqueOptions(rows, filter.key)
            .map(option => `<option value="${fmt.text(option)}" ${option === value ? "selected" : ""}>${fmt.text(option)}</option>`)
            .join("");
          return `
            <label class="control">${fmt.text(filter.label)}
              <select data-control-view="${view}" data-filter-key="${fmt.text(filter.key)}">
                <option value="">all</option>${options}
              </select>
            </label>
          `;
        }
        return `
          <label class="control">${fmt.text(filter.label)}
            <input type="search" value="${fmt.text(value)}" placeholder="contains..." data-control-view="${view}" data-filter-key="${fmt.text(filter.key)}">
          </label>
        `;
      }).join("");
      const sort = state.sorts[view];
      const sorterOptions = config.sorters
        .map(sorter => `<option value="${fmt.text(sorter.key)}" ${sort.key === sorter.key ? "selected" : ""}>${fmt.text(sorter.label)}</option>`)
        .join("");
      return `
        <div class="table-controls">
          <div class="filter-group">${filters}</div>
          <div class="sort-group">
            <label class="control">sort
              <select data-control-view="${view}" data-sort-select="true">
                <option value="">default</option>${sorterOptions}
              </select>
            </label>
            <button class="sort-dir" data-control-view="${view}" data-sort-dir="true" title="Toggle sort direction">${sort.dir === "asc" ? "↑" : "↓"}</button>
          </div>
        </div>
      `;
    }

    function filteredAndSorted(view, rows) {
      const config = tableConfigs[view];
      if (!config) return rows;
      ensureControls(view);
      const filters = state.filters[view];
      let visible = rows.filter(row => config.filters.every(filter => {
        const needle = String(filters[filter.key] || "").trim().toLowerCase();
        if (!needle) return true;
        const haystack = String(rowValue(row, filter.key)).toLowerCase();
        return filter.type === "select" ? haystack === needle : haystack.includes(needle);
      }));
      const sort = state.sorts[view];
      const sorter = config.sorters.find(item => item.key === sort.key);
      if (!sorter) return visible;
      const direction = sort.dir === "asc" ? 1 : -1;
      return [...visible].sort((a, b) => {
        let av = rowValue(a, sorter.key);
        let bv = rowValue(b, sorter.key);
        if (sorter.type === "number") {
          av = av === "" ? Number.NEGATIVE_INFINITY : Number(av);
          bv = bv === "" ? Number.NEGATIVE_INFINITY : Number(bv);
        } else if (sorter.type === "date") {
          av = av ? parseDashboardTime(av).getTime() : 0;
          bv = bv ? parseDashboardTime(bv).getTime() : 0;
        } else {
          av = String(av).toLowerCase();
          bv = String(bv).toLowerCase();
        }
        if (av < bv) return -1 * direction;
        if (av > bv) return 1 * direction;
        return 0;
      });
    }

    function attachControlHandlers(view) {
      document.querySelectorAll(`[data-control-view="${view}"][data-filter-key]`).forEach(control => {
        const applyFilter = () => {
          ensureControls(view);
          state.filters[view][control.dataset.filterKey] = control.value;
          rerenderView(view);
        };
        control.addEventListener("change", applyFilter);
        control.addEventListener("keydown", event => {
          if (event.key === "Enter") applyFilter();
        });
      });
      document.querySelectorAll(`[data-control-view="${view}"][data-sort-select]`).forEach(control => {
        control.addEventListener("change", () => {
          ensureControls(view);
          state.sorts[view].key = control.value;
          rerenderView(view);
        });
      });
      document.querySelectorAll(`[data-control-view="${view}"][data-sort-dir]`).forEach(control => {
        control.addEventListener("click", () => {
          ensureControls(view);
          state.sorts[view].dir = state.sorts[view].dir === "asc" ? "desc" : "asc";
          rerenderView(view);
        });
      });
    }

    function rerenderView(view) {
      if (!state.data) return;
      if (view === "positions") renderPositions(state.data);
      if (view === "orders") renderOrders(state.data);
      if (view === "redemptions") renderRedemptions(state.data);
      if (view === "trades") renderTrades(state.data);
      if (view === "states") renderStates(state.data);
    }

    function renderMetrics(data) {
      const totals = data.totals;
      const items = [
        ["Cost", totals.cost],
        ["Est value", totals.est_value],
        ["Unrealized", totals.unrealized, pnlClass(totals.unrealized)],
        ["Realized", totals.realized, pnlClass(totals.realized)],
        ["Total MTM", totals.total, pnlClass(totals.total)],
        ["Spend today", totals.spend_today],
      ];
      if (totals.available_cash !== null && totals.available_cash !== undefined) {
        items.push(["Available cash", totals.available_cash]);
      }
      if (totals.pending_valuation_cost > 0) {
        items.push(["Awaiting valuation", totals.pending_valuation_cost]);
      }
      document.getElementById("metrics").innerHTML = items.map(([label, value, cls]) => `
        <div class="metric">
          <label>${fmt.text(label)}</label>
          <strong class="${cls || ""}">${fmt.money(value)}</strong>
        </div>
      `).join("");
    }

    function renderPositions(data) {
      const sourceRows = filteredAndSorted("positions", data.positions);
      const rows = sourceRows.map(row => `<tr>
        ${cell(fmt.time(row.position_created_at))}
        ${cell(row.outcome)}
        ${num(row.shares, fmt.shares)}
        ${num(row.avg_entry_price, fmt.price)}
        ${num(row.cost)}
        ${num(row.bid, fmt.price)}
        ${num(row.ask, fmt.price)}
        ${num(row.est_value)}
        ${num(row.unrealized, fmt.money, pnlClass(row.unrealized))}
        ${num(row.realized, fmt.money, pnlClass(row.realized))}
        ${num(row.total, fmt.money, pnlClass(row.total))}
        ${cell(row.status)}
        <td class="market">${row.market_url
          ? `<a href="${fmt.text(row.market_url)}" target="_blank" rel="noopener noreferrer">${fmt.text(row.market_title)}</a>`
          : fmt.text(row.market_title)}</td>
      </tr>`);
      document.getElementById("positions").innerHTML = tableControls("positions", data.positions) + table(
        [
          {label: "created"}, {label: "outcome"}, {label: "shares", cls: "num"}, {label: "avg", cls: "num"},
          {label: "cost", cls: "num"}, {label: "bid", cls: "num"}, {label: "ask", cls: "num"},
          {label: "est value", cls: "num"}, {label: "unrealized", cls: "num"},
          {label: "realized", cls: "num"}, {label: "total", cls: "num"}, {label: "status"}, {label: "market"}
        ],
        rows,
        "No copied positions yet."
      );
      attachControlHandlers("positions");
    }

    function renderOurPerformance(data) {
      const rows = (data.daily_performance || []).map(row => `<tr>
        ${cell(row.date)}
        ${num(row.buy_count, value => value)}
        ${num(row.buy_spend)}
        ${num(row.sell_count, value => value)}
        ${num(row.sell_notional)}
        ${num(row.settlement_count, value => value)}
        ${num(row.settlement_payout)}
        ${num(row.settlement_pnl, fmt.money, pnlClass(row.settlement_pnl))}
        ${num(row.fees)}
        ${num(row.net_cashflow, fmt.money, pnlClass(row.net_cashflow))}
      </tr>`);
      document.getElementById("our-performance").innerHTML = `
        <div class="chart-note" style="margin-bottom: 10px;">
          Local bot accounting by ${fmt.text(data.settings.trading_day_timezone)} trading day. Dry-run rows are simulated; live rows are what the bot recorded locally.
        </div>
        ${table(
          [
            {label: "date"}, {label: "buys", cls: "num"}, {label: "buy spend", cls: "num"},
            {label: "sells", cls: "num"}, {label: "sell notional", cls: "num"},
            {label: "settlements", cls: "num"}, {label: "settlement payout", cls: "num"},
            {label: "settlement pnl", cls: "num"}, {label: "fees", cls: "num"},
            {label: "cashflow", cls: "num"}
          ],
          rows,
          "No local performance rows yet."
        )}
      `;
    }

    function shortWallet(wallet) {
      if (!wallet) return "";
      return wallet.length > 14 ? `${wallet.slice(0, 8)}...${wallet.slice(-6)}` : wallet;
    }

    function renderPerformance(data) {
      const entries = data.source_performance || [];
      if (!entries.length) {
        document.getElementById("performance").innerHTML = `<div class="table-wrap"><div class="empty">No TARGET_WALLETS configured.</div></div>`;
        return;
      }
      if (!state.performanceWallet || !entries.some(entry => entry.wallet === state.performanceWallet)) {
        state.performanceWallet = entries[0].wallet;
      }
      const active = entries.find(entry => entry.wallet === state.performanceWallet) || entries[0];
      const chips = entries.map(entry => `
        <button class="wallet-chip ${entry.wallet === active.wallet ? "active" : ""}" data-wallet="${fmt.text(entry.wallet)}" title="${fmt.text(entry.wallet)}">
          ${fmt.text(entry.wallet_name || shortWallet(entry.wallet))}
        </button>
      `).join("");
      const latest = active.latest_pnl === null || active.latest_pnl === undefined ? "" : fmt.money(active.latest_pnl);
      const latestDaily = active.latest_daily_pnl === null || active.latest_daily_pnl === undefined ? "" : fmt.money(active.latest_daily_pnl);
      const error = active.error ? `<div class="empty">${fmt.text(active.error)}</div>` : "";
      const cacheText = active.fetched ? "updated local cache" : "served from local DB";
      const rows = [...(active.candles || [])].reverse().slice(0, 60).map(row => `<tr>
        ${cell(row.date)}
        ${num(row.open)}
        ${num(row.high)}
        ${num(row.low)}
        ${num(row.close)}
        ${num(row.daily_pnl, fmt.money, pnlClass(row.daily_pnl))}
      </tr>`);
      document.getElementById("performance").innerHTML = `
        <div class="performance-head">
          <div class="wallet-switcher">${chips}</div>
          <div class="chart-note">Latest completed day PnL: <span class="${pnlClass(active.latest_pnl)}">${latest}</span> | Latest daily: <span class="${pnlClass(active.latest_daily_pnl)}">${latestDaily}</span> | ${fmt.text(cacheText)} | today excluded</div>
        </div>
        <div class="chart-wrap" id="performance-chart-wrap">
          <canvas class="chart-canvas" id="performance-chart"></canvas>
          <div class="chart-tooltip" id="performance-tooltip"></div>
        </div>
        ${error}
        ${table(
          [
            {label: "date"}, {label: "open", cls: "num"}, {label: "high", cls: "num"},
            {label: "low", cls: "num"}, {label: "close", cls: "num"}, {label: "daily PnL", cls: "num"}
          ],
          rows,
          "No PnL history available."
        )}
      `;
      document.querySelectorAll("button.wallet-chip").forEach(button => {
        button.addEventListener("click", () => {
          state.performanceWallet = button.dataset.wallet;
          renderPerformance(state.data);
        });
      });
      drawPerformanceChart(active);
    }

    function drawPerformanceChart(entry) {
      const canvas = document.getElementById("performance-chart");
      const wrap = document.getElementById("performance-chart-wrap");
      const tooltip = document.getElementById("performance-tooltip");
      if (!canvas || !wrap || !tooltip) return;
      const candles = entry.candles || [];
      const width = Math.max(wrap.clientWidth - 24, candles.length * 9, 900);
      const height = 520;
      const ratio = window.devicePixelRatio || 1;
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      canvas.width = Math.floor(width * ratio);
      canvas.height = Math.floor(height * ratio);
      const ctx = canvas.getContext("2d");
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      ctx.clearRect(0, 0, width, height);
      state.performanceHitboxes = [];

      if (!candles.length) {
        ctx.fillStyle = "#8b949e";
        ctx.fillText("No PnL history available.", 24, 42);
        return;
      }

      const pad = { left: 66, right: 24, top: 24, bottom: 96 };
      const chartH = height - pad.top - pad.bottom;
      const barTop = height - 72;
      const barH = 44;
      const values = candles.flatMap(c => [c.open, c.high, c.low, c.close]);
      const daily = candles.map(c => c.daily_pnl);
      const minY = Math.min(...values, 0);
      const maxY = Math.max(...values, 0);
      const span = Math.max(1, maxY - minY);
      const y = value => pad.top + ((maxY - value) / span) * chartH;
      const plotW = width - pad.left - pad.right;
      const step = Math.max(5, plotW / candles.length);
      const candleW = Math.max(3, Math.min(9, step * 0.62));
      const maxAbsDaily = Math.max(1, ...daily.map(v => Math.abs(v)));

      ctx.strokeStyle = "#30363d";
      ctx.lineWidth = 1;
      ctx.fillStyle = "#8b949e";
      ctx.font = "12px ui-sans-serif, system-ui";
      for (let i = 0; i <= 4; i++) {
        const value = minY + (span * i / 4);
        const yy = y(value);
        ctx.beginPath();
        ctx.moveTo(pad.left, yy);
        ctx.lineTo(width - pad.right, yy);
        ctx.stroke();
        ctx.fillText(fmt.money(value), 10, yy + 4);
      }

      candles.forEach((c, i) => {
        const x = pad.left + i * step + step / 2;
        const up = c.close >= c.open;
        const color = up ? "#3fb950" : "#f85149";
        const highY = y(c.high);
        const lowY = y(c.low);
        const openY = y(c.open);
        const closeY = y(c.close);
        const bodyTop = Math.min(openY, closeY);
        const bodyH = Math.max(2, Math.abs(closeY - openY));

        ctx.strokeStyle = color;
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.moveTo(x, highY);
        ctx.lineTo(x, lowY);
        ctx.stroke();
        ctx.fillRect(x - candleW / 2, bodyTop, candleW, bodyH);

        const barHeight = Math.max(1, Math.abs(c.daily_pnl) / maxAbsDaily * barH);
        ctx.fillStyle = c.daily_pnl >= 0 ? "rgba(63,185,80,0.65)" : "rgba(248,81,73,0.65)";
        if (c.daily_pnl >= 0) {
          ctx.fillRect(x - candleW / 2, barTop + barH - barHeight, candleW, barHeight);
        } else {
          ctx.fillRect(x - candleW / 2, barTop, candleW, barHeight);
        }

        state.performanceHitboxes.push({ x: x - step / 2, w: step, candle: c });
      });

      ctx.fillStyle = "#8b949e";
      ctx.fillText("Cumulative PnL candles", pad.left, 18);
      ctx.fillText("Daily PnL bars", pad.left, barTop - 8);

      canvas.onmousemove = event => {
        const rect = canvas.getBoundingClientRect();
        const mouseX = event.clientX - rect.left;
        const hit = state.performanceHitboxes.find(item => mouseX >= item.x && mouseX <= item.x + item.w);
        if (!hit) {
          tooltip.style.display = "none";
          return;
        }
        const c = hit.candle;
        tooltip.innerHTML = `
          <strong>${fmt.text(c.date)}</strong><br>
          Open: ${fmt.money(c.open)}<br>
          High: ${fmt.money(c.high)}<br>
          Low: ${fmt.money(c.low)}<br>
          Close: ${fmt.money(c.close)}<br>
          Daily PnL: <span class="${pnlClass(c.daily_pnl)}">${fmt.money(c.daily_pnl)}</span>
        `;
        tooltip.style.display = "block";
        tooltip.style.left = `${Math.min(mouseX + 18, wrap.scrollLeft + wrap.clientWidth - 230)}px`;
        tooltip.style.top = `${Math.max(12, event.clientY - rect.top - 20)}px`;
      };
      canvas.onmouseleave = () => { tooltip.style.display = "none"; };
    }

    function renderOrders(data) {
      const sourceRows = filteredAndSorted("orders", data.copied_orders);
      const rows = sourceRows.map(row => `<tr>
        ${cell(fmt.time(row.copied_at))}
        ${cell(row.copied_order_status)}
        ${cell(row.source_side)}
        ${cell(row.source_outcome)}
        ${cell(row.copied_outcome)}
        ${num(row.source_price, fmt.price)}
        ${num(row.reference_price, fmt.price)}
        ${num(row.our_price, fmt.price)}
        ${num(row.entry_diff, fmt.price, priceDiffClass(row))}
        ${num(row.source_notional_usd)}
        ${num(row.requested_notional_usd)}
        ${num(row.requested_shares, fmt.shares)}
        ${num((row.actual_fee_usd || row.estimated_fee_usd || 0))}
        ${num(row.bid, fmt.price)}
        ${num(row.our_mtm_pnl, fmt.money, pnlClass(row.our_mtm_pnl))}
        <td class="market">${fmt.text(row.market_title)}</td>
      </tr>`);
      document.getElementById("orders").innerHTML = tableControls("orders", data.copied_orders) + table(
        [
          {label: "copied at"}, {label: "status"}, {label: "side"},
          {label: "source outcome"}, {label: "copied outcome"},
          {label: "source px", cls: "num"}, {label: "reference px", cls: "num"},
          {label: "our px", cls: "num"},
          {label: "diff", cls: "num"}, {label: "source $", cls: "num"},
          {label: "our $", cls: "num"}, {label: "our shares", cls: "num"},
          {label: "fee", cls: "num"}, {label: "bid", cls: "num"},
          {label: "our mtm pnl", cls: "num"}, {label: "market"}
        ],
        rows,
        "No copied orders yet."
      );
      attachControlHandlers("orders");
    }

    function renderRedemptions(data) {
      const sourceRows = filteredAndSorted("redemptions", data.copied_redemptions);
      const rows = sourceRows.map(row => `<tr>
        ${cell(fmt.time(row.created_at))}
        ${cell(row.status)}
        ${num(row.shares, fmt.shares)}
        ${num(row.payout_usd)}
        ${num(row.realized_pnl, fmt.money, pnlClass(row.realized_pnl))}
        ${num(row.source_payout_usd)}
        <td class="market">${row.market_url
          ? `<a href="${fmt.text(row.market_url)}" target="_blank" rel="noopener noreferrer">${fmt.text(row.market_title)}</a>`
          : fmt.text(row.market_title)}</td>
        ${cell(row.error_message)}
      </tr>`);
      document.getElementById("redemptions").innerHTML = tableControls("redemptions", data.copied_redemptions) + table(
        [
          {label: "created"}, {label: "status"}, {label: "shares", cls: "num"},
          {label: "payout", cls: "num"}, {label: "realized", cls: "num"},
          {label: "source payout", cls: "num"}, {label: "market"}, {label: "error"}
        ],
        rows,
        "No copied settlements yet."
      );
      attachControlHandlers("redemptions");
    }

    function renderTrades(data) {
      const sourceRows = filteredAndSorted("trades", data.recent_trades);
      const rows = sourceRows.map(row => `<tr>
        ${cell(fmt.time(row.timestamp))}
        ${compactCell(row.wallet_name, row.source_wallet)}
        ${cell(row.side)}
        ${cell(row.outcome)}
        ${num(row.price, fmt.price)}
        ${num(row.size, fmt.shares)}
        ${num(row.notional_usd)}
        <td class="market">${fmt.text(row.market_title)}</td>
      </tr>`);
      document.getElementById("trades").innerHTML = tableControls("trades", data.recent_trades) + table(
        [
          {label: "time"}, {label: "profile"}, {label: "side"}, {label: "outcome"},
          {label: "price", cls: "num"}, {label: "shares", cls: "num"},
          {label: "notional", cls: "num"}, {label: "market"}
        ],
        rows,
        "No source trades stored yet."
      );
      attachControlHandlers("trades");
    }

    function renderStates(data) {
      const sourceRows = filteredAndSorted("states", data.source_states);
      const rows = sourceRows.map(row => `<tr>
        ${compactCell(row.wallet_name, row.source_wallet)}
        ${cell(row.status)}
        ${num(row.baseline_source_shares, fmt.shares)}
        ${num(row.observed_source_shares, fmt.shares)}
        ${cell(row.outcome)}
        ${compactCell(row.asset_id)}
        ${compactCell(row.market_label, row.market_label)}
        ${cell(row.freeze_reason)}
        ${cell(row.decision_summary)}
      </tr>`);
      document.getElementById("states").innerHTML = tableControls("states", data.source_states) + table(
        [
          {label: "profile"}, {label: "status"}, {label: "baseline", cls: "num"},
          {label: "observed", cls: "num"}, {label: "outcome"}, {label: "asset"},
          {label: "market"}, {label: "reason"}, {label: "decision snapshot"}
        ],
        rows,
        "No source token lifecycle state yet."
      );
      attachControlHandlers("states");
    }

    function renderErrors(data) {
      const rows = data.errors.map(row => `<tr>
        ${cell(fmt.time(row.created_at))}
        ${cell(row.context)}
        ${cell(row.error_message)}
      </tr>`);
      document.getElementById("errors").innerHTML = table(
        [{label: "created"}, {label: "context"}, {label: "message"}],
        rows,
        "No logged errors."
      );
    }

    function renderSettings(data) {
      const rows = Object.entries(data.settings)
        .concat([
          ["mode", data.mode],
          ["target_wallets", data.target_wallets.join(", ") || "(none)"],
          ["stop_trading", data.stop_trading ? "true" : "false"],
        ])
        .map(([key, value]) => `<div class="kv"><span>${fmt.text(key)}</span><code>${fmt.text(value)}</code></div>`)
        .join("");
      document.getElementById("settings").innerHTML = `<div class="settings">${rows}</div>`;
    }

    function render(data) {
      state.data = data;
      document.getElementById("mode").textContent = data.mode;
      document.getElementById("mode").className = `pill ${data.mode === "live" ? "warn" : "good"}`;
      document.getElementById("stop").textContent = data.stop_trading ? "STOP_TRADING active" : "STOP_TRADING clear";
      document.getElementById("stop").className = `pill ${data.stop_trading ? "bad" : "good"}`;
      document.getElementById("updated").textContent = `updated ${fmt.time(data.generated_at)}`;
      renderMetrics(data);
      renderPositions(data);
      renderOurPerformance(data);
      renderPerformance(data);
      renderOrders(data);
      renderRedemptions(data);
      renderTrades(data);
      renderStates(data);
      renderErrors(data);
      renderSettings(data);
    }

    async function refresh() {
      try {
        const response = await fetch("/api/summary", { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        render(await response.json());
        state.lastError = null;
      } catch (error) {
        state.lastError = error;
        document.getElementById("updated").textContent = `refresh failed: ${error.message}`;
        document.getElementById("updated").className = "pill bad";
      }
    }

    document.querySelectorAll("button.tab").forEach(button => {
      button.addEventListener("click", () => {
        document.querySelectorAll("button.tab").forEach(tab => tab.classList.remove("active"));
        document.querySelectorAll("section.view").forEach(view => view.classList.remove("active"));
        button.classList.add("active");
        document.getElementById(button.dataset.view).classList.add("active");
      });
    });

    window.addEventListener("resize", () => {
      if (!state.data) return;
      const active = (state.data.source_performance || []).find(entry => entry.wallet === state.performanceWallet);
      if (active && document.getElementById("performance").classList.contains("active")) {
        drawPerformanceChart(active);
      }
    });

    refresh();
    setInterval(refresh, 30_000);
  </script>
</body>
</html>
"""
