# Concentrated LP Manager

Open, monitor, rebalance, compound, and close concentrated-liquidity (CL) positions on
**Uniswap V3** (Ethereum, Arbitrum, Base) and **Aerodrome Slipstream** (Base) with
cooldown + daily-cap safety rails and ledger writes for IL/fee accounting.

## Actions

| Action | Args | Behavior |
|---|---|---|
| `scan` | `--venue?`, `--pair?`, `--chain?` | Read-only. Lists candidate pools sorted by realized fee APR. |
| `quote-open` | `--pool`, `--size` | Computes range bounds, optimal token split, expected fee APR, IL at ±10/25/50%. |
| `open` | `--pool`, `--size?` | Mints LP NFT at the computed range. Uses the wallet's existing balances of the pair. |
| `status` | `--pool?` | Per-position: in/out of range, fees earned, IL vs HODL, gauge rewards. |
| `rebalance` | `--pool` | Burn → re-mint at new center. Halts on cooldown / daily cap. |
| `compound` | `--pool?` | Collect fees and add to position. Skips when below `compound_threshold_usd`. |
| `close` | `--pool` | Decrease 100% liquidity, collect fees, burn NFT. |
| `attach` | — | Register `monitor.py` as a wayfinder runner job. |
| `detach` | — | Remove the runner job. |

Run any action with:

```bash
poetry run python scripts/main.py --action <name> [args...]
```

## What's inside

- `wfpath.yaml` — path manifest
- `inputs/config.yaml` — wallet, gas reserve, default strategy, monitor interval
- `inputs/pools.yaml` — list of pools to manage
- `scripts/main.py` — controller (all 9 actions)
- `scripts/monitor.py` — background poller (alert-only; never auto-executes)
- `scripts/attach.py` — installs monitor as a runner job
- `skill/instructions.md` — canonical skill instructions

## Safety

- **Always `quote-open` before `open`.** Confirm range, swap need, and IL before minting.
- **Out-of-range alerts come first** in `status` output — out-of-range LPs earn no fees.
- **Rebalance cooldown is a hard gate.** Re-triggers within `rebalance_cooldown_minutes`
  return `skipped: cooldown` even when manually invoked.
- **Daily cap.** No more than `max_rebalances_per_day` rebalances per pool per UTC day.
- **MEV awareness.** Rebalance swaps use venue-native swap surface, not BRAP.
- **Ledger every leg.** Each rebalance writes 3 ledger rows (decrease, swap, mint) — needed for IL/fee accounting.
- **The monitor never executes.** It surfaces band-exit candidates via MCP notify only.

## Build & publish

```bash
wayfinder path fmt --path .
wayfinder path doctor --path .
wayfinder path build --path . --out dist/bundle.zip
wayfinder path publish --path .
```

## Out of scope (v0.1)

- Hedged LP (delta-neutral via perp short)
- Just-in-time / single-tick LP
- Auto-execute rebalance from the monitor
- Multi-NFT split positions in the same pool
- Pre-swap funding via BRAP (manually balance the wallet for now)
- ProjectX (HyperEVM) — see `wayfinder_paths/strategies/projectx_thbill_usdc_strategy/`
