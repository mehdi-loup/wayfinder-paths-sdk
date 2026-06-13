# Path: concentrated-lp-manager

**Slug:** `concentrated-lp-manager`
**Primary kind:** strategy
**Risk tier:** execution
**One-liner:** Manage concentrated-liquidity LP positions across Uniswap V3, Aerodrome Slipstream, and ProjectX (HyperEVM V3 fork) — open ranges intelligently, monitor for band exits, rebalance, and compound fees.

---

## Market gap

Three V3-style adapters (Uniswap, Aerodrome Slipstream, ProjectX) ship with the SDK and are completely unused by any installed path. CLM is the highest-skill DeFi action — getting range selection, rebalance triggers, and IL accounting right is genuinely hard. That difficulty is why nobody has shipped it; it's also why a well-built version has a moat.

## Reference reading (load before implementing)

Skills:
- `/using-uniswap-adapter` — V3 LP NFT lifecycle, fee collection, range math
- `/using-aerodrome-slipstream-adapter` — deployment-aware reads, gauge rewards
- `/using-projectx-adapter` — HyperEVM V3 fork: pool reads, positions, swap, lp.write
- `/using-pool-token-balance-data` — pool discovery + price reads
- `/using-brap-adapter` — to balance the LP pair via swaps
- `/developing-wayfinder-paths`
- `/simulation-dry-run` — test rebalance flows on Gorlami before live

Existing code to study:
- `wayfinder_paths/strategies/projectx_thbill_usdc_strategy/` — single-pool WIP; mirror its lifecycle, then generalize
- `wayfinder_paths/adapters/uniswap_adapter/`, `aerodrome_slipstream_adapter/`, `projectx_adapter/` — all three expose `lp.write` capability; harmonize their interfaces
- `trailing-hl-orders` registry path — for the controller/monitor/attach pattern (multi-component path)

## Component plan

```
paths/concentrated-lp-manager/
├── wfpath.yaml
├── README.md
├── inputs/
│   ├── config.yaml
│   └── pools.yaml
├── skill/
│   └── instructions.md
└── scripts/
    ├── main.py            # controller: open/close/rebalance/compound/status
    ├── monitor.py         # background poller: detect band exits, post snapshots
    └── attach.py          # one-shot bootstrapper: install monitor as runner job
```

Three components, mirroring the `trailing-hl-orders` shape. The monitor does NOT execute rebalances autonomously — it surfaces *candidates* through the safety-review pipe, and the user (or the controller on next `update`) acts.

## Config slots

`inputs/config.yaml`:
```yaml
wallet: main
gas_reserve_native_eth: 0.005             # don't drain gas across rebalances
slippage_bps: 30
default_strategy:
  range_strategy: vol_scaled              # static_pct | atr_band | vol_scaled
  range_width_atr: 1.5                    # used by atr_band / vol_scaled
  rebalance_threshold_pct: 5              # rebalance when price exits band by this %
  rebalance_cooldown_minutes: 60
  max_rebalances_per_day: 4
  fee_compound: true
  compound_threshold_usd: 10              # don't compound below this
monitor:
  poll_interval_seconds: 300
  max_runtime_hours: 168
ledger_record: true
```

`inputs/pools.yaml`:
```yaml
positions:
  - pool: 0x...                           # pool address
    venue: uniswap_v3
    chain: 8453
    pair: [USDC, ETH]
    target_usd: 1000
    strategy:                             # overrides default_strategy
      range_strategy: static_pct
      range_width_pct: 8
  - pool: 0x...
    venue: aerodrome_slipstream
    chain: 8453
    pair: [USDC, AERO]
    target_usd: 500
    # uses default_strategy
  - pool: 0x...
    venue: projectx
    chain: 999
    pair: [USDC, HYPE]
    target_usd: 750
```

## Action surface

