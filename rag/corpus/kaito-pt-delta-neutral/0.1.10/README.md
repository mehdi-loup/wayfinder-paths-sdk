# KAITO PT Delta-Neutral Carry

Delta-neutral carry strategy combining Pendle PT-sKAITO fixed yield with a Hyperliquid KAITO perpetual short hedge.

## Strategy

- **Long leg**: Buy PT-sKAITO on Pendle (Base). The PT trades at a discount to par and accrues a fixed yield (~29% APR) as it converges to 1 sKAITO at maturity (2026-07-30).
- **Short leg**: Short KAITO perp on Hyperliquid (1-2x leverage). Hedges directional KAITO exposure. Funding rate income/cost is variable.
- **Net carry**: PT fixed yield minus any funding costs paid on the short.

## Backtest

```bash
# Default: 60-day lookback, $100K notional, 1x leverage
poetry run python scripts/pt_delta_neutral_backtest.py \
  --symbol KAITO \
  --lookback-days 60 \
  --out applet/dist/data/backtest_results.json

# With 1.5x leverage on the short
poetry run python scripts/pt_delta_neutral_backtest.py \
  --symbol KAITO \
  --lookback-days 60 \
  --leverage 1.5 \
  --out applet/dist/data/backtest_results.json
```

## Key risks

- **Funding rate reversal**: If KAITO funding goes deeply negative, shorts pay longs, eroding carry.
- **PT delta drift**: PT delta to underlying is not exactly 1.0; hedge ratio needs periodic rebalancing.
- **Implied APY compression**: If PT implied APY drops, mark-to-market rises but re-entry yields worsen.
- **Liquidity**: PT-sKAITO TVL is ~$183K — large positions face slippage.
- **Smart contract risk**: Pendle PT and sKAITO staking contracts.

## Path validation

```bash
poetry run wayfinder path fmt --path examples/paths/kaito-pt-delta-neutral
poetry run wayfinder path doctor --check --path examples/paths/kaito-pt-delta-neutral
```
