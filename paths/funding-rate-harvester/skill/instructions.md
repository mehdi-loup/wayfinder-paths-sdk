# Funding Rate Harvester

Use this skill whenever the user wants to **harvest perp funding delta-neutrally** — "harvest funding", "basis trade", "delta-neutral yield on ETH/BTC/HYPE", "short the perp and hold spot", or "earn funding without price risk". It shorts Hyperliquid perps, hedges with a yield-bearing spot leg (Pendle PT / weETH / sUSDe / HL spot), optionally locks floating funding to a Boros fixed rate, and rotates pairs only when the uplift beats migration cost.

## Skill triggers

- "harvest funding rates" / "best funding to farm right now"
- "delta-neutral yield on <asset>"
- "basis trade ETH/BTC/HYPE"
- "short perp long spot" / "cash and carry"
- "lock in this funding rate" (Boros)
- "check / rebalance / unwind my funding harvester"

## Actions

Run everything with `python scripts/wf_run.py -- --action <action> [args]`.

- `discover --top 10`: read-only. Ranks (asset, spot-leg) combos by **net stacked carry** = funding EMA + spot-leg yield − fees − slippage (amortized). Shows each leg's economics and the best spot leg per asset. Cross-checks Delta Lab `screen/perp funding_now`.
- `quote --symbol ETH --size 1000`: full decomposition — funding APR (now + EMA), spot-leg yield, fees, slippage, net APR, break-even days. Includes the Boros lock quote when `rate_lock.enabled`.
- `deposit --symbol ETH --amount 1000 [--leg pendle_pt] [--gas 5] --confirm`: opens the pair **hedge first, then spot leg** (same-venue HL pairs fill atomically). Auto-bridges HL margin from Arbitrum USDC. Refuses below `min_net_carry_apr_bps`, above position caps, or under HL minimums ($5 deposit floor, $10 order notional).
- `update --confirm`: the core loop — safety rails first (stale-data freeze, liquidation guard: add margin then reduce, drawdown halt, leverage-cap recheck), then negative-carry exit after grace, breakeven-gated rotation, delta-band rebalance with 1h/2×-band churn guard, and the Boros lock decision. Without `--confirm` it evaluates and reports without broadcasting.
- `rotate [--force] --confirm`: evaluate rotation now; `--force` relaxes only the dwell (minimum-hold) gate — the breakeven and threshold gates always still apply.
- `lock --symbol ETH [--tenor 21] --confirm` / `unlock --symbol ETH --confirm`: manually open/unwind a Boros fixed-rate lock sized to the short leg.
- `status`: per pair — notional, current stacked carry, accrued funding + spot yield (estimates), lock PnL (separate line), MTM PnL, days held, delta ratio, liquidation distance, next rotation eval.
- `unwind [--symbol ETH] --confirm`: close one/all pairs — **spot first, hedge last** — and reconcile realized value vs entry.
- `exit --confirm`: after unwind, settles the strategy wallet's HL USDC to Arbitrum and, **only when a dedicated `strategy_wallet` is configured**, transfers the remaining USDC to the main wallet's address (resolved from the configured wallet, never from a command-line address). Without `--confirm` it returns a plan showing the withdrawal amount and destination and moves nothing; it refuses to run while any pair is still open.

## Safety rules (non-negotiable)

- **Never open without quoting.** `deposit` internally runs `quote` and includes it in the plan; present it to the user before confirming.
- **Hedge first on entry, hedge last on exit.** If the spot leg fails after the hedge opens (or a crash interrupts an open), the pair persists in state as `half_open`: `update` suspends carry actions for it and `unwind --symbol <SYM> --confirm` closes whatever filled. Never leave it unresolved.
- **Paper gate.** Live deposits refuse until `paper_gate_hours` (default 48h) of paper updates are recorded. The `--skip-paper-gate` flag exists only for an operator who has already rehearsed the flow elsewhere; do not pass it on the user's behalf without their explicit instruction.
- **Funding sign.** Positive funding = shorts receive (harvestable); negative = shorts pay. The scorer uses signed funding, so don't "fix" negative rates.
- **Confirmation gates.** Every fund-moving action (`deposit`, `update`, `rotate`, `lock`, `unlock`, `unwind`, `exit`) returns a `requires_confirmation` plan and broadcasts nothing unless `--confirm` is passed. Always present that plan and obtain the user's confirmation before re-running with `--confirm`.
- **Paper before live.** Set `mode: paper` in `inputs/config.yaml` for a rehearsal with live market data and virtual fills. Run at least 48h of paper updates before a live pilot.
- **Drawdown halts stick.** After a `max_drawdown_pct` halt, `update --confirm --resume` is required to continue — confirm with the user first.

## Wallet & data flow

- Only the wallet **address** is read for balances/positions; signing is delegated to the host / Wayfinder execution service. Private keys are never read or stored.
- Reads go through the Wayfinder RPC proxy, Hyperliquid public info API, Delta Lab (screening + weETH yield), and Pendle/Boros APIs. Position objects stay local; the only external fund-moving output is signed transactions/orders after the confirmation gate. Halt/stale alerts go through the Wayfinder notify service as human-readable summaries.
- Durable state (pair registry, funding EMAs, idempotency keys, paper balances) lives under the runner state dir via `monitor_state` — never `/tmp`.

## Steps

1. Inspect `inputs/config.yaml` and `inputs/universe.yaml` — confirm mode (paper/live), spot-leg priority, caps, and symbols match user intent. Leave `wallet` blank to use the session-connected wallet.
2. `discover` first, present the ranked table, and let the user pick (or accept the top combo).
3. `quote` the chosen symbol/size and show the decomposition + break-even days.
4. `deposit` without `--confirm` to produce the plan; after the user approves, re-run with `--confirm`.
5. Schedule updates: runner script jobs only run `.py` files inside `.wayfinder_runs/`, so create a thin wrapper `.wayfinder_runs/library/funding-rate-harvester/update.py` that imports the path entrypoint and calls `action_update(confirm=True)`, then `wayfinder runner add-job --type script --script-path .wayfinder_runs/library/funding-rate-harvester/update.py --interval 900`. Warn the user that scheduled runner jobs run unattended without an interactive confirmation step and are live fund-moving automation — the risk caps in `inputs/config.yaml` are the operative safeguard, so confirm those match intent before enabling a schedule.
6. Summarize positions, stacked carry, and any rails that fired after every action.
