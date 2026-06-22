# Architecture Notes

This project is dry-run first. Public Polymarket data is used for monitoring, CLOB public book data is used for executable quote checks, and authenticated trading is isolated in `execution.py`.

## Flow

1. `PollingIngestor` polls `https://data-api.polymarket.com/trades` for each configured target wallet.
2. Wallet monitoring also uses `https://data-api.polymarket.com/activity?type=TRADE` to catch UI activity sooner when available.
2. Source `REDEEM` activity is ingested separately as a resolution-check signal, not as a trade or payout authority.
3. On startup, `PollingIngestor` queries public `/positions` for each source wallet and marks currently-held tokens as `pre_existing`.
4. `normalize_trade` converts public buy/sell trade payloads into `TradeEvent`.
5. Non-trade activity types such as `Merge` are ignored by the copy loop because they are position-management actions, not orderbook trades.
6. Binary and multi-outcome trades are both represented by the specific traded `asset_id` / `token_id` plus `outcome`.
7. `Database.insert_trade` deduplicates by transaction, wallet, market/outcome, side, size, and price.
8. `source_token_states` tracks whether each source token lifecycle is clean, pre-existing, or frozen.
9. `OutcomeSelector` optionally maps a source `Up`/`Down` token to the authoritative opposite token while preserving the original source event for lifecycle tracking. The selective `inverse_down_underdog` mode ignores nonqualifying signals without freezing lifecycle state. The shadow-regime mode evaluates and records both the Up shadow trade and its Down counterfactual, then chooses the effective real path from a persisted runtime override, the calculated rolling regime, or the configured initial path.
10. `CrowdingAnalyzer` optionally checks nearby same-market/outcome trades and stores "suspected copy pressure".
11. `DecisionEngine` sizes source-mode buys by copied notional and inverse Up/Down buys by a configured fraction of source shares, then applies age, slippage, maximum buy price, market-end window, available balance, liquidity, crowding, daily spend, per-token and condition-wide exposure, accepted-entry count, source-position lifecycle, and sell-position checks.
12. `Executor` records dry-run orders or submits guarded live FAK orders through `py-clob-client`.
13. `ResolutionScanner` checks local copied positions against public Gamma market resolution data. Dry-run wins/losses are settled locally; live winners are marked `redeem_required`; live losses are realized as losses.
14. `RedemptionExecutor` handles source `REDEEM` activity by triggering `ResolutionScanner` for that condition. Only authoritative token-level winner data determines payout.
15. `UserPnlClient` reads Polymarket's public user PnL series for dashboard-only source performance charts.
16. `positions.py` keeps local copied position state and prevents blind sell copying.
17. `funds.py` derives replenishable dry-run cash from local orders/settlements and reads authenticated collateral balance in live mode.
18. `copy_decisions` stores the exact decision-time risk snapshot used by Source States.
19. `SettlementAuditor` previews or applies authoritative corrections to legacy condition-level source-redemption settlements.
20. `shadow_orders` stores one executable paper-only Up order per source wallet and market. `shadow_regime.py` deterministically replays resolved shadow outcomes to derive warm-up, active path, and pending switch confirmation across restarts.

## Live Trading Boundary

Live trading requires:

- `pct run-live --i-understand-live-trading-risk`
- `POLYMARKET_PRIVATE_KEY`
- `MAX_TRADE_USD`
- no `STOP_TRADING` file

The live executor uses marketable limit orders with FAK behavior. Market orders on Polymarket are represented as marketable limit orders, so slippage limits are enforced before submission.

`STOP_TRADING` is checked at the execution boundary. It blocks new dry-run and live BUY/SELL orders without stopping polling or resolution scanning, and it does not cancel already-submitted live orders. Observed trades are still deduplicated and blocked orders are not retried, but blocked executions do not advance source-token lifecycle state.

Copy decisions walk visible CLOB depth up to the slippage and absolute-price limit. The average price, worst price, levels, fill ratio, market metadata, fee rate, and estimated fee are stored with the decision/order snapshot. Dry-run positions use fee-inclusive cost basis. Live FAK orders receive one immediate authenticated status reconciliation when possible.

The optional market-title allowlist performs case-insensitive substring matching on BUY decisions only. SELL decisions bypass it so configuration changes cannot strand an existing copied position. The active allowlist is stored in each decision's strategy snapshot.

With `RISK_MISMATCH_SCOPE=wallet_market`, any frozen token acts as a BUY-only
market guard for that source wallet. Opposite-outcome BUYs are rejected without
freezing an already-clean copied token, and risk-reducing SELLs remain capped by
the local position.

Short-duration filtering measures `eventStartTime` to `endDate`. Gamma's `startDate` is the market creation/listing time and is used only as a fallback when no event start is available.

`target_trades.observed_at` and structured decision timing preserve polling and processing latency at sub-second precision. Every decision embeds the active strategy thresholds. `market_resolution_observations` stores authoritative token payout maps for all evaluated markets that have an advertised end time, not only markets with copied positions, so rejected decisions can be analyzed counterfactually.

Live resolution scanning does not sign redemption transactions. Winning positions become `redeem_required`; losses can be accounted immediately from authoritative resolution metadata.

Resolution state is determined from authoritative token payouts, not from a missing order book or a price near zero or one. Because the public API can lag the website after a market ends, the dashboard exposes `awaiting_resolution` and leaves bidless valuation fields unknown until a payout is available.

## Data Limits

Crowding detection is inference, not proof. It is stored and displayed as "suspected copy pressure" and should be treated as a risk signal only.