| Action | Args | Behavior |
|---|---|---|
| `scan` | `--venue?`, `--pair?` | Read-only. Lists candidate pools sorted by 7d fee/TVL APR; pulls TVL, fee tier, current tick, ATR. |
| `quote-open` | `--pool`, `--size` | Computes range bounds given strategy, swaps needed to reach 50/50, expected fee APR, IL at +/-X% price moves. |
| `open` | `--pool`, `--size?` | Mints LP NFT at computed range. Pre-swaps via BRAP if pair is unbalanced. |
| `status` | `--pool?` | Per-position: current tick vs range, time-in-range %, fees earned (USD), IL estimate vs HODL baseline, gauge rewards if applicable. |
| `rebalance` | `--pool` | Withdraw → swap to balance → re-mint at new center. Halts if `max_rebalances_per_day` exceeded or cooldown active. |
| `compound` | `--pool?` | Collect fees, swap to balanced ratio, add to position. |
| `close` | `--pool` | Withdraw entire position to native tokens. |
| `attach` | — | Register `monitor.py` as a wayfinder runner job for this path. |
| `detach` | — | Remove the runner job. |

## Skill triggers

- "open a uniswap LP position on <PAIR>"
- "find the best LP pools for <PAIR>"
- "rebalance my LP positions"
- "compound LP fees"
- "what's my LP performance"
- "close my LP on <POOL>"
- "monitor my LPs"

## Safety rules

- **Always quote-open before open.** Show range bounds, swap needed, expected fee APR, IL at +/-10% / +/-25% / +/-50%. Get confirmation unless user said "just open".
- **Out-of-range warning.** When `status` shows a position out of range, lead with that — out-of-range LPs earn zero fees and accrue IL.
- **Rebalance cooldown is a hard gate.** Don't rebalance within `rebalance_cooldown_minutes` of the last rebalance, even if user manually triggered — surface the cooldown timer and ask if they want to wait or override.
- **Slippage on rebalance.** A rebalance is ~3 transactions (collect, swap, re-mint). If estimated total slippage > 0.5% of position, surface and ask for confirmation.
- **MEV awareness.** When swapping during rebalance, prefer venue-native swap on the same DEX (less MEV exposure than routing through BRAP for the swap leg) — BRAP is for funding, not for in-flow rebalancing.
- **Ledger every leg.** A rebalance writes 3 ledger rows (decrease_liquidity, swap, mint), not one — needed for IL/fee accounting.

## Acceptance criteria

1. `scan` returns pools with non-zero 7d volume and TVL, sorted by realized fee APR (NOT by gross volume).
2. `quote-open` IL estimates match a closed-form V3 IL formula within 5%; range bounds are deterministic given strategy + pool state.
3. `open` mints an LP NFT and writes a ledger entry containing token ID, range ticks, deposited amounts, and venue.
4. `status` correctly tags out-of-range positions and computes time-in-range from on-chain swap events (not from the path's own poll history, which would be lossy).
5. `rebalance` enforces cooldown and daily cap; double-trigger within cooldown returns "skipped: cooldown".
6. `compound` accumulates fees and reinvests them; small-fee positions skip compound when under threshold.
7. `monitor.py` runs for `max_runtime_hours` then exits cleanly; surfaces band-exit candidates through MCP notify, does NOT auto-execute.
8. Smoke test: open → status → rebalance → compound → close on a single Uniswap V3 pool on Base via Gorlami fork.

## Build & publish

```
wayfinder path fmt --path .
wayfinder path doctor --path .
wayfinder path build --path .
wayfinder path publish --path .
```

## Out of scope (v0.1)

- Hedged LP (delta-neutral via perp short) — natural follow-up using funding-rate-harvester as a primitive.
- Just-in-time / single-tick LP — too MEV-exposed for retail.
- Auto-execute rebalance from the monitor — v0.1 monitor is alert-only.
- Multi-NFT split positions in the same pool — start with one NFT per (pool, wallet).
- Permit2 / batch approvals optimization — keep flows readable in v0.1, optimize later.
