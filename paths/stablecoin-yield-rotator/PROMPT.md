# Path: stablecoin-yield-rotator

**Slug:** `stablecoin-yield-rotator`
**Primary kind:** strategy
**Risk tier:** execution
**One-liner:** Rotate stablecoin (USDC/USDT/DAI/USDS/USDe/GHO) deposits across Aave V3, Morpho Blue markets, Morpho vaults, Euler V2, Hyperlend, and Moonwell to chase the best risk-adjusted net APY, with gas-amortized hysteresis so you don't churn.

---

## Market gap

Every existing single-venue stable-yield path locks you into one protocol's curve. Net APYs across Aave / Morpho / Spark / Euler / Hyperlend diverge by 100–400 bps regularly, but no path currently watches them all and rotates. The WIP `stablecoin_yield_strategy` is a scaffold; productize it as a path with full multi-venue coverage and execution.

## Reference reading (load before implementing)

Skills:
- `/using-aave-v3-adapter`
- `/using-morpho_blue_market-adapter`
- `/using-sparklend-adapter`
- `/using-euler-v2-adapter`
- `/using-hyperlend-adapter`
- `/using-brap-adapter`
- `/using-delta-lab` (for `screen/lending` cross-venue snapshot)
- `/using-pool-token-balance-data`
- `/developing-wayfinder-paths` (path scaffolding + publish flow)

Existing code to study:
- `wayfinder_paths/strategies/hyperlend_stable_yield_strategy/` — single-venue baseline; mirror its deposit/update/status/withdraw shape
- `wayfinder_paths/strategies/stablecoin_yield_strategy/` — WIP discovery skeleton
- `wayfinder_paths/strategies/moonwell_wsteth_loop_strategy/` — borrow/repay reference (we don't borrow here, but adapter patterns are similar)

## Component plan

Single `main` script. Actions are CLI flags. No background monitor in v0.1; rotation runs on demand or via the wayfinder runner on a schedule.

```
paths/stablecoin-yield-rotator/
├── wfpath.yaml
├── README.md
├── inputs/
│   └── config.yaml
├── skill/
│   └── instructions.md
└── scripts/
    └── main.py
```

## Config slots (`inputs/config.yaml`)

```yaml
wallet: main
chains: [1, 137, 8453, 42161, 999]       # ethereum, polygon, base, arbitrum, hyperevm
assets: [USDC, USDT, DAI, USDS, USDE, GHO]
venues: [aave_v3, morpho_blue_market, morpho_vault, euler_v2, hyperlend]
constraints:
  min_apy_delta_bps: 50                   # don't rotate for <50bps improvement
  gas_amortization_days: 30               # rotation must pay back gas within this window
  max_gas_usd_per_rotation: 25
  max_position_pct_per_venue: 50          # diversification cap
  min_scan_tvl_usd: 100000                # exclude small/illiquid targets
  max_scan_apy: 0.5                       # stablecoin APY sanity cap
  blocklist_markets: []                   # optional safety overrides
slippage_bps: 30
```

## Action surface

| Action | Args | Behavior |
|---|---|---|
| `scan` | — | Read-only. Pulls supply APYs for all (asset, venue, chain) tuples. Returns ranked table with utilization, supply cap headroom, net APY. |
| `quote-rotation` | — | Computes proposed rotation deltas given current positions vs scan output. Shows expected net APY uplift, gas estimate, payback days. Read-only. |
| `deposit` | `--amount`, `--asset` | Initial deposit into top-ranked venue. |
| `update` | — | Execute rotation if quote passes constraints. Multi-step: withdraw old → bridge if cross-chain (BRAP) → deposit new. Halt on first revert. |
| `status` | — | Aggregated positions across all venues + USD totals + blended APY. |
| `withdraw` | `--amount?` | Liquidate to stablecoin in wallet (full or partial). |

## Skill triggers (`skill/instructions.md`)

- "rotate my stables to the best yield"
- "where's the best USDC yield right now"
- "scan stablecoin lending rates"
- "deposit X USDC into the best stable yield"
- "withdraw from stable rotator"

## Safety rules

- **Never rotate without quoting first.** Show user the table (current → proposed, APY delta, gas, payback days) and get confirmation, unless user said "just rotate".
- **Halt on revert.** If a withdraw or bridge step fails, surface the error and stop — do not continue depositing into the new venue with phantom funds.
- **Utilization spike guard.** If target venue's utilization is >95% or supply cap headroom <5% of position size, skip and use second-best.
- **Gas check first.** Before any rotation step, verify gas-token balance on each chain in the path.
- **Cross-chain bridge only when net APY uplift × position × payback_days > bridge_fee × 2.** Same-chain rotations are preferred.

## Acceptance criteria

1. `scan` returns a ranked table for all configured (asset, venue, chain) tuples with no missing rows; APYs match Delta Lab `screen/lending` to within 5 bps.
2. `quote-rotation` produces a deterministic plan from cached scan data plus live wallet positions.
3. `update` successfully rotates a test position cross-venue (e.g., Aave Base → Morpho Base) on a Gorlami fork.
4. `update` successfully rotates cross-chain (e.g., Aave Arbitrum → Hyperlend HyperEVM) on dual-fork simulation; bridge gas + token seeded on destination, deposit completes.
5. `status` shows blended portfolio APY equal to weighted-average of position APYs.
6. Smoke test exercises scan → deposit → update → withdraw on a single chain.
7. `wayfinder path doctor --path .` passes; `wayfinder path fmt --path .` is clean.

## Build & publish

```
wayfinder path fmt --path .
wayfinder path doctor --path .
wayfinder path eval --path .            # if fixtures added
wayfinder path build --path .
wayfinder path publish --path .          # only after manual review on dev
```

## Out of scope (v0.1)

- Borrow legs / leverage loops (separate path).
- Yield-bearing stable wrappers (sUSDe, sDAI rebases) — keep base stables only.
- Liquidation risk monitoring (no borrowing in v0.1, so N/A).
- Auto-rotation on a runner schedule — ship manual `update` first; runner integration is a follow-up.
