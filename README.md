# Polymarket Copy Trader

Local Python 3.11+ project for monitoring selected Polymarket wallets, evaluating whether to copy executed trades, detecting suspected copy pressure, and optionally submitting guarded orders through the official CLOB client. Wallet monitoring combines the official public Data API `/trades` and `/activity` endpoints.

Dry-run mode is the intended starting point and is fully wired. Live trading is isolated behind explicit safety checks.

Only buy/sell trade activity is used as a copy signal. Non-trade activity such as Polymarket `Merge` is ignored. Normal buy/sell trades are tracked by outcome token, so binary and multi-outcome markets use the same internal path.

## Setup

```bash
uv sync --extra dev
cp .env.example .env
```

Edit `.env` with target wallets and limits.

New to Polymarket or copy trading? Start with [docs/guide_newbie.md](docs/guide_newbie.md).

## Run

```bash
uv run pct run-dry
uv run pct inspect-wallet --wallet 0x...
uv run pct debug-wallet --wallet 0x...
uv run pct show-recent-trades
uv run pct show-positions
uv run pct show-dashboard
uv run pct show-orders
uv run pct show-redemptions
uv run pct show-source-states
uv run pct dashboard
uv run pct show-crowding --wallet 0x...
uv run pct refresh-crowding --wallet 0x... --hours 24
uv run pct refresh-redemptions --wallet 0x...
uv run pct refresh-resolutions
uv run pct reconcile-settlements
uv run pct backfill-wallet --wallet 0x... --days 7
uv run pct reset-simulation --i-understand-this-deletes-local-simulation
```

Live trading requires the optional CLOB dependency and an explicit risk flag:

```bash
uv sync --extra live
uv run pct run-live --i-understand-live-trading-risk
```

## Configuration

Important `.env` values:

