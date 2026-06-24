# Concentrated LP Manager

Use this skill when the user wants to discover, open, monitor, rebalance, compound, or close concentrated-liquidity (CL) positions on Uniswap V3 (Ethereum, Arbitrum, Base) or Aerodrome Slipstream (Base).

## Triggers

- "open a uniswap LP position on <PAIR>"
- "find the best LP pools for <PAIR>"
- "rebalance my LP positions"
- "compound LP fees"
- "what's my LP performance"
- "close my LP on <POOL>"
- "monitor my LPs"

## Actions

- `scan`: read-only ranked pool table by realized 7d fee APR. Filters: `--venue`, `--pair`, `--chain`.
- `quote-open`: read-only economics for `--pool --size`. Returns range bounds, optimal token split, expected fee APR, and IL at ±10/25/50% price moves.
- `open`: mints a new LP NFT at the computed range. Uses the wallet's existing balances of the pair (no auto-swap in v0.1).
- `status`: per-position snapshot — current tick vs range, time-in-range, fees earned, IL vs HODL, gauge rewards.
- `rebalance`: burn the existing position and re-mint at a new center. Hard-gated by `rebalance_cooldown_minutes` and `max_rebalances_per_day`.
- `compound`: collect fees and add to position; skips when accrued fees are below `compound_threshold_usd`.
- `close`: decrease 100% of liquidity, collect fees, burn NFT.
- `attach` / `detach`: install or remove `monitor.py` as a wayfinder runner job.

## Safety

- Never `open` without first running `quote-open` and showing the user the range, optimal token split, fee APR, and IL at ±10/25/50% — unless the user explicitly said "just open".
- When `status` shows a position out of range, lead with that — out-of-range LPs earn zero fees and accrue IL.
- `rebalance` is a hard gate on `rebalance_cooldown_minutes` and `max_rebalances_per_day`. If skipped, surface the cooldown timer / cap state and ask whether to wait or override.
- `compound` skips when accrued fees are under `compound_threshold_usd` — surface the threshold rather than spending gas on dust.
- A rebalance is multiple transactions (decrease, swap, mint). If estimated total slippage exceeds 0.5% of position, surface and confirm.
- The monitor (`monitor.py`) is alert-only. It never executes a rebalance. Surface its candidates and let the user (or the controller on next `update`) act.
- Ledger writes record every leg (decrease + swap + mint as 3 rows). Don't collapse them.

## Workflow examples

The exported skill ships with a runtime launcher at `scripts/wf_run.py`. Invoke it with the controller args you need:

```bash
# Discover candidates
python scripts/wf_run.py -- --action scan --pair ETH/USDC --chain 8453

# Quote a $1000 open on a specific pool
python scripts/wf_run.py -- --action quote-open --pool 0x... --size 1000

# Open the position (after the user confirms the quote)
python scripts/wf_run.py -- --action open --pool 0x... --size 1000

# Snapshot all open positions
python scripts/wf_run.py -- --action status

# Rebalance one pool
python scripts/wf_run.py -- --action rebalance --pool 0x...

# Install background monitor
python scripts/wf_run.py -- --action attach
```

## Configuration

`inputs/config.yaml` declares the wallet label, gas reserve, slippage default, the
default range strategy (`static_pct` / `atr_band` / `vol_scaled`), rebalance cooldown,
daily cap, fee-compound toggle, monitor poll interval, and ledger toggle. Per-pool
overrides live in `inputs/pools.yaml` under `positions[].strategy`.

## Publishing

1. From the source repo, run `wayfinder path fmt --path .` and `wayfinder path doctor --path .`.
2. Set `WAYFINDER_PATHS_API_URL` to the target Strategies backend and `WAYFINDER_API_KEY`.
3. Run `wayfinder path publish --path .`. Add `--bonded --owner-wallet 0x... --risk-tier execution` for a bonded publish.
4. Report the returned `manageUrl`, `reviewState`, `publishState`, and `nextAction`.
