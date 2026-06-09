# Stablecoin Yield Rotator

Rotate stablecoin (USDC/USDT/DAI) deposits across Aave V3, Morpho Blue markets, Morpho vaults, Euler V2, and Hyperlend to chase the best risk-adjusted net APY, with gas-amortized hysteresis so you don't churn.

## Actions

`scan`, `quote-rotation`, `deposit`, `update`, `status`, `withdraw`, `gorlami-scenario`.

| Action | Args | Notes |
|---|---|---|
| `scan` | — | Read-only. Ranked APY table for all (asset, venue, chain) tuples. |
| `quote-rotation` | — | Read-only. Proposed deltas vs current positions; expected uplift, gas, payback days. |
| `deposit` | `--amount <float> --asset <USDC\|USDT\|DAI>` | Initial deposit into the top-ranked venue for that asset. |
| `update` | `--confirm` | Re-quote + gas-check + execute. Without `--confirm`, emits the plan only (no broadcast). With `--confirm`, executes leg-by-leg, depositing the actual post-bridge balance delta on cross-chain legs. Halts on first revert. |
| `status` | — | Positions across all venues + USD totals + blended APY. |
| `withdraw` | `--amount <float>?` | Full or partial liquidate to stablecoin in wallet. |
| `gorlami-scenario` | `--amount <float>?` | Creates a Gorlami Base fork, seeds wallet ETH + USDC, then runs scan → deposit → status → withdraw → status against Aave V3. Defaults to 10 USDC. |

## What's inside

- `wfpath.yaml` — path manifest
- `inputs/config.yaml` — wallet, chains, assets, venues, rotation constraints
- `scripts/main.py` — CLI dispatcher
- `scripts/venues.py` — per-venue read/write wiring (Aave V3, Morpho Blue markets, Morpho vaults, Euler V2, Hyperlend; SparkLend read-only when explicitly configured)
- `scripts/rotation.py` — quote-rotation constraint engine
- `skill/instructions.md` — canonical skill instructions

## Safety

- Quote before rotating (`quote-rotation` then `update`).
- Run `gorlami-scenario` before live fund-moving changes when validating this path.
- Halt on revert mid-rotation.
- Skip target venues with utilization > 95% or supply cap headroom < 5% of position size.
- Cross-chain bridges only when `uplift_usd × payback_days > bridge_fee_usd × 2`.
- Gas balance check on every chain in the rotation path.
- Scan data is cached for 15 minutes; wallet positions are always refreshed before quote/update, and target venues are re-checked live before fund-moving execution.

## Limitations (v0.1)

- No borrow legs / leverage loops.
- No yield-bearing stable wrappers (sUSDe, sDAI rebases) — base stables only.
- No auto-rotation on a runner schedule — `update` runs on demand.
- SparkLend: read-only via this path. `SparkLendAdapter` exposes only borrow/repay (plus reads), no `lend`/`unlend`. Add `sparklend` back to `inputs/config.yaml` once the adapter exposes supply/withdraw — until then, rotations into/out of SparkLend are blocked at the dispatcher with `NotImplementedError`.
- Hyperlend: HyperEVM-only.
- Cross-chain rotation goes through BRAP and is gated more strictly than same-chain rotation.
