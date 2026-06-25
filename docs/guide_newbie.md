# Beginner Guide: Polymarket Copy Trading With This Project

This guide explains the trading concepts behind this project. It is written for people who can run code but are new to Polymarket, prediction markets, and copy trading.

This is not financial advice. Copy trading can lose money quickly, especially in thin markets or when copied trades are delayed.

## 1. What Polymarket Trading Is

Polymarket is a prediction market. Instead of buying a stock or token because you think it will go up, you buy an outcome because you think an event is more likely than the current price implies.

Example market:

> Will the highest temperature in New York City be between 80-81°F on May 29?

Many Polymarket markets are binary:

- `Yes`: pays out if the event happens.
- `No`: pays out if the event does not happen.

Prices are usually between `0.00` and `1.00`.

Rough intuition:

- A `Yes` price of `0.20` means the market is pricing that outcome around 20%.
- A `Yes` price of `0.80` means the market is pricing that outcome around 80%.
- If the outcome wins, a share redeems for `1.00`.
- If the outcome loses, it redeems for `0.00`.

So if you buy 100 `Yes` shares at `0.20`, you spend about `$20`. If `Yes` wins, those shares are worth `$100`. If `Yes` loses, they are worth `$0`.

Polymarket can also have multi-outcome markets. Instead of only `Yes` and `No`, a market may have several possible outcomes, such as candidates, teams, price ranges, or event choices. Each outcome still has its own token, price, orderbook, and payout behavior. If that specific outcome wins, its shares redeem for `1.00`; if it loses, they redeem for `0.00`.

## 2. Outcome Tokens, Markets, and Token IDs

On Polymarket, each outcome is represented by an outcome token. A binary market has separate tokens for `Yes` and `No`; a multi-outcome market has one token for each possible outcome.

Important identifiers you will see in this project:

- `market_title`: human-readable question text.
- `condition_id`: market-level identifier used by Polymarket/CTF.
- `asset_id` / `token_id`: the specific outcome token being traded.
- `outcome`: `Yes`, `No`, or another outcome label in a multi-outcome market.

This distinction matters. A trader can buy one outcome on a market, sell another outcome on the same market, or trade several outcomes as part of a strategy. This project tracks positions by market plus token/outcome, not just by market title.

Because the project is token/outcome based, normal buy/sell copying works for both binary and multi-outcome markets as long as the public API provides the outcome token ID and the CLOB has a usable orderbook for that token.

Official reference: https://docs.polymarket.com/concepts/positions-tokens

## 3. Orderbook Basics

Polymarket uses a CLOB, a Central Limit Order Book.

That means traders place limit orders, and trades happen when buy and sell orders match.

Common terms:

- `bid`: the highest price someone is currently willing to pay.
- `ask`: the lowest price someone is currently willing to sell for.
- `spread`: the gap between best bid and best ask.
- `liquidity`: how much size is available near the current price.
- `slippage`: how much worse your execution price is compared with the source trade or quoted price.

Example:

- Best bid: `0.18`
- Best ask: `0.20`

If you want to buy immediately, you probably pay around the ask, `0.20`.

If you want to sell immediately, you probably receive around the bid, `0.18`.

This project checks the current CLOB orderbook before copying. It does not blindly assume the target trader's price is still available.

Official reference: https://docs.polymarket.com/trading/orderbook

## 4. Limit Orders, Marketable Limit Orders, FOK, and FAK

Polymarket trading is based on limit orders.

A normal limit order says:

> Buy or sell only at this price or better.

For copy trading, you usually do not want to leave stale resting orders behind. If a target trader bought at `0.20`, you do not want your bot placing an order that sits around for hours and fills later in a totally different situation.

So this project is designed around immediate execution behavior:

- `FOK`: Fill or Kill. Fill the whole order immediately or cancel it.
- `FAK`: Fill and Kill. Fill whatever is available immediately and cancel the rest.

Market orders on Polymarket are represented as marketable limit orders. In practice, that means you still provide a worst acceptable price for slippage protection.

Official reference: https://docs.polymarket.com/developers/CLOB/orders/create-order

## 5. Split and Merge Activity

The Polymarket Activity tab can show actions that are not ordinary market trades. Two important examples are `Split` and `Merge`.

In simple terms:

- A `Split` converts collateral into a complete set of outcome tokens for a market.
- A `Merge` converts a complete set of complementary outcome tokens back into collateral.

For a binary market, a complete set means holding matching `Yes` and `No` exposure. In a multi-outcome market, a complete set involves the relevant collection of outcome tokens for that market. Complete sets can be merged back into collateral instead of being sold through the orderbook.

This is different from a normal `Buy` or `Sell`:

- A `Buy` or `Sell` is a market trade against another participant through the orderbook.
- A `Merge` is a position-management action involving complementary outcome tokens.
- A `Merge` does not mean the trader is expressing a new directional view.
- A `Merge` should not be copied as a buy or sell trade.

In this project, target-wallet monitoring intentionally polls `/activity` with `type=TRADE`, and the copy engine only normalizes buy/sell trades into `TradeEvent`. `Merge` activity is ignored by the copy-trading loop.

If you see `Merge` in the Polymarket UI, interpret it as the trader cleaning up or converting balanced outcome exposure, not as a trade signal to copy.

Official reference: https://docs.polymarket.com/developers/CTF/split-merge

## 6. What Copy Trading Means

Copy trading means watching another trader and attempting to follow their trades.

Simple version:

1. Target wallet buys `Yes` on a market.
2. This project detects the trade.
3. It checks whether the trade passes your risk rules.
4. In dry-run mode, it logs what it would do.
5. In live mode, it may submit a similar order from your wallet.

But copy trading is not magic. You are usually late.

Possible problems:

