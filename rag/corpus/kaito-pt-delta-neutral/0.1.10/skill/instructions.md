# KAITO PT Delta-Neutral Carry

Use this skill to inspect, backtest, and monitor the KAITO PT delta-neutral carry strategy.

## Strategy mechanics

- **Long leg**: Buy Pendle PT-sKAITO on Base. The PT trades at a discount to par and accrues a fixed yield (currently ~29% APR implied APY). At maturity (2026-07-30), each PT redeems for 1 sKAITO.
- **Short leg**: Short KAITO perp on Hyperliquid (1-2x leverage). This hedges directional KAITO price exposure.
- **Net carry**: PT fixed yield minus funding costs on the short. When funding is positive, shorts receive additional income.

### Hedge ratio

PT delta to underlying is approximately `pt_price / underlying_price`. Near maturity, delta approaches 1.0. Far from maturity with high yield, delta is less than 1.0 (PT trades at a deeper discount). The short size should be calibrated to match PT delta exposure.

### Funding sign convention

- **Positive funding** = longs pay shorts = good for our short (we receive)
- **Negative funding** = shorts pay longs = bad for our short (we pay)

Over the past 60 days, KAITO funding has averaged **-8.4% annualized** (shorts pay), meaning the short leg is a net cost. The PT yield (~29%) more than compensates.

## Running the backtest

```bash
# Default: 60-day lookback, $100K notional, 1x leverage
poetry run python scripts/pt_delta_neutral_backtest.py \
  --symbol KAITO \
  --lookback-days 60 \
  --out applet/dist/data/backtest_results.json

# With higher leverage on the short
poetry run python scripts/pt_delta_neutral_backtest.py \
  --symbol KAITO \
  --lookback-days 60 \
  --leverage 1.5 \
  --out applet/dist/data/backtest_results.json
```

### Parameters

| Flag | Default | Description |
|------|---------|-------------|
| `--symbol` | KAITO | Basis symbol |
| `--lookback-days` | 60 | Historical window |
| `--notional-usd` | 100000 | Starting capital |
| `--leverage` | 1.0 | Short leg leverage (1-2x) |
| `--market-address` | 0xb0eb...1fac | Pendle market address (Base) |
| `--chain-id` | 8453 | Chain for Pendle market |
| `--out` | stdout | Output JSON file path |

## Data sources

- **Price + funding**: Delta Lab (`DeltaLabClient.get_asset_timeseries(symbol="KAITO", series="price,funding")`)
- **PT history**: Pendle API via `PendleAdapter.fetch_market_history(chain_id=8453, market_address="0xb0eb82ba25ffa51641d8613d270ad79183171fac")`
- **Current rates**: `wayfinder://delta-lab/KAITO/apy-sources/7/10`

## Applet

The dashboard shows:
1. **Summary cards** - Total return, Sharpe, max drawdown, carry breakdown
2. **NAV chart** - Portfolio value with PT leg and short leg overlays
3. **Carry decomposition** - Cumulative PT yield vs cumulative funding P&L
4. **Rate panels** - PT implied APY and Hyperliquid funding rate over time
5. **Price chart** - KAITO spot price and PT/underlying ratio

## Key risks

- **Funding rate reversal**: If funding goes deeply negative, the short leg becomes expensive.
- **PT delta drift**: Hedge ratio shifts with time-to-maturity and implied APY changes. Rebalance periodically.
- **Implied APY compression**: Lower APY = higher PT price (mark-to-market gain) but worse re-entry yield.
- **Liquidity**: PT-sKAITO TVL ~$183K. Size positions accordingly.
- **Smart contract risk**: Pendle PT contracts and sKAITO staking.

## Signals

Five signal types for live monitoring:

1. **Net carry 5% increment** - Fires when net carry APR (PT implied APY + annualized funding) crosses any 5% boundary (10%, 15%, 20%, 25%, 30%, 35%, etc.) in either direction. Track the last emitted band to avoid duplicate signals.
   ```bash
   # Example: net carry rose above 30%
   poetry run wayfinder pack signal emit --slug kaito-pt-delta-neutral --version 0.1.0 \
     --title "Net carry crossed above 30% APR" \
     --message "PT implied APY 29.4% + funding 1.2% = 30.6% net carry" \
     --level info --metric net_carry_apr=0.306 --metric band=0.30

   # Example: net carry dropped below 20%
   poetry run wayfinder pack signal emit --slug kaito-pt-delta-neutral --version 0.1.0 \
     --title "Net carry dropped below 20% APR" \
     --message "PT implied APY 25.1% + funding -6.3% = 18.8% net carry" \
     --level warning --metric net_carry_apr=0.188 --metric band=0.20
   ```

