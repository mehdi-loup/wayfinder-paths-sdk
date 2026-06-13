# Path: restaking-yield-stacker

**Slug:** `restaking-yield-stacker`
**Primary kind:** strategy
**Risk tier:** execution
**One-liner:** Stack ETH staking + restaking + AVS rewards, with smart exit-routing — stake to ether.fi, restake to chosen EigenLayer AVS operator, compound rewards, and exit via the cheapest path (Pendle PT, AMM unwind, or native cooldown).

---

## Market gap

ether.fi and EigenCloud adapters both ship in the SDK, neither has a path consuming them. ETH holders looking for "stake + restake + monitor" have to hand-roll the flow today — picking AVS operators, tracking rewards, and timing exits across cooldown / Pendle PT redemption / AMM unwind requires a tool. The eETH/weETH ↔ ETH spread also creates a recurring arbitrage that this path can capture as an exit option.

## Reference reading (load before implementing)

Skills:
- `/using-etherfi-adapter` — ETH → eETH / weETH flow, wrap/unwrap, native cooldown
- `/using-eigencloud-adapter` — strategy discovery, delegation, operator selection
- `/using-pendle-adapter` — PT-weETH option for fixed-yield exit
- `/using-pool-token-balance-data` — eETH/weETH/ETH price reads, Pendle PT discount
- `/using-brap-adapter` — funding from non-mainnet wallets
- `/developing-wayfinder-paths`

Existing code to study:
- `wayfinder_paths/adapters/etherfi_adapter/` — stake/wrap/unwrap surface
- `wayfinder_paths/adapters/eigencloud_adapter/` — operator metadata, delegate/undelegate
- `wayfinder_paths/adapters/pendle_adapter/` — PT discovery, swap-to-PT, redeem-at-maturity
- Moonwell wstETH loop strategy — for ETH-asset lifecycle reference

## Component plan

```
paths/restaking-yield-stacker/
├── wfpath.yaml
├── README.md
├── inputs/
│   ├── config.yaml
│   └── operators.yaml
├── skill/
│   └── instructions.md
└── scripts/
    └── main.py
```

Single component. Restaking is event-light; doesn't need a background monitor.

## Config slots

`inputs/config.yaml`:
```yaml
wallet: main
target_lst: weETH                         # weETH | eETH
target_chain: 1                           # ethereum mainnet
strategy:
  restake_path: native_eigenlayer         # native_eigenlayer | symbiotic | none (just LST)
  compound_rewards: true                  # auto-restake harvested rewards
  compound_min_eth: 0.05                  # don't compound below this
  exit_strategy: cheapest                 # cheapest | pendle_pt | cooldown | amm
risk:
  max_slippage_bps: 30
  max_drawdown_eth_pct: 5
  pendle_pt_min_apr_bps: 400              # only consider PT exit if implied APR ≥ this
ledger_record: true
```

`inputs/operators.yaml`:
```yaml
# user-curated AVS operator preferences. The path queries EigenCloud for live data
# and selects from this whitelist; if empty, surfaces top-N by combined APR + slashing-history score.
allowed_operators: []                     # empty = open universe, ranked
exclude_operators: []
operator_filter:
  min_total_delegated_eth: 1000
  max_slashing_events: 0
  prefer_avs_categories: [data_availability, oracle, bridging]
```

## Action surface

| Action | Args | Behavior |
|---|---|---|
| `discover` | `--top N` | Read-only. Pulls EigenCloud operator + AVS data, ranks by net APR (operator + AVS rewards − fees − slashing-adjusted risk). |
| `quote` | `--amount` | For a given ETH stake size, shows: native staking APR (eETH), restaking APR (selected operator), expected total APR, gas cost, exit-cost menu. |
| `stake` | `--amount`, `--operator?` | ETH → eETH → weETH (if config) → delegate to chosen operator (or top-ranked from filter). Records start-NAV and start-time. |
| `compound` | — | Harvest restaking + AVS rewards, add to position (re-stake the proceeds), or swap to ETH if `compound_rewards: false`. |
| `status` | — | Position breakdown: ETH-equivalent (via eETH→ETH oracle rate), AVS rewards accrued, days staked, projected next-claim, exit-cost menu (PT discount %, AMM slippage, cooldown days). |
| `quote-exit` | `--amount?` | Compares all three exit routes (Pendle PT, AMM unwind via BRAP, native cooldown) on slippage + time + total ETH out. |
| `queue-withdraw` | `--amount` | Initiates native cooldown if that's the chosen exit. |
| `exit` | `--route?` | Executes selected route. Default = `config.strategy.exit_strategy`. |

## Skill triggers

- "stake my ETH for restaking yield"
- "find the best AVS operators"
- "what's my restaking position"
- "compound my restaking rewards"
- "exit my restaking position"
- "what's the cheapest way to exit my weETH"

## Safety rules

- **Operator due-diligence gate.** Before delegating, surface operator's slashing history, total delegated ETH, AVS list, fee. Require confirmation unless `allowed_operators` whitelist matched the choice.
- **Exit cost is non-trivial.** `quote-exit` is mandatory before `exit` — show all three routes with current slippage and absolute ETH out, ranked. Never default-execute the most-expensive exit silently.
- **PT maturity awareness.** If exit_strategy=pendle_pt and the matched PT matures in <7 days, prefer holding-to-maturity; if >30 days and PT discount > AMM slippage, prefer PT sale. If between, surface both and ask.
- **Native cooldown lock-in.** Once `queue-withdraw` is called, the user's eETH is committed to the cooldown queue — irreversible until cooldown ends. Confirm explicitly.
- **Compound dust guard.** Don't compound below `compound_min_eth` — gas would eat the rewards.
- **Drawdown halt.** If position ETH-equivalent drops by `max_drawdown_eth_pct` below start-NAV (which would imply slashing or oracle drift), surface immediately and pause auto-compound until user reviews.

## Acceptance criteria

1. `discover` ranks operators by net APR adjusted for slashing risk; matches EigenCloud's UI within 10 bps for the top 10.
2. `stake` performs ETH → eETH → weETH (if configured) → delegate, with each step gas-checked and ledger-recorded; halts on revert.
3. `status` reports ETH-equivalent using the live eETH/ETH redemption rate, NOT the AMM mid (which can drift several bps off fair value).
4. `quote-exit` returns three rows (Pendle PT, AMM, cooldown) with deterministic ETH-out estimates; the "cheapest" choice is highlighted but the user picks.
5. `exit` via Pendle PT correctly handles both pre-maturity sale (sell PT for ETH on Pendle AMM) and at-maturity redeem (PT → underlying).
6. `compound` is idempotent — running twice in quick succession claims any unclaimed rewards on the second call but doesn't double-process.
7. Smoke test: stake → status → compound → quote-exit → exit (via AMM, fastest route) on Gorlami fork; verifies ledger reconciles.

## Build & publish

```
wayfinder path fmt --path .
wayfinder path doctor --path .
wayfinder path build --path .
wayfinder path publish --path .
```

## Out of scope (v0.1)

- LST other than eETH/weETH (Renzo ezETH, Kelp rsETH, Puffer pufETH) — pick one to ship well; expand later.
- Loop-leverage on weETH (borrow ETH against weETH, restake again) — separate path; this one stays unleveraged.
- Symbiotic / Karak / other restaking platforms — EigenCloud first.
- Auto-rotate between operators on APR changes — manual `update` only in v0.1.
- Slashing insurance / hedge — surface risk only.