- The target got a better price than you can get.
- The market moved immediately after their trade.
- Other copy traders may crowd into the same position.
- The target may hedge elsewhere.
- The target may have information or reasoning you do not understand.
- The target may simply be wrong.
- Public APIs may expose trades with delay.

This project is built to be cautious by default. It starts in `dry_run`, checks slippage, limits trade size, tracks local copied positions, and refuses risky live mode unless you explicitly enable it.

## 7. What This Project Watches

The main data source is the public Polymarket Data API. For target-wallet monitoring, this project combines:

- `https://data-api.polymarket.com/trades`
- `https://data-api.polymarket.com/activity?type=TRADE`

The website Activity tab may show user activity before the `/trades` endpoint exposes it, so the project also polls `/activity` and deduplicates the results. The project polls these endpoints for each wallet in:

```env
TARGET_WALLETS=0xabc...,0xdef...
```

For every trade, it normalizes the public payload into a `TradeEvent`:

- source wallet
- transaction hash
- timestamp
- market/condition identifiers
- token/asset identifier
- market title
- outcome
- side
- price
- size
- notional USD
- raw payload

It stores seen trades in SQLite so it does not process the same trade repeatedly.

The project does not currently ingest or copy non-trade activity types such as `Merge`.

Binary and multi-outcome buy/sell trades are handled the same way internally: the project follows the specific `asset_id` / `token_id` for the traded outcome.

Official API references:

- https://docs.polymarket.com/api-reference/core/get-trades-for-a-user-or-markets
- https://docs.polymarket.com/api-reference/core/get-user-activity

## 8. Why Old Trades Are Ignored

Copy trading is time-sensitive.

If the target bought 5 minutes ago, the current price may no longer be similar. A late copy can be much worse than the original trade.

That is why this setting exists:

```env
MAX_TRADE_AGE_SECONDS=300
```

If a detected trade is older than this, the project skips it.

For research/dry-run:

- `300` or `600` seconds can be useful because public data may appear late.

For live trading:

- Keep this conservative.
- A smaller value reduces the chance of chasing stale trades.

## 9. Copy Size and Risk Limits

This project does not copy the full target size by default.

Main settings:

```env
COPY_RATIO=0.25
INVERSE_SHARE_COPY_RATIO=0.10
MAX_TRADE_USD=25
MIN_TRADE_USD=1
```

Example:

- Target buys `$100`.
- `COPY_RATIO=0.25`.
- Your intended copy size is `$25`.
- If `MAX_TRADE_USD=10`, your copy size is capped at `$10`.

Formula:

```text
copy size = min(target notional * COPY_RATIO, MAX_TRADE_USD)
```

This copies dollar notional, not share count.

That formula applies to `OUTCOME_SELECTION_MODE=source`. The inverse mode uses
an independent share ratio instead:

```text
target opposite shares = source shares * INVERSE_SHARE_COPY_RATIO
```

The resulting dollar cost is still limited by `MAX_TRADE_USD`, the daily spend
cap, per-market exposure, available balance, visible liquidity, and fees.

Example:

- Source trader buys `$10` worth of shares at `0.0050`, receiving about `2000` shares.
- Your bot copies `$10`, but the current executable price is `0.0170`.
- You receive only about `588` shares.

So even with `COPY_RATIO=1`, your copied shares can be much lower than the source trader's shares if your entry price is worse. This is normal and is one reason `show-orders` separates `source shares` from `our shares`.

`MIN_TRADE_USD` ignores tiny trades. This is useful because very small trades may be noise, testing, dust, or not worth copying after slippage.

## 10. Slippage Protection

The most important copy-trading safety check is slippage.

Setting:

```env
MAX_SLIPPAGE_CENTS=2
```

For a `BUY`:

- Target bought at `0.20`.
- Max slippage is `2` cents.
- Highest allowed copy price is `0.22`.
- If current ask is `0.23`, skip.

For a `SELL`:

- Target sold at `0.20`.
- Max slippage is `2` cents.
- Lowest acceptable sell price is `0.18`.
- If current bid is `0.17`, skip.

This protects you from chasing after the price has already moved.

Slippage and maximum entry price answer different questions. Slippage compares your executable price with the source trader's price. An optional maximum buy price rejects expensive outcome shares even when the source trader received a similar price:

```env
MAX_BUY_PRICE=0.90
```

For example, a source buy at `0.89` and executable copy price of `0.905` can pass a `2` cent slippage check but fail `MAX_BUY_PRICE=0.90`. This limit applies only to buys.

The Source States dashboard stores the decision-time source price, executable price, allowed slippage price, and calculated slippage. These are historical snapshots, not quotes fetched later.

### Inverse Up/Down mode

By default, the bot copies the source trader's selected outcome:

```env
OUTCOME_SELECTION_MODE=source
```

For an experimental contrarian dry-run, you can select the opposite outcome on strict two-outcome `Up`/`Down` markets:

```env
OUTCOME_SELECTION_MODE=inverse_up_down
INVERSE_SHARE_COPY_RATIO=0.10
```

In this mode, a source `BUY Down` becomes a copied `BUY Up`, and a later source `SELL Down` reduces the copied `Up` position. The reverse applies to source `Up` trades.

Inverse buys target the configured fraction of the source trade's share count,
not its dollar notional. For example, a source purchase of `100 Down` shares
with `INVERSE_SHARE_COPY_RATIO=0.10` targets `10 Up` shares at the currently
executable Up price. `COPY_RATIO` does not size inverse buys.

The bot does not infer the opposite token from the market title. It queries authoritative market metadata and proceeds only when there are exactly two labeled outcomes, `Up` and `Down`, with distinct token IDs. Other binary markets, multi-outcome markets, missing metadata, and source tokens outside the returned pair are rejected.