- `TARGET_WALLETS`: comma-separated wallets to monitor.
- `COPY_MODE`: `dry_run` or `live`.
- `SEED_EXISTING_TRADES_ON_STARTUP`: mark already-visible wallet history as seen on monitor startup, then process only later trades.
- `TRADING_DAY_TIMEZONE`: timezone used for `Spend today`, daily spend caps, and the dashboard's local performance tab.
- `SOURCE_POSITION_POLICY`: defaults to `skip_preexisting`; do not copy tokens the source already held before bot startup.
- `SELL_SIZING_MODE`: defaults to `source_position_ratio`; copied sells reduce local exposure proportionally to the source's observed position reduction.
- `ON_RISK_MISMATCH`: defaults to `freeze_token`; stop copying a token if a tracked source buy/sell cannot be copied.
- `RISK_MISMATCH_SCOPE`: `token` preserves the original per-outcome behavior. `wallet_market` blocks subsequent BUYs for both outcomes when any token for that source wallet and market freezes; risk-reducing SELLs remain available.
- `SOURCE_POSITION_SIZE_THRESHOLD`: minimum source position size to treat as pre-existing at startup.
- `OUTCOME_SELECTION_MODE`: `source` copies the traded outcome; `inverse_up_down` copies either authoritative opposite token; `inverse_down_underdog` copies `Up` only when the source buys cheap `Down`; `shadow_regime_down_underdog` records those executable Up trades as a shadow strategy and persistently follows or inverts them from rolling resolved performance.
- `MAX_TRADE_USD`: max copied notional per trade.
- `COPY_RATIO`: copied fraction of source notional in `source` mode.
- `INVERSE_SHARE_COPY_RATIO`: copied fraction of source shares in either inverse outcome mode.
- `INVERSE_DOWN_MAX_SOURCE_PRICE`: strict source-`Down` BUY price ceiling for `inverse_down_underdog`; the default `0.50` requires a price below `0.50`.
- `SHADOW_REGIME_WINDOW`: resolved shadow-market window used for win rate; default `50`.
- `SHADOW_REGIME_CONFIRMATION_MARKETS`: consecutive resolved shadow markets required before changing the active follow/invert path; default `10`.
- `SHADOW_REGIME_INITIAL_PATH`: real path before the first full window: `warmup` (default), `follow_shadow`, or `invert_shadow`.
- `SHADOW_REAL_TRADE_POLICY`: `auto_regime` preserves the rolling follow/invert regime; `price_filter` only executes selected high-conviction shadow signals.
- `SHADOW_FOLLOW_MIN_PRICE`: in `price_filter`, follow the shadow Up trade when its executable price is at least this value; default `0.70`.
- `SHADOW_ENABLE_INVERT_BRANCH`: in `price_filter`, keep the Down/invert branch available. Defaults to `true` for backward compatibility; set `false` to test high-Up-only trading.
- `SHADOW_INVERT_MIN_PRICE` / `SHADOW_INVERT_MAX_PRICE`: in `price_filter`, invert to Down when its executable price is in this half-open range; defaults `0.40` to `0.45`.
- `MAX_COPIED_BUYS_PER_WALLET_MARKET`: optional accepted BUY limit for each source wallet and condition.
- `MAX_SLIPPAGE_CENTS`: max worse price versus the selected outcome's reference price.
- `MAX_BUY_PRICE`: optional maximum executable price for copied buys.
- `MAX_SECONDS_UNTIL_MARKET_END`: optional maximum time until the advertised market end; buys with missing end metadata are skipped.
- `MARKET_TYPE_FILTER`: `all` or `short_duration_up_down`. The latter requires an authoritative two-token `Up`/`Down` market in the configured duration range.
- `UP_DOWN_MIN_DURATION_SECONDS` / `UP_DOWN_MAX_DURATION_SECONDS`: allowed scheduled duration for the short-duration filter.
- `MIN_NET_UPSIDE_USD` / `MIN_NET_UPSIDE_PERCENT`: optional minimum theoretical payout advantage after entry fee, consumed spread/book depth, and safety margin.
- `NET_UPSIDE_SAFETY_MARGIN_USD`: amount subtracted from theoretical upside before applying the minimum.
- `INCLUDE_EXIT_FEE_IN_UPSIDE`: reserve an estimated second fee when evaluating upside. Leave false when positions are intended to settle.
- `MIN_TRADE_USD`: ignore source trades below this.
- `MAX_TRADE_AGE_SECONDS`: ignore stale trades.
- `ALLOW_MARKET_CATEGORIES`: reserved for category filtering.
- `ALLOW_MARKET_TITLE_KEYWORDS`: optional comma-separated, case-insensitive title substrings. When configured, only matching BUYs are allowed; SELLs remain available to reduce existing positions.
- `BLOCK_MARKET_KEYWORDS`: comma-separated market title keyword blocks.
- `ENABLE_CROWDING_CHECK`: enable suspected copy pressure checks.
- `CROWDING_LOOKBACK_SECONDS`: window after target trade.
- `CROWDING_MAX_FOLLOWERS`: skip if suspected follower count exceeds this.
- `ENABLE_RESOLUTION_SCANNER`: scan local copied positions for resolved markets.
- `RESOLUTION_SCAN_INTERVAL_SECONDS`: interval for resolution scans during `run-dry`/`run-live`; 60 seconds is the default for timely short-market settlement.
- `DAILY_SPEND_CAP_USD`: local daily copy cap including estimated fees. Empty, `none`, or `unlimited` disables only this cap.
- `PER_MARKET_EXPOSURE_CAP_USD`: local per-market cap.
- `CONDITION_EXPOSURE_CAP_USD`: optional aggregate open cost cap across all outcome tokens in one condition.
- `DRY_RUN_STARTING_BALANCE_USD`: optional simulated cash balance. Dry-run buys reduce it; sells and settlements replenish it.
- `POLYMARKET_PRIVATE_KEY`: only required for live mode.
- `CHAIN_ID`: defaults to Polygon `137`.

Never hardcode private keys.

`shadow_regime_down_underdog` is experimental and dry-run only. Inspect its
current state with `uv run pct show-shadow-regime`. Use
`uv run pct set-shadow-regime auto|follow_shadow|invert_shadow` to persist a
restart-safe runtime override for future signals. Accepted shadow signals store
both the shadow fill and a contemporaneous opposite-side executable decision
for later counterfactual analysis.

`show-dashboard` and `show-orders` report mark-to-market PnL using the current best bid as the estimated exit price. Lowering `COPY_RATIO` reduces dollar exposure, but it does not improve trade quality; use slippage, age, liquidity, and wallet filters for that.

For a browser view, run this in a second terminal while `run-dry` or `run-live` is running:

```bash
uv run pct dashboard
```

Open `http://127.0.0.1:8765`. The web dashboard is read-only and refreshes from local SQLite plus public CLOB quotes; it does not place or cancel orders.

