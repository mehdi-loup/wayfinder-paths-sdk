# Implementability rules

## Data we have

From `wayfinder_paths.core.backtesting`:

- `fetch_prices(symbols, start, end, interval='1h')` — hourly close prices per symbol
- `fetch_funding_rates(symbols, start, end, venue='hyperliquid')` — perp funding rates
- `fetch_lending_rates(symbol, start, end, venues=None)` — supply + borrow rates per venue

Retention: ~211 days (safe window). Coverage: majors (BTC, ETH, SOL, HYPE) and selected alts. No OHLC — close-only.

## Data we do NOT have (paper automatically non-implementable)

- Options market data (IV, skew, term structure, variance risk premium)
- L2 order book (bid-ask spread, depth, imbalance, queue position)
- Trade-level data (tick size effects, aggressor flow)
- Fundamental data (earnings, analyst estimates, macro releases)
- Sentiment data (news NLP scores, social media sentiment — except Alpha Lab's scored feed)
- Survivorship-bias-free historical universe
- Intraday below 1h
- Data older than ~7 months

## Cross-domain transfer rules

When evaluating whether a paper tested on domain X can be implemented for crypto hourly:

| Paper domain | Rule |
|---|---|
| US equities monthly | Signal must be computable at higher frequency. Monthly signals on hourly bars often have unit-transfer problems — check if formula is rescalable. |
| US equities daily | More transferable. Hourly rescale via `√(24)` for vol-based features. |
| FX daily | Reasonable transfer to crypto hourly — similar continuous-time market structure. |
| Commodities / fixed income | Poor transfer — different microstructure, different distributional properties. |
| Crypto daily | Direct transfer with hourly rescaling. |
| Crypto intraday | Direct transfer. |

## Signal reformulation rules

Some signals need reformulation:

- **"Market cap" signals** → no reliable crypto market-cap-by-hour. Use volume-weighting as proxy or skip.
- **"Book-to-market" and fundamental ratios** → no crypto analogue. Reject.
- **Industry / sector classifications** → no crypto analogue. Reject or collapse.
- **Risk-free rate in Sharpe** → use 0 (or stablecoin supply rate if available from `fetch_lending_rates("USDC", ...)`)

## Implementability check — quick decision tree

1. Does the signal require options data? → REJECT
2. Does it require L2 / order book? → REJECT
3. Does it require fundamentals? → REJECT
4. Is the paper's data horizon > weekly? → REJECT unless rescalable
5. Can you write it using only `(prices, funding, lending)`? → ACCEPT
6. Parameters all specified numerically? → ACCEPT
7. Parameters underspecified? → Mark UNDERSPECIFIED, skip