For slippage, the inverse reference price is `1 - source price`. For example, if the source buys `Down` at `0.60`, the copied `Up` reference is `0.40`; the current `Up` ask is compared with `0.40`. All liquidity, maximum buy price, exposure, balance, execution, position, and settlement logic uses the copied token. The Copied Orders tab shows source outcome, copied outcome, source price, and inverse reference price separately.

This mode bets against the source trader and should be evaluated as a separate strategy. It is disabled by default.

### Selective inverse Down-underdog mode

For a narrower dry-run hypothesis, the bot can ignore every BUY except a source
purchase of `Down` below a strict price threshold, then purchase authoritative
`Up` shares:

```env
OUTCOME_SELECTION_MODE=inverse_down_underdog
INVERSE_SHARE_COPY_RATIO=0.10
INVERSE_DOWN_MAX_SOURCE_PRICE=0.50
MAX_COPIED_BUYS_PER_WALLET_MARKET=5
CONDITION_EXPOSURE_CAP_USD=25
```

With the default threshold, source `BUY Down` at `0.49` can become copied
`BUY Up`; source `BUY Down` at `0.50` or higher and all source `BUY Up` signals
are recorded as strategy skips. These skips do not freeze source lifecycle
state. A later source `SELL Down` can still reduce an existing copied `Up`
position even if its sell price is above the entry threshold.

`MAX_COPIED_BUYS_PER_WALLET_MARKET` counts accepted BUY orders for one source
wallet and condition. `CONDITION_EXPOSURE_CAP_USD` sums open cost across every
copied outcome token in the condition. Both settings are optional and apply to
all outcome-selection modes when configured.

### Shadow-regime Down-underdog mode

This dry-run-only experiment uses the selective inverse strategy as a permanent
paper-only shadow strategy:

```env
OUTCOME_SELECTION_MODE=shadow_regime_down_underdog
INVERSE_SHARE_COPY_RATIO=0.10
INVERSE_DOWN_MAX_SOURCE_PRICE=0.40
MAX_COPIED_BUYS_PER_WALLET_MARKET=1
CONDITION_EXPOSURE_CAP_USD=25

SHADOW_REGIME_WINDOW=50
SHADOW_REGIME_CONFIRMATION_MARKETS=10
SHADOW_REGIME_INITIAL_PATH=warmup
SHADOW_REAL_TRADE_POLICY=auto_regime
SHADOW_FOLLOW_MIN_PRICE=0.70
SHADOW_INVERT_MIN_PRICE=0.40
SHADOW_INVERT_MAX_PRICE=0.45
```

Every qualifying source `BUY Down` first goes through the normal
`inverse_down_underdog` checks as a shadow `BUY Up`. Only a shadow order that
would have been executable is recorded, and only one is recorded per source
wallet and market. The first 50 resolved shadow markets are warm-up and create
no real dry-run orders when `SHADOW_REGIME_INITIAL_PATH=warmup`. Set the initial
path to `follow_shadow` or `invert_shadow` to place real dry-run orders
immediately while the first statistical window accumulates.

After warm-up:

- shadow win rate above 50% activates `follow_shadow`, so the real dry-run buys
  the same Up token for each newly accepted shadow order;
- shadow win rate below 50% activates `invert_shadow`, so the real dry-run buys
  Down for exactly those same shadow signals;
- exactly 50% retains the current path, or remains in warm-up if no path has
  been established.

The active path is persistent. If the rolling 50-market rate crosses to the
other side of 50%, it must remain there for 10 consecutive newly resolved
shadow markets before switching. A tie or recovery resets pending confirmation.
The switch affects the next accepted shadow order; it does not rewrite existing
positions.

Inspect the state at any time:

```bash
uv run pct show-shadow-regime
```

The calculated automatic path and the effective execution path are shown
separately. Override future signals without restarting the bot:

```bash
uv run pct set-shadow-regime invert_shadow --reason "manual experiment"
uv run pct set-shadow-regime follow_shadow
uv run pct set-shadow-regime auto
```

Overrides are stored in SQLite, survive restarts, and never rewrite existing
positions. Every accepted shadow signal also records an executable quote and
decision for its opposite token so future counterfactual PnL can be calculated.

Set `SHADOW_REAL_TRADE_POLICY=price_filter` to ignore the rolling regime for
real execution and trade only selected price bands:

- follow the shadow Up trade when its executable price is at least
  `SHADOW_FOLLOW_MIN_PRICE`;
- otherwise invert to Down when the Down executable price is in
  `[SHADOW_INVERT_MIN_PRICE, SHADOW_INVERT_MAX_PRICE)`;
- otherwise record the shadow and opposite quote, but place no real dry-run
  order.

The output reports resolved shadow markets, rolling wins and win rate, active
path, desired path, pending path, confirmation progress, and switch count.

## 11. BUY vs SELL Behavior

This project treats buys and sells differently.

For `BUY` trades:

- It may copy if the trade passes size, age, liquidity, slippage, and risk checks.

For `SELL` trades:

- It only sells if your local bot has a copied position in that same market/outcome.
- It does not blindly short.
- It does not sell just because the target wallet sells something you never copied.
- It caps the copied sell by your local copied share balance.

This is important. Without this rule, a copy bot could accidentally open short-like exposure or sell unrelated holdings.

Example:

1. Source trader buys 20 shares.
2. Your bot copies that buy and now has 20 copied shares.
3. Source trader later buys 30 more shares, but your bot skips it because it fails a risk rule.
4. Source trader later sells 25 shares.

Your bot must not sell 25 shares, because it only has 20 copied shares. The project caps the sell to the local copied position. In this example it can sell at most 20 shares, and it will never intentionally go negative/short.

