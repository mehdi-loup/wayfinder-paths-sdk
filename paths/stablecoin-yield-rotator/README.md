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

## Wallet & data flow

What this path reads, where it computes, and what leaves your machine:

- **Wallet inputs.** The wallet is resolved from the session-connected wallet (or an explicit `wallet` in `inputs/config.yaml`). The path reads only the wallet **address** to look up on-chain balances and lending positions. Signing is delegated entirely to the host/execution service, which alone holds the keys; the path operates purely from the public address.
- **Reads (network).** On-chain balances, positions, APYs, TVL, and utilization are read through the rate-limited **Wayfinder RPC proxy** and the protocol adapters (Aave V3, Morpho, Euler V2, Hyperlend, Moonwell). Euler's stable vaults are discovered via a single **Delta Lab** `screen_lending` call. These are read-only queries.
- **Compute (local).** The **position and balance objects** returned by the adapters have exactly two destinations: (1) **local computation** — ranking, the rotation plan (deltas, gas, payback, constraint gates) in `scripts/rotation.py`, and `status` display; and (2) **host-bound Wayfinder execution paths** (the SDK / hosted Wayfinder execution service that signs and broadcasts on the configured wallet). The path ships no analytics or telemetry hooks, and every outbound request it makes targets a Wayfinder RPC/execution endpoint or a public chain RPC.
- **Writes (network).** The only outbound fund-moving traffic is **signed transactions broadcast to the relevant chains** (withdraw → bridge via BRAP → deposit), and only after the confirmation gate below. `auto-rotate` additionally sends an **email summary** of executed rotations / new failures via the Wayfinder notify service — a human-readable rotation summary only (asset, venue, USD amounts), not raw position objects or wallet credentials.
- **Applet.** The bundled applet (`applet/dist/index.html`) is a **static, read-only snapshot** — `bridge: []`, `externalOrigins: []`, no runtime fetch. It renders APY data baked into the path at build time and runs entirely offline in the browser: it takes no wallet connection and makes no network calls.

> **In short:** position objects stay on the host running the path — used locally for the decision, or handed to host-bound Wayfinder execution paths to sign/broadcast. Every data destination is either local computation or a Wayfinder host-bound endpoint.

### Confirmation safeguards (execution paths)

| Action | Gate before any broadcast |
|---|---|
| `scan`, `quote-rotation`, `status` | Read-only — no broadcast. |
| `deposit`, `withdraw` | Fund-moving. Via MCP they pass the Claude **safety-review hook** (human confirmation preview). |
| `update` | Plan-only by default (`status=requires_confirmation`, nothing broadcast). Broadcasts **only** with `--confirm`, executing leg-by-leg and **halting on the first revert**. |
| `auto-rotate` | Runner-only, runs `update --confirm` **without interactive confirmation** — the `inputs/config.yaml` constraints (APY delta, gas payback, TVL/utilization guards, diversification cap) are the only gate, and runner jobs do **not** pass the safety-review hook. Treat as live fund-moving automation. |

Before any fund-moving execution, the path re-checks live wallet positions and target venues (cached scan data is used only for ranking), verifies native gas on each chain in the path, and gates cross-chain bridges on `uplift_usd × payback_days > bridge_fee_usd × 2`.

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

## Limitations

- No borrow legs / leverage loops.
- No yield-bearing stable wrappers (sUSDe, sDAI rebases) — base stables only. USDe is supported as a plain lend asset; note it carries Ethena protocol risk on top of venue risk.
- SparkLend: read-only via this path. `SparkLendAdapter` exposes only borrow/repay (plus reads), no `lend`/`unlend`. Add `sparklend` back to `inputs/config.yaml` once the adapter exposes supply/withdraw — until then, rotations into/out of SparkLend are blocked at the dispatcher with `NotImplementedError`.
- Hyperlend: HyperEVM-only.
- Cross-chain rotation goes through BRAP and is gated more strictly than same-chain rotation.
