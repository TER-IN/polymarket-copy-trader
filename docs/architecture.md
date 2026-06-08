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
9. `OutcomeSelector` optionally maps a source `Up`/`Down` token to the authoritative opposite token while preserving the original source event for lifecycle tracking.
10. `CrowdingAnalyzer` optionally checks nearby same-market/outcome trades and stores "suspected copy pressure".
11. `DecisionEngine` applies age, notional, slippage, maximum buy price, market-end window, available balance, liquidity, crowding, daily spend, exposure, source-position lifecycle, and sell-position checks.
12. `Executor` records dry-run orders or submits guarded live FAK orders through `py-clob-client`.
13. `ResolutionScanner` checks local copied positions against public Gamma market resolution data. Dry-run wins/losses are settled locally; live winners are marked `redeem_required`; live losses are realized as losses.
14. `RedemptionExecutor` handles source `REDEEM` activity by triggering `ResolutionScanner` for that condition. Only authoritative token-level winner data determines payout.
15. `UserPnlClient` reads Polymarket's public user PnL series for dashboard-only source performance charts.
16. `positions.py` keeps local copied position state and prevents blind sell copying.
17. `funds.py` derives replenishable dry-run cash from local orders/settlements and reads authenticated collateral balance in live mode.
18. `copy_decisions` stores the exact decision-time risk snapshot used by Source States.
19. `SettlementAuditor` previews or applies authoritative corrections to legacy condition-level source-redemption settlements.

## Live Trading Boundary

Live trading requires:

- `pct run-live --i-understand-live-trading-risk`
- `POLYMARKET_PRIVATE_KEY`
- `MAX_TRADE_USD`
- no `STOP_TRADING` file

The live executor uses marketable limit orders with FAK behavior. Market orders on Polymarket are represented as marketable limit orders, so slippage limits are enforced before submission.

`STOP_TRADING` is checked at the execution boundary. It blocks new dry-run and live BUY/SELL orders without stopping polling or resolution scanning, and it does not cancel already-submitted live orders. Observed trades are still deduplicated and blocked orders are not retried. The current ingestion flow also advances source-token lifecycle state after a blocked execution, so repeated stop/resume cycles can desynchronize source lifecycle state from copied positions.

## Data Limits

Crowding detection is inference, not proof. It is stored and displayed as "suspected copy pressure" and should be treated as a risk signal only.