This also means your local copied portfolio can diverge from the source trader's real portfolio. That divergence is normal when some source trades are skipped due to slippage, age, size caps, daily spend caps, or exposure caps.

Related setting:

```env
ALLOW_SHORT_SELLS=false
```

The current project is intentionally conservative around sells.

## 12. Source Position Lifecycle Tracking

The project is configured to avoid copying tokens where the source trader already had a position before your bot started.

On startup, the bot queries the source wallet's current public positions:

```text
https://data-api.polymarket.com/positions?user=...
```

Every token the source already holds is marked `pre_existing`. The bot skips copy-buying and copy-selling pre-existing tokens because it did not observe the source trader's full position lifecycle from the beginning.

For tokens the source did not hold at startup, the bot can mark the lifecycle as `clean`. A clean token means the bot has observed the source activity from the start of that position lifecycle.

If a source buy is copied at a capped size, the token can still remain clean because the bot knows both how many shares the source added and how many shares the bot copied.

When the source later sells a clean token, the bot sizes the copied sell by source-position ratio.

Example:

- Source observed position: `1000` shares.
- Your copied position: `250` shares.
- Source sells `100` shares.
- Source reduced position by `10%`.
- Your bot sells `10%` of your local copied position, or `25` shares.

If a source trade cannot be copied after tracking begins, the token is marked `frozen`. Once frozen, the bot stops copying that token. This is safer than pretending the local portfolio still mirrors the source trader's risk.

Examples that can freeze a token:

- a source buy fails slippage checks,
- a source buy cannot fit remaining daily spend,
- a source buy cannot fit per-market exposure,
- a source sell cannot be copied safely,
- the API payload is missing a token ID.

Relevant safe defaults:

```env
SOURCE_POSITION_POLICY=skip_preexisting
SELL_SIZING_MODE=source_position_ratio
ON_RISK_MISMATCH=freeze_token
RISK_MISMATCH_SCOPE=wallet_market
SOURCE_POSITION_SIZE_THRESHOLD=0.01
```

`RISK_MISMATCH_SCOPE=token` freezes only the rejected outcome token.
`wallet_market` is safer for paired or evolving strategies: after any token
freezes, later BUYs for either outcome from that source wallet and market are
rejected. SELLs may still reduce an existing copied position.

Inspect lifecycle state with:

```bash
uv run pct show-source-states
```

The lifecycle model is conservative. It may skip trades that a more aggressive bot would copy, but it reduces the chance of accidentally following only the middle of a larger source position.

## 13. Local Position Tracking

The project maintains local copied positions in SQLite.

A copied position stores:

- market ID
- token/asset ID
- outcome
- total shares
- average entry price
- total cost
- source wallets copied from
- status
- realized PnL if calculable

You can inspect positions with:

```bash
uv run pct show-positions
uv run pct show-dashboard
uv run pct show-orders
uv run pct show-redemptions
uv run pct dashboard
```

In `dry_run`, these are simulated positions. They show what the project would have done.

In `live`, they are local records of submitted/copied behavior. For serious live use, you should still reconcile against actual Polymarket balances.

`uv run pct dashboard` starts a read-only browser dashboard at `http://127.0.0.1:8765`. Run it in a second terminal while `uv run pct run-dry` or `uv run pct run-live` continues running in the first terminal. It shows the same local copied positions, copied orders, source token lifecycle states, recent trades, and recent errors, and refreshes from SQLite plus public CLOB quotes. It does not place trades.

The dashboard's `Spend today` metric and `Our Performance` tab use `TRADING_DAY_TIMEZONE` from `.env`. This matters around midnight: if your trading day is `Europe/Prague`, the daily spend cap resets at Prague midnight instead of UTC midnight.

You can also simulate a finite cash balance:

```env
DRY_RUN_STARTING_BALANCE_USD=3000
```

Dry-run buys reduce `Available cash`; copied sells and settlement payouts replenish it. Open-position market value is not spendable cash. In live mode this static setting is ignored and the bot queries the authenticated CLOB collateral balance before a buy. A balance lookup failure rejects the trade rather than assuming funds are available.

To restrict new positions to markets ending soon:

```env
MAX_SECONDS_UNTIL_MARKET_END=86400
```

This example permits buys only when the advertised market end is within 24 hours. Missing or invalid end metadata causes the buy to be skipped. The timestamp is the scheduled market end, not a guarantee of final resolution or payout time. Copied sells remain allowed because they reduce existing exposure.

The `Our Performance` tab is your local bot accounting by trading day. In `dry_run`, it is simulated accounting from the orders and settlements the bot would have made. In `live`, it shows what the bot recorded locally; you should still reconcile live balances and manual redemptions against your actual Polymarket wallet.

In `Our Performance`, `cashflow` is not settlement-only profit. It is same-day local cash movement:

```text
cashflow = settlement payout + sell notional - buy spend
```

That means today's `cashflow` can go down even when no settlement changed. If the bot copies more buys during the day, `buy spend` increases immediately, while the payout from those new positions may not arrive until later, if they win and resolve. For actual profit/loss on resolved markets, look at `settlement pnl`.

The dashboard's `Source Performance` tab reads Polymarket's public user PnL chart data for each configured `TARGET_WALLETS` wallet. This does not require your bot to run 24/7 because the data comes from Polymarket's historical PnL endpoint. Completed daily points are stored in SQLite. The dashboard deliberately excludes the current day and only refetches a wallet's PnL history when yesterday's completed point is missing locally. The large chart uses daily cumulative PnL candles and a daily PnL table:

- `open`: previous available cumulative PnL point
- `close`: current day's cumulative PnL point
- `daily PnL`: `close - open`
- `high` / `low`: with `fidelity=1d`, this is the range between the previous and current daily point, not true intraday high/low