2. **Market expiry** - Fires when PT-sKAITO approaches maturity. Emit at 30 days, 7 days, and 1 day before expiry (2026-07-30). At expiry, PT redeems for 1 sKAITO — the short hedge should be closed and the PT redeemed.
   ```bash
   # 30 days before expiry
   poetry run wayfinder pack signal emit --slug kaito-pt-delta-neutral --version 0.1.0 \
     --title "PT-sKAITO maturity in 30 days" \
     --message "Market 0xb0eb...1fac expires 2026-07-30. Plan exit or roll to a new PT." \
     --level info --metric days_to_maturity=30

   # 7 days before expiry
   poetry run wayfinder pack signal emit --slug kaito-pt-delta-neutral --version 0.1.0 \
     --title "PT-sKAITO maturity in 7 days" \
     --message "Close short hedge and prepare PT redemption, or roll into a new maturity." \
     --level warning --metric days_to_maturity=7

   # 1 day before expiry
   poetry run wayfinder pack signal emit --slug kaito-pt-delta-neutral --version 0.1.0 \
     --title "PT-sKAITO expires tomorrow" \
     --message "Immediate action required: close short, redeem PT." \
     --level error --metric days_to_maturity=1
   ```

3. **Funding flip warning** - Fires when KAITO funding rate turns negative (shorts start paying).
   ```bash
   poetry run wayfinder pack signal emit --slug kaito-pt-delta-neutral --version 0.1.0 \
     --title "KAITO funding turned negative" \
     --message "Funding rate -0.8% annualized" \
     --level warning --metric funding_rate_ann=-0.008
   ```

4. **Delta drift alert** - Fires when hedge ratio deviates significantly from target.
   ```bash
   poetry run wayfinder pack signal emit --slug kaito-pt-delta-neutral --version 0.1.0 \
     --title "Delta drift: rebalance needed" \
     --message "PT delta shifted from 0.85 to 0.78" \
     --level warning --metric current_delta=0.78
   ```

5. **Funding flip recovery** - Fires when funding returns to positive after a negative period.
   ```bash
   poetry run wayfinder pack signal emit --slug kaito-pt-delta-neutral --version 0.1.0 \
     --title "KAITO funding turned positive" \
     --message "Funding rate +2.1% annualized — shorts now receiving" \
     --level info --metric funding_rate_ann=0.021
   ```

6. **Health check (twice daily)** - Scheduled at ~00:00 and ~12:00 UTC. Emits a snapshot of current strategy state: net carry APR, PT implied APY, funding rate, hedge ratio, days to maturity, and KAITO spot price. Always fires regardless of whether thresholds have been crossed — provides a heartbeat that confirms the monitoring pipeline is alive and gives a quick status glance.
   ```bash
   poetry run wayfinder pack signal emit --slug kaito-pt-delta-neutral --version 0.1.0 \
     --title "Health check: net carry 21.3% APR" \
     --message "PT APY 29.4% | Funding -8.1% ann | Hedge 0.83 | 114d to maturity | KAITO $0.42" \
     --level info \
     --metric net_carry_apr=0.213 \
     --metric implied_apy=0.294 \
     --metric funding_rate_ann=-0.081 \
     --metric hedge_ratio=0.83 \
     --metric days_to_maturity=114 \
     --metric kaito_price_usd=0.42
   ```

## Scheduled data refresh

The applet loads a static `backtest_results.json`. To keep it current, set up a runner job that re-runs the backtest every 12 hours (aligned with the health check signals):

```bash
# Ensure the runner daemon is running
poetry run wayfinder runner ensure

# Add a 12-hour refresh job
poetry run wayfinder runner add-job \
  --name kaito-pt-refresh \
  --type script \
  --script examples/paths/kaito-pt-delta-neutral/scripts/pt_delta_neutral_backtest.py \
  --args "--symbol KAITO --lookback-days 60 --out examples/paths/kaito-pt-delta-neutral/applet/dist/data/backtest_results.json" \
  --interval 43200 \
  --config ./config.json
```

This re-generates the applet data with the latest 60-day window twice daily. After each run, republish the path to push the updated JSON to the host:

```bash
poetry run wayfinder path build --path examples/paths/kaito-pt-delta-neutral --out dist/bundle.zip
```

## Validation

```bash
poetry run wayfinder path fmt --path examples/paths/kaito-pt-delta-neutral
poetry run wayfinder path doctor --check --path examples/paths/kaito-pt-delta-neutral
```
