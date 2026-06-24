# PRIME Daily Intel

Daily intelligence snapshot for the $PRIME token. Run one-shot or on a schedule.

## How to run

```bash
poetry run python examples/paths/prime-daily-intel/scripts/main.py
```

Optional config override:
```bash
poetry run python examples/paths/prime-daily-intel/scripts/main.py --config examples/paths/prime-daily-intel/inputs/config.yaml
```

## Output modules

| Key | Description |
|-----|-------------|
| `price_momentum` | Spot price, 7d/30d change, annualized vol from Delta Lab |
| `cross_chain` | Base vs Ethereum price spread derived from the 0.3% V3 pool; alerts when net spread > threshold |
| `uniswap_v3` | PRIME/WETH 0.3% and 1.0% pools on Ethereum: liquidity, tick, TVL from token balances |
| `aerodrome` | CL200-WETH/PRIME on Base: TVL, gauge status, fee APY from Delta Lab |
| `alpha_signals` | Top scored PRIME mentions from Alpha Lab in the last 24h |
| `onchain_pulse` | Large Transfer events (>50k PRIME) on Base and Ethereum in the last 24h |

## Key config (inputs/config.yaml)

- `thresholds.spread_alert_pct` — alert threshold for cross-chain spread (default 2%)
- `thresholds.large_transfer_prime` — minimum PRIME for on-chain pulse (default 50,000)
- `thresholds.alpha_min_score` — minimum Alpha Lab score to include (default 0.5)
- `uniswap_v3.pools` — V3 pool addresses to track (0.3% and 1.0% on Ethereum)

## To publish

1. `poetry run wayfinder path fmt --path examples/paths/prime-daily-intel`
2. `poetry run wayfinder path doctor --check --path examples/paths/prime-daily-intel`
3. `poetry run wayfinder path publish --path examples/paths/prime-daily-intel`