Use this tab to see whether a watched trader is currently improving, flat, or drawing down before you decide whether to copy them.

When a market resolves, winning shares become redeemable for settlement payout and losing shares become worth `0`.

The project scans your local copied positions against public Gamma market resolution data:

- In `dry_run`, winning copied positions are marked `resolved`, payout is recorded, and realized PnL becomes `payout - cost`.
- In `dry_run`, losing copied positions are marked `resolved`, payout is `0`, and realized PnL becomes `-cost`.
- In `live`, losing copied positions are marked resolved locally as losses.
- In `live`, winning copied positions are marked `redeem_required` because your own wallet must redeem the winning shares manually.

For short-window markets such as `Up or Down`, the scanner also checks the public CLOB market endpoint by condition ID. That endpoint exposes token-level `winner` / `price` data after the market closes, which lets the bot settle both wins and losses locally. Losing dry-run settlements should appear as `payout = 0` and negative `realized`.

Run an immediate scan with:

```bash
uv run pct refresh-resolutions
```

Polymarket also shows `Redeem` activity for winning shares. `Redeem` is not a trade. It means the holder claimed settlement payout on resolved winning tokens. Public source-wallet redemption payloads may identify only the condition and total payout, with no asset or outcome. Therefore, the project uses `REDEEM` only to trigger an authoritative CLOB/Gamma resolution lookup. It never assumes that every copied outcome in the condition won.

If the bot was not running when a recent source redemption happened, you can refresh source redeem activity manually:

```bash
uv run pct refresh-redemptions --wallet 0x...
```

In `live`, the source trader redeeming does not redeem your wallet's shares. Actual live redemption is a wallet settlement transaction through Polymarket's settlement/adapter path, not a CLOB buy/sell order.

### Repairing legacy settlement accounting

Versions before the token-level settlement safeguard could treat a condition-only source redemption as a `$1` payout for every copied outcome in that condition. This could incorrectly mark losing positions, or even both `Up` and `Down`, as winners.

Preview authoritative corrections without changing the database:

```bash
uv run pct reconcile-settlements
```

Stop older bot processes, review the preview, and then apply:

```bash
uv run pct reconcile-settlements --apply --i-understand-this-updates-local-accounting
```

Rows whose public market result is not yet available remain untouched and can be retried later.

Dry-run positions persist in SQLite across runs. That is useful when you want to let a paper portfolio accumulate over time, but confusing when you want a fresh experiment.

To clear simulated copied orders, simulated positions, and crowding observations:

```bash
uv run pct reset-simulation --i-understand-this-deletes-local-simulation
```

By default this keeps `target_trades`, the table of already-seen wallet trades. Keeping it prevents the next run from reprocessing the same historical activity. If you also want to delete seen target trades, use:

```bash
uv run pct reset-simulation --i-understand-this-deletes-local-simulation --include-seen-trades
```

Use `--include-seen-trades` carefully. On the next run, visible wallet history may be seeded again or, depending on your settings, processed again.

The dashboard estimates open copied position PnL using the current best bid as the exit price. This is a mark-to-market estimate, not guaranteed execution, settlement value, or full realized PnL.

The dashboard has two different views:

- `Copied Position Mark-to-Market`: your local copied position after applying your `COPY_RATIO`, caps, fills, and any local sells.
- `Copied Trades: Source vs Our Mark-to-Market Estimate`: individual source trades that triggered copied orders, with source-trader shares shown separately from your copied shares.

Do not sum `source shares` and expect it to equal your copied position size. `source shares` are the target trader's original trade size. Your copied shares depend on copied notional, `COPY_RATIO`, `MAX_TRADE_USD`, your simulated/live execution price, and whether any copied sells reduced the local position.

To compare your simulated entry against the source trader's entry, use:

```bash
uv run pct show-orders
```

In the dashboard's `Copied Orders` tab:

- `source px` is the price the source trader got on the original Polymarket trade. It comes from Polymarket's public trade data.
- `our px` is the bot's copied entry price. In `dry_run`, this is the simulated copied entry derived from the public orderbook quote used by the decision engine. In `live`, it is the bot's recorded fill price when available, otherwise the submitted/limit price estimate.
- `diff` is `our px - source px`.
  For a copied buy, a positive diff means you entered worse than the source trader because you paid more; a negative diff means you entered better because you paid less.
  For a copied sell, the meaning flips: a positive diff means you sold for more than the source trader, which is better; a negative diff means you sold for less, which is worse.

The dashboard colors `diff` as price improvement, not as simple positive/negative math. Green means your copied price was better than the source trader's price for that side. Red means your copied price was worse.

These prices may differ because your bot sees the source trade after it happened, then checks the current orderbook. If the market moved, liquidity disappeared, or the bot had to cross the current ask/bid, your copied entry can be worse than the source trader's entry. This is why `MAX_SLIPPAGE_CENTS` matters.

Dashboard timestamps use `TRADING_DAY_TIMEZONE` and are displayed in 24-hour format. Database timestamps are stored in UTC, then converted for display.

### Positions tab columns

The dashboard's `Positions` tab shows your local copied position per market/outcome token. In `dry_run`, these are simulated positions. In `live`, they are positions reconstructed from orders the bot recorded locally, so you should still reconcile against your actual Polymarket wallet.