`Spend today` and the `Our Performance` tab use `TRADING_DAY_TIMEZONE`, not UTC. `Our Performance` is local bot accounting: dry-run rows are simulated, while live rows reflect orders and settlements the bot recorded locally.

The `Source Performance` tab renders every configured `TARGET_WALLETS` entry using Polymarket's public user PnL series from `USER_PNL_API_BASE_URL`. It stores completed daily PnL points in SQLite, deliberately excludes the current day, and only refetches a wallet's PnL history when yesterday's completed point is missing locally. With `fidelity=1d`, daily candle open/close and daily PnL are derived from Polymarket's daily cumulative PnL points; intraday high/low is not available from that 1-day series.

The default config is intentionally live-safe: the bot baselines source positions on startup, skips tokens the source already held, freezes a token if risk tracking breaks, and never intentionally sells more than the local copied shares.

When `DRY_RUN_STARTING_BALANCE_USD` is configured, the dashboard shows `Available cash`. This is uncommitted simulated collateral, not open-position market value. In live mode the bot instead queries the authenticated CLOB collateral balance before each copied buy and fails closed when that balance cannot be read.

`MAX_SECONDS_UNTIL_MARKET_END` uses Polymarket's advertised market end time, not the unknown final settlement time. It applies to buys only so the bot does not block risk-reducing copied sells.

## Safety

Live mode refuses to start unless:

- `--i-understand-live-trading-risk` is passed.
- `POLYMARKET_PRIVATE_KEY` is set.
- `MAX_TRADE_USD` is set.
- `COPY_RATIO <= 1`, unless `ALLOW_COPY_RATIO_GT_ONE=true`.

If a file named `STOP_TRADING` exists in the project root, new dry-run and live BUY/SELL orders are blocked while the polling loop and resolution scanner continue running. Already-submitted live orders are not cancelled. Blocked trades do not advance the tracked source-token lifecycle, but the observed source trade is deduplicated and is not retried after the file is removed.

Dry-run execution walks visible order-book levels within the slippage and maximum-price limits, records partial fills, and estimates the market fee. Simulated cash, position cost basis, realized PnL, and dashboard cashflow include those fees. Live FAK responses receive an immediate order-status lookup when an order ID is available; exchange-reported fees are preferred over estimates.

Each decision stores the exact strategy thresholds, source/observation/decision timestamps, polling delay, market metadata, visible book, simulated fills, fee calculation, and rejection reason. The resolution scanner also stores authoritative payout maps for evaluated markets after their advertised end, including markets whose trades were rejected. This supports later counterfactual analysis of filters and source wallets.

Polymarket's website can display an outcome before the public resolution API returns its authoritative payout. During that delay, ended positions remain visible as `awaiting_resolution`. Missing bids are displayed as unknown values rather than as a total loss, and the scanner retries automatically.

Resolved live winners are marked `redeem_required`. Automatic on-chain redemption is intentionally not performed because redemption burns the winning-token balance and requires a separately signed Conditional Tokens transaction.

## Tests

```bash
uv run pytest
```

## Notes

Polling via the public Data API is the reliable ingestion path. `src/websocket_ingestion.py` is intentionally experimental until a usable official free activity/trade stream is verified for your workflow.

Market resolution is tracked from your local copied positions using public Gamma market data. In dry-run mode, resolved wins and losses are settled into realized PnL. In live mode, resolved losses are recorded as realized losses; resolved wins are marked `redeem_required` so you can manually redeem in your own Polymarket wallet.

`REDEEM` activity is handled separately from trades and used only as a signal to perform an authoritative token-level resolution check. Source redemption payloads can omit the winning asset/outcome, so their payout amount is never used to settle copied positions. Source-wallet redemption does not redeem your shares.

Older databases created before this safeguard may contain condition-level source-redemption settlements that incorrectly paid losing outcome tokens. Preview corrections with:

```bash
uv run pct reconcile-settlements
```

After reviewing the output, apply verified corrections with:

```bash
uv run pct reconcile-settlements --apply --i-understand-this-updates-local-accounting
```

Stop any older bot process before applying reconciliation so it cannot add more legacy settlement rows.

See `docs/architecture.md` for the module map.

For dry-run research, public Data API trades may arrive a few minutes after their trade timestamp. If you see useful trades skipped as "trade too old", consider `MAX_TRADE_AGE_SECONDS=300` or `600`. For live trading, keep this conservative.
