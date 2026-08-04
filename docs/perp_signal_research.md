# BTC Perpetual Signal Direction Research

`pct analyze-perp-signals` is an offline, read-only study of whether filled BTC
Up/Down dry-run orders point in the same direction as a TradingView BTC
perpetual chart between bot execution and the Polymarket market end.

It does not place orders, contact an exchange, or modify the bot database. It
also does not model perp fees, slippage, funding, leverage, or liquidation, so
"correct" means directionally compatible with a perp trade, not necessarily
profitable.

## Export TradingView data

Use the `MEXC:BTCUSDT.P` chart with a 10-second or 45-second interval and export
chart data as CSV. Prefer 10 seconds when its shorter export history covers the
signals you need; use 45 seconds for the longer historical coverage. Either
supported TradingView time format is accepted:

- ISO timestamps with an explicit offset, such as `+02:00`
- UNIX timestamps in seconds

The CSV must contain `time`, `open`, `high`, `low`, and `close`. `Volume` is
optional. Timestamps are normalized to UTC.

TradingView exposes different rolling windows depending on the chart interval.
Observed exports have covered only about three days at 10 seconds and about ten
days at 45 seconds. Save exports regularly if you want to build a longer local
history. Files may overlap: identical candles are deduplicated, while
the newer export may replace the older export's final still-forming candle when
it later becomes finalized. A conflict anywhere inside completed history still
stops the analysis for inspection.

## Run the analysis

With the approved bot database name and one export:

```bash
uv run pct analyze-perp-signals \
  --source-db polymarket_copy_trader_202607251157.sqlite3 \
  --tradingview-csv path/to/MEXC_BTCUSDT.P_45s.csv \
  --round-trip-cost-bps 2
```

Repeat `--tradingview-csv` for multiple archives, or point to a directory:

```bash
uv run pct analyze-perp-signals \
  --source-db polymarket_copy_trader_202607251157.sqlite3 \
  --tradingview-csv-dir tradingview-data
```

When no CSV option is supplied, the command first reports the UTC coverage
required by qualifying bot signals and prompts for a file path. The command
does not read `.env`; its default source database is
`polymarket_copy_trader_202607251157.sqlite3`.

## Exact rules

Qualifying signals are filled `dry_run` BTC Up/Down `BUY` orders with positive
filled shares. Rejected, blocked, zero-fill, and sell orders are excluded.

- Execution time is `copied_orders.recorded_at`. The stored decision completion
  time is used only if the recorded execution time is absent.
- Market end is the structured `market_end_time` stored in the copy decision.
  The human-readable market title is not parsed for timing.
- Entry price is the open of the first candle timestamped at or after execution.
- End price is the open of the first candle timestamped at or after market end.
- A candle more than one detected interval late is excluded rather than silently
  approximated.

With 45-second data, either sampled price can be displaced from its target time
by up to approximately 45 seconds. The exact entry and end delays are retained
for every matched signal, so the lower timing precision remains visible in the
output. This is a reasonable tradeoff for the longer history, including 5-minute
markets, but results should be interpreted as 45-second-resolution evidence.

Classification:

```text
Up   succeeds when end price > entry price
Down succeeds when end price < entry price
Equal prices are ties
```

Every matched signal is evaluated in both mappings:

```text
follow_signal: Up -> long, Down -> short
invert_signal: Up -> short, Down -> long
```

`--round-trip-cost-bps` models the combined entry fee, exit fee, spread, and
slippage. One basis point is `0.01%`. The configured cost is subtracted from
the gross return of both mappings. By default it is derived from two market
fills at the configured `--taker-fee-percent`: `0.02% + 0.02% = 4 bps`.
Every report also includes
a sensitivity table at 0, 1, 2, 4, 6, and 10 basis points, plus the configured
value when it is different.

The defaults for the two dollar PnL columns are:

```text
margin = $100
leverage = 100x
notional = $10,000
order type = market
taker fee = 0.02% on entry and 0.02% on exit
slippage = 0
```

They can be changed with `--perp-margin-usd`, `--perp-leverage`, and
`--taker-fee-percent`. For quantity `notional / entry_price`, gross long PnL is
`quantity * (exit_price - entry_price)` and gross short PnL is
`quantity * (entry_price - exit_price)`. Net PnL subtracts the taker fee on both
the entry fill value and exit fill value.

The use of the next candle open avoids using the close of a candle that was
still forming when the signal became observable.

## Output

By default, each run creates a new directory under
`research/perp-signals/<timestamp>/` containing:

- `configuration.json`: reproducible rules and paths
- `imported_candles_summary.json`: coverage, cadence, duplicates, and gaps
- `signals.csv`: matched signals and price comparisons
- `excluded_signals.csv`: unmatched signals with explicit reasons
- `summary.json`: machine-readable aggregate results
- `report.md`: readable accuracy and duration summary

Only the overlap between the supplied TradingView files and bot history can be
evaluated. Older bot signals remain listed as exclusions when their candles are
not available.

`signals.csv` includes `pnl_if_pro` for trading in the bot signal's direction
and `pnl_if_against` for trading in the opposite direction. These are endpoint
PnL estimates. At 100x leverage, intratrade liquidation can occur long before
the market-end exit, and this analysis does not yet model maintenance margin,
liquidation, or funding.

The summary and report compare follow and invert using directional accuracy,
gross and net average/median returns, break-even round-trip cost, profitable
trades after costs, fixed-notional total returns, a sequential compounded path,
maximum drawdown, daily stability, market duration, and entry delay. They also
show a chronological split where the first 70% of matched signals are the
exploratory/training segment and the final 30% are a holdout segment.

Fixed-notional total return is the sum of per-trade returns assuming the same
notional for every signal. Sequential compounding assumes the entire evolving
account is applied to one trade after another. It is a diagnostic path, not a
portfolio simulation: signals may overlap, and leverage, margin, liquidation,
funding, and concurrent exposure are not modeled.