- `market`: the market name. When the source trade included a valid Polymarket event slug, the name links to that market's page on Polymarket.
- `avg`: your average entry price for the currently tracked copied position. If the bot copied multiple buys of the same token, this is the weighted average price.
- `cost`: the remaining cost basis of the copied position. Roughly, this is how many dollars the bot has tied up in the currently open shares.
- `bid`: the current best bid from the public CLOB orderbook. This is the highest visible price someone is currently willing to pay for the token.
- `ask`: the current best ask from the public CLOB orderbook. This is the lowest visible price someone is currently willing to sell the token for.
- `est value`: the dashboard's estimated value of the position. For open positions, it uses `shares * bid`, because the bid is the price you could theoretically sell into immediately. For `redeem_required` winners, it uses the redeemable share payout estimate.
- `unrealized`: estimated open profit or loss that has not been locked in yet. For open positions, the rough formula is `est value - cost`.
- `realized`: profit or loss already locked into local accounting, usually from copied sells or market settlement/resolution.
- `total`: `unrealized + realized`. This is the dashboard's current total local PnL estimate for that copied position.

Important: `bid`, `ask`, `est value`, and `unrealized` are estimates. A visible bid can disappear, a market can be thin, and a real live wallet may differ from local bot accounting if an order partially fills, fails, or is manually changed outside the bot.

After a market's advertised end, Polymarket's website may show an outcome before the public API publishes the authoritative token payout. The dashboard labels this temporary state `awaiting_resolution`. If no bid exists, estimated value and PnL stay blank instead of showing a misleading full loss. The default resolution scan interval is 60 seconds.

Dry-run execution walks the visible order book only through levels allowed by the slippage and maximum-buy-price limits. It records partial FAK-style fills, the average and worst execution prices, and an estimated market fee. The simulation still cannot guarantee that the displayed liquidity would remain available long enough for a real order to consume it.

For later debugging and strategy comparison, the database also keeps:

- the exact strategy settings used for every decision,
- source-trade, observation, and completed-decision timestamps,
- polling delay and decision-processing duration,
- complete decision-time market metadata and visible order book,
- simulated depth fills and fee calculations,
- structured rejection reasons,
- authoritative payout maps after evaluated markets resolve, including rejected trades.

Use a fresh `DATABASE_URL` filename for a clean experiment. Reusing an older database is supported, but old positions, spending, and decisions remain part of local accounting and can make comparison harder.

## 14. Mark-to-Market PnL

`our mtm pnl` means "our mark-to-market profit and loss."

In this project, it estimates what your copied position would be worth if you exited immediately at the current best bid.

For a copied `BUY`, the rough formula is:

```text
our mtm pnl = (current best bid - our entry price) * our copied shares
```

Example:

- Source trader buys at `0.2580`.
- Your simulated copy enters at `0.2590`.
- You copy `10` shares.
- Current best bid is `0.2580`.

Then:

```text
(0.2580 - 0.2590) * 10 = -0.01
```

That is a tiny negative mark-to-market result. It mostly reflects spread/slippage.

Another example:

- Your simulated copy enters at `0.2430`.
- Current best bid is `0.0020`.
- You copied about `20.7` shares.

Then your mark-to-market PnL is strongly negative because the market is currently bidding almost nothing for the token.

Good vs bad:

- Positive `our mtm pnl`: if you could sell at the current bid, the copied position would be up.
- Near-zero `our mtm pnl`: the copied position is roughly flat after spread.
- Small negative `our mtm pnl`: normal immediately after entering, because buying usually crosses the ask and selling immediately hits the bid.
- Large negative `our mtm pnl`: the market moved against you, liquidity disappeared, or the best bid is much lower than your entry.
- Blank or unavailable bid: the project could not find an executable bid, so the estimate is incomplete.

This value is useful, but it is not perfect:

- It uses the best bid, not guaranteed full-size exit liquidity.
- It does not predict settlement.
- It can look terrible in thin markets even if the outcome later wins.
- It can look good temporarily even if the outcome later loses.
- It is not the source trader's total PnL.

Should you minimize `our mtm pnl` by lowering `COPY_RATIO`?

Not exactly. Lowering `COPY_RATIO` reduces the dollar size of gains and losses, but it does not make the copied trade better.

If a copied trade is down `-80%`, lowering `COPY_RATIO` only makes the dollar loss smaller. It does not improve the price, timing, or edge.

Settings affect different parts of the problem:

- `COPY_RATIO`: controls position size.
- `MAX_TRADE_USD`: caps dollars per copied trade.
- `MAX_SLIPPAGE_CENTS`: controls how much worse your entry may be than the selected outcome's reference price.
- `OUTCOME_SELECTION_MODE`: optionally selects the authoritative opposite token for strict `Up`/`Down` markets.
- `MAX_BUY_PRICE`: rejects copied buys above an absolute outcome-token price.
- `MAX_SECONDS_UNTIL_MARKET_END`: restricts buys to markets whose advertised end is close enough.
- `DRY_RUN_STARTING_BALANCE_USD`: simulates replenishable available collateral in dry-run mode.
- `MAX_TRADE_AGE_SECONDS`: controls how stale a trade can be before it is ignored.
- `MIN_TRADE_USD`: filters tiny source trades.
- `CROWDING_MAX_FOLLOWERS`: helps avoid trades with suspected copy pressure.

If `our mtm pnl` is repeatedly ugly, first investigate why:

- Are you entering too late?
- Is `MAX_TRADE_AGE_SECONDS` too loose?
- Is `MAX_SLIPPAGE_CENTS` too loose?
- Are these markets very illiquid?
- Is the trader buying into fast-moving weather or news markets where prices collapse quickly?
- Is the trader doing a strategy involving buys, sells, and merges that your simple copy logic does not fully capture?

Then choose the right response:

- Reduce `COPY_RATIO` or `MAX_TRADE_USD` to reduce risk while testing.
- Tighten `MAX_SLIPPAGE_CENTS` to avoid worse entries.
- Tighten `MAX_TRADE_AGE_SECONDS` to avoid stale copies.
- Increase `MIN_TRADE_USD` to ignore tiny noisy trades.
- Stop copying that wallet or market type if the pattern is consistently bad.

