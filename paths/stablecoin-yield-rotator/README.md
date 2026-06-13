# Stablecoin Yield Rotator

Rotate stablecoin (USDC/USDT/DAI/USDS/USDe/GHO) deposits across Aave V3, Morpho Blue markets, Morpho vaults, Euler V2, Hyperlend, and Moonwell — on Ethereum, Polygon, Base, Arbitrum, and HyperEVM — to chase the best risk-adjusted net APY, with gas-amortized hysteresis so you don't churn.

## Actions

`scan`, `quote-rotation`, `deposit`, `update`, `auto-rotate`, `status`, `withdraw`, `gorlami-scenario`.

| Action | Args | Notes |
|---|---|---|
| `scan` | — | Read-only. Ranked APY table for all (asset, venue, chain) tuples. |
| `quote-rotation` | — | Read-only. Proposed deltas vs current positions; expected uplift, gas, payback days. |
| `deposit` | `--amount <float> --asset <USDC\|USDT\|DAI\|USDS\|USDE\|GHO>` | Initial deposit into the top-ranked venue for that asset. |
| `update` | `--confirm` | Re-quote + gas-check + execute. Without `--confirm`, emits the plan only (no broadcast). With `--confirm`, executes leg-by-leg, depositing the actual post-bridge balance delta on cross-chain legs. Halts on first revert. Idle wallet balances of configured stables (≥ 1 unit) are planned as 0%-APY deposit legs, so a fresh wallet bootstraps without a manual `deposit`. |
| `auto-rotate` | — | Unattended `update --confirm` for runner scheduling. Emails a summary on executed rotations and on new failures (repeated identical halts alert once). No-ops are silent. |
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
- Gas-starved **destination** chains get a planned top-up leg: a small slice of the rotating stable is bridged into native gas first, and the top-up's full cost is added to the rotation's cost in the payback/max-gas gates — a rotation that can't amortize its own gas funding is skipped. Gas-starved **source** chains can't be fixed automatically (no gas to sign with) and are surfaced as skipped legs with a fund-gas reason.
- Scan data is cached for `scan_cache_ttl_seconds` (default 21600 = 6h); wallet positions are always refreshed before quote/update, and target venues are re-checked live before fund-moving execution. Lower the TTL for a fresher ranking.
- All reads go through the rate-limited Wayfinder RPC proxy. To avoid 429s, the plan build bounds concurrent reads (`rpc_concurrency`, default 4) with one shared limiter across scan + positions + balance reads, and retries transient 429/rate-limit errors with backoff. Lower `rpc_concurrency` if you still hit limits.
- **Request efficiency:** Euler is permissionless (hundreds of vaults/chain), so enumerating it on-chain cost ~2,500 calls/quote. Instead the stable Euler vaults are discovered via one Delta Lab `screen_lending` call (its `market_external_id` is the eVault address) and only those are read on-chain; Euler positions read account state directly. Morpho-vault holdings and idle/gas balances are batched per chain via Multicall3. A cold full-config quote went from ~750 reads to ~230.

## Scheduled auto-rotation

Run rotations on a schedule with the project-local runner. Runner **script jobs only run
`.py` files inside `.wayfinder_runs/`**, so add a tiny wrapper there that calls the
`auto-rotate` action (this also avoids passing CLI args through the runner):

```bash
mkdir -p .wayfinder_runs/library/stablecoin-yield-rotator
cat > .wayfinder_runs/library/stablecoin-yield-rotator/auto_rotate.py <<'PY'
import asyncio, sys
from pathlib import Path
SCRIPTS = Path(__file__).resolve().parents[3] / "paths" / "stablecoin-yield-rotator" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import main as rotator
rotator.emit(asyncio.run(rotator.action_auto_rotate(rotator.load_yaml("config.yaml"))))
PY

poetry run wayfinder runner start
poetry run wayfinder runner add-job \
  --name stable-rotator-auto \
  --type script \
  --script-path .wayfinder_runs/library/stablecoin-yield-rotator/auto_rotate.py \
  --cron "0 9 * * *" --timezone America/Toronto   # daily at 09:00
```

`auto-rotate` executes the rotation plan **without interactive confirmation** — the
constraints in `inputs/config.yaml` (APY delta, gas payback, TVL/utilization guards,
diversification cap) are the only gate, and runner executions do not go through the
Claude safety review hook. Treat the schedule as live fund-moving automation and size
constraints accordingly. Outcome notifications are emailed via the Wayfinder notify
service; dedupe state lives in `./.wayfinder/runner/job_state/`.

## Limitations (v0.2)

- No borrow legs / leverage loops.
- No yield-bearing stable wrappers (sUSDe, sDAI rebases) — base stables only. USDe is supported as a plain lend asset; note it carries Ethena protocol risk on top of venue risk.
- SparkLend: read-only via this path. `SparkLendAdapter` exposes only borrow/repay (plus reads), no `lend`/`unlend`. Add `sparklend` back to `inputs/config.yaml` once the adapter exposes supply/withdraw — until then, rotations into/out of SparkLend are blocked at the dispatcher with `NotImplementedError`.
- Hyperlend: HyperEVM-only.
- Cross-chain rotation goes through BRAP and is gated more strictly than same-chain rotation.