## 15. Suspected Copy Pressure

One of this project's special features is crowding detection.

The idea:

1. A target wallet trades.
2. The project looks at trades in the same market shortly after.
3. It checks whether other wallets traded the same side/outcome at similar or worse prices.
4. It records this as "suspected copy pressure."

This is inference, not proof.

Someone trading after the target may be:

- copying the target,
- reacting to the same public information,
- following another trader,
- part of the same trading group,
- or simply trading coincidentally.

Important settings:

```env
ENABLE_CROWDING_CHECK=true
CROWDING_LOOKBACK_SECONDS=60
CROWDING_MAX_FOLLOWERS=5
```

The recorded score includes:

- follower count
- follower notional
- median delay
- average slippage vs target
- repeated follower wallets

If follower count is above `CROWDING_MAX_FOLLOWERS`, the project can skip the copy because the trade may already be crowded.

Commands:

```bash
uv run pct show-crowding --wallet 0x...
uv run pct refresh-crowding --wallet 0x... --hours 24
```

`refresh-crowding` is useful because the bot may detect the target trade immediately, but the full follower window only becomes knowable after `CROWDING_LOOKBACK_SECONDS` has passed.

## 16. Dry-Run Mode

Dry-run mode is the default and safest way to learn.

```env
COPY_MODE=dry_run
```

Run:

```bash
uv run pct run-dry
```

In dry-run mode, the project:

- watches target wallets,
- logs detected trades,
- checks orderbook prices,
- decides whether it would copy,
- records simulated orders,
- updates simulated copied positions,
- never submits real orders.

A good beginner workflow:

```bash
uv run pct run-dry
```

Let it run. After a detected trade, wait at least your crowding window plus a little extra time. Then stop with `Ctrl+C`.

Inspect:

```bash
uv run pct refresh-crowding --wallet 0x... --hours 3
uv run pct show-crowding --wallet 0x...
uv run pct show-recent-trades
uv run pct show-positions
```

## 17. Live Mode

Live mode is intentionally harder to start.

```env
COPY_MODE=live
POLYMARKET_PRIVATE_KEY=...
MAX_TRADE_USD=...
```

Run:

```bash
uv run pct run-live --i-understand-live-trading-risk
```

Live mode refuses to start unless:

- the explicit risk flag is provided,
- `POLYMARKET_PRIVATE_KEY` is set,
- `MAX_TRADE_USD` is set,
- `COPY_RATIO <= 1` unless explicitly allowed,
- safety checks pass.

Emergency stop:

```bash
touch STOP_TRADING
```

If `STOP_TRADING` exists, the project blocks new dry-run and live BUY/SELL orders but keeps running. Wallet polling and periodic resolution scanning continue, so existing dry-run positions can still settle and live winners can still be marked `redeem_required`. The file does not cancel live orders that were already submitted.

Trades observed while the stop file exists are stored with copied-order status `blocked` and are not retried after trading is enabled again.

Blocked trades do not advance the tracked source-token lifecycle. They are still deduplicated and are not replayed when the file is removed.

Remove it only when you intentionally want to allow trading again:

```bash
rm STOP_TRADING
```

Do not use live mode until you have watched dry-run behavior for long enough to understand how the target wallet trades and how your settings behave.

## 18. Key `.env` Settings Explained

```env
TARGET_WALLETS=0x...
```

Wallets to monitor. Start with one wallet.

```env
COPY_MODE=dry_run
```

Use `dry_run` while learning. Use `live` only after testing.

```env
SEED_EXISTING_TRADES_ON_STARTUP=true
```

On startup, mark already-visible history as seen so the bot only processes new trades from that point forward.

```env
SOURCE_POSITION_POLICY=skip_preexisting
```

Skip tokens the source already held before bot startup.

```env
SELL_SIZING_MODE=source_position_ratio
```

For clean tracked tokens, copied sells are sized by the source's observed position reduction ratio.

```env
ON_RISK_MISMATCH=freeze_token
```

Freeze a token if a source trade cannot be copied after tracking begins.

```env
SOURCE_POSITION_SIZE_THRESHOLD=0.01
```

Minimum source position size to treat as pre-existing on startup.

```env
MAX_TRADE_USD=25
```

Maximum copied size per trade.

```env
COPY_RATIO=0.25
```

Copy 25% of the target's notional size.

```env
MIN_TRADE_USD=1
```

Ignore tiny trades below `$1`.

```env
MAX_TRADE_AGE_SECONDS=300
```

Ignore trades older than this.

```env
MAX_SLIPPAGE_CENTS=2
```

Maximum worse price versus the target's entry.

```env
DAILY_SPEND_CAP_USD=100
```

Local daily cap for copied buys, including estimated fees. Leave it empty or use `none`/`unlimited` to disable this cap while retaining the available-balance and exposure limits.

```env
MARKET_TYPE_FILTER=short_duration_up_down
UP_DOWN_MIN_DURATION_SECONDS=300
UP_DOWN_MAX_DURATION_SECONDS=900
```

Optionally restrict copied buys to authoritative short-duration `Up`/`Down` markets.

Duration is measured from Polymarket's `eventStartTime` to `endDate`. The earlier market creation/listing timestamp is not treated as the prediction window.

```env
MIN_NET_UPSIDE_USD=1
MIN_NET_UPSIDE_PERCENT=5
NET_UPSIDE_SAFETY_MARGIN_USD=0.25
INCLUDE_EXIT_FEE_IN_UPSIDE=false
```

Optionally reject buys whose maximum payout advantage is too small after visible book depth, entry fee, and the safety margin. Enable the exit-fee option only when you expect to sell before resolution.

```env
PER_MARKET_EXPOSURE_CAP_USD=50
```

Maximum local exposure in one market/outcome.

```env
ENABLE_CROWDING_CHECK=true
```

Enable suspected copy pressure checks.

```env
CROWDING_LOOKBACK_SECONDS=60
```

How long after the target trade to look for possible followers.

```env
CROWDING_MAX_FOLLOWERS=5
```

Skip or avoid trades that appear too crowded.

```env
BLOCK_MARKET_KEYWORDS=sports,election
```

To allow new buys only for markets whose titles contain at least one configured
substring, use:

```env
ALLOW_MARKET_TITLE_KEYWORDS=bitcoin,ethereum
```

Matching is case-insensitive and uses OR logic. An empty value disables this
allowlist. It applies only to BUYs so copied SELLs can still reduce existing
positions.

Optional title keywords to avoid.

## 19. Choosing Wallets to Watch

Do not watch a wallet just because it recently made money.

Better questions:

- Does the wallet trade markets you understand?
- Does it enter early or chase late?
- Does it use small sizing or large concentrated bets?
- Does it often sell quickly?
- Does it trade illiquid markets?
- Does it appear to be copied by others?
- Are its wins repeatable or just one lucky market?

Use dry-run first and inspect the results over time.

## 20. Common Beginner Mistakes

Mistake: copying too large.

Fix: lower `COPY_RATIO` and `MAX_TRADE_USD`. This reduces dollar risk, but does not improve the quality of each copied entry.

Mistake: allowing too much slippage.

Fix: lower `MAX_SLIPPAGE_CENTS`.

Mistake: thinking `COPY_RATIO` fixes bad mark-to-market PnL.

Fix: use `COPY_RATIO` for sizing. Use slippage, age, liquidity, and wallet selection to improve trade quality.

Mistake: copying a token where the source already had a position before your bot started.

Fix: keep `SOURCE_POSITION_POLICY=skip_preexisting`.

Mistake: continuing to copy a token after missing one source trade.

Fix: keep `ON_RISK_MISMATCH=freeze_token`.

Mistake: copying stale trades.

Fix: lower `MAX_TRADE_AGE_SECONDS` for live mode.

Mistake: trusting crowding detection as proof.

Fix: treat it as a risk signal only.

Mistake: running live mode too soon.

Fix: collect dry-run data first.

Mistake: copying markets you do not understand.

Fix: use `BLOCK_MARKET_KEYWORDS` or monitor different wallets.

Mistake: treating `Merge` as a buy or sell signal.

Fix: remember that merge is position management for complementary outcome tokens. This project ignores it in the copy loop.

Mistake: assuming every market is binary.

Fix: look at the `outcome` and `asset_id` / `token_id`. The project tracks the specific traded outcome token, which is what matters for multi-outcome markets.

## 21. A Suggested Beginner Configuration

For learning:

```env
TARGET_WALLETS=0xYourTargetWallet
COPY_MODE=dry_run
SEED_EXISTING_TRADES_ON_STARTUP=true
SOURCE_POSITION_POLICY=skip_preexisting
SELL_SIZING_MODE=source_position_ratio
ON_RISK_MISMATCH=freeze_token
SOURCE_POSITION_SIZE_THRESHOLD=0.01

MAX_TRADE_USD=5
COPY_RATIO=0.10
MAX_SLIPPAGE_CENTS=2
MIN_TRADE_USD=1
MAX_TRADE_AGE_SECONDS=300

ENABLE_CROWDING_CHECK=true
CROWDING_LOOKBACK_SECONDS=60
CROWDING_MAX_FOLLOWERS=3

DAILY_SPEND_CAP_USD=25
PER_MARKET_EXPOSURE_CAP_USD=10
ALLOW_SHORT_SELLS=false
```

This is intentionally small. The goal is to learn behavior first, not maximize size.

## 22. How to Read a Dry-Run Log

Example:

```text
detected trade wallet=0x... side=BUY outcome=Yes price=0.2000 size=76.0000 notional=15.20 market=...
copy decision=False reason=trade too old: 156.3s
```

Meaning:

- The target bought `Yes`.
- They paid around `0.20`.
- They bought 76 shares.
- The notional was about `$15.20`.
- The bot skipped it because the public data showed it later than your allowed age window.

Example:

```text
copy decision=True reason=copy allowed
```

Meaning:

- The trade passed your filters.
- In dry-run, a simulated order was recorded.
- In live mode, the bot would attempt an order if all live safety checks passed.

## 23. Official Docs Used by This Project

- API overview: https://docs.polymarket.com/api-reference
- Data API trades endpoint: https://docs.polymarket.com/api-reference/core/get-trades-for-a-user-or-markets
- Data API activity endpoint: https://docs.polymarket.com/api-reference/core/get-user-activity
- Data API positions endpoint: https://docs.polymarket.com/api-reference/core/get-current-positions-for-a-user
- Positions and outcome tokens: https://docs.polymarket.com/concepts/positions-tokens
- Split and merge: https://docs.polymarket.com/developers/CTF/split-merge
- Orderbook: https://docs.polymarket.com/trading/orderbook
- Limit orders: https://docs.polymarket.com/polymarket-learn/trading/limit-orders
- CLOB order creation: https://docs.polymarket.com/developers/CLOB/orders/create-order
- CLOB trading overview: https://docs.polymarket.com/developers/CLOB/trades/trades-data-api

## 24. Final Advice

Start with dry-run. Let the project collect real examples. Read the logs. Inspect skipped trades. Look at simulated positions. Adjust one setting at a time.

The goal is not simply to copy a wallet. The goal is to understand whether copying that wallet, with your delays, your slippage, your size, and your risk limits, would have made sense.
