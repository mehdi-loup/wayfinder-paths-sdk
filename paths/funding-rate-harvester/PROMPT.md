# Path: funding-rate-harvester

**Slug:** `funding-rate-harvester`
**Primary kind:** strategy
**Risk tier:** execution
**One-liner:** Generalize the proven KAITO PT-delta-neutral pattern to any HL perp + spot/PT hedge — discover the best-carry pairs from Delta Lab, open delta-neutral, harvest funding, unwind on carry collapse.

---

## Market gap

`kaito-pt-delta-neutral` proves the mechanic works on a single asset. `basis_trading_strategy` and `boros_hype_strategy` cover the in-tree side but aren't packaged as installable paths. There's no path that scans the universe, ranks pairs by net carry, and lets a user pick or auto-allocate. Delta Lab's `screen/perp` and `delta-neutral` resources expose exactly the data needed; nothing consumes them as a path.

## Reference reading (load before implementing)

Skills:
- `/using-hyperliquid-adapter` — perp + spot execution; load **before** the first `hyperliquid_execute` call (required-params rules)
- `/using-pendle-adapter` — PT hedge leg
- `/using-ethena-vault-adapter` — sUSDe as collateral/spot leg
- `/using-etherfi-adapter` — weETH spot leg
- `/using-delta-lab` — screening (note: APY 0.98 = 98%, not 0.98%)
- `/using-brap-adapter` — funding token movements
- `/developing-wayfinder-paths`
- `/backtest-strategy` — sanity-check carry assumptions before live capital

Existing code to study:
- `wayfinder_paths/strategies/basis_trading_strategy/` — universe scan + carry-rank pattern
- `wayfinder_paths/strategies/boros_hype_strategy/` — Boros + HL composition
- Registry path `kaito-pt-delta-neutral` — single-asset proof
- Delta Lab client `get_top_apy`, `screen_perp`, `get_delta_neutral`

## Component plan

```
paths/funding-rate-harvester/
├── wfpath.yaml
├── README.md
├── inputs/
│   ├── config.yaml
│   └── universe.yaml
├── skill/
│   └── instructions.md
└── scripts/
    ├── main.py            # discover/deposit/update/status/unwind/exit
    └── monitor.py         # optional v0.2: background unwind-trigger poller
```

v0.1 ships only `main`. Add `monitor` after the controller stabilizes — register it as a `kind: script` component with the `attach` pattern from `trailing-hl-orders`.

## Config slots

`inputs/config.yaml`:
```yaml
wallet: main
strategy_wallet: funding_harvester
hedge:
  target_delta_band_pct: 1.5             # rebalance hedge if |delta|/notional exceeds this
  leverage_cap: 3
  hedge_venue: hyperliquid               # only HL in v0.1
spot_legs:                                # allowed long-leg venues (in priority order)
  - pendle_pt
  - etherfi
  - ethena
  - hl_spot
risk:
  min_net_carry_apr_bps: 1000            # don't open a pair below 10% net APY
  unwind_carry_floor_bps: 200            # unwind when net carry drops below 2%
  max_drawdown_pct: 8
  max_position_usd: 5000
slippage_bps: 25
ledger_record: true
```

`inputs/universe.yaml`:
```yaml
symbols: [BTC, ETH, SOL, HYPE, KAITO, ENA, EIGEN, ETHFI]
filters:
  min_oi_usd: 10_000_000
  min_funding_apr_bps: 800
  max_lookback_volatility_pct: 60
allow_dynamic_discovery: true             # if true, also pulls top-N from Delta Lab beyond whitelist
```

## Action surface

| Action | Args | Behavior |
|---|---|---|
| `discover` | `--top N` (default 10) | Read-only. Pulls Delta Lab `delta-neutral` + HL funding; ranks by net-of-fees carry APR. Shows table with each leg's economics. |
| `quote` | `--symbol`, `--size` | Computes expected open cost (slippage on both legs, opening funding window, fees) and break-even days. |
| `deposit` | `--symbol`, `--amount`, `--gas` | Open delta-neutral pair: spot/PT long + HL short, hedge-first to avoid leg-risk window. |
| `update` | — | Rebalance hedge to target band; harvest funding (auto-claim is HL native); compound funding into spot leg if config says so. |
| `status` | — | All open pairs: notional, current carry, realized funding, mark-to-market PnL, days held, leg drift. |
| `unwind` | `--symbol?` | Close one or all pairs symmetrically (hedge first if profitable, spot first if losing). |
| `exit` | — | Transfer remaining wallet balance to main. |

## Skill triggers

- "find the best funding rate pairs"
- "scan delta-neutral opportunities"
- "open a delta-neutral position on <SYMBOL>"
- "harvest funding"
- "unwind my funding trades"
- "check my basis carry"

## Safety rules

- **Never open without quoting.** Show expected carry, fees, slippage on both legs, and break-even days before executing.
- **Hedge first on entry, hedge last on exit.** Reduces leg-risk window. If the spot leg fails after the hedge opens, the user is short-unhedged — surface this loudly and ask whether to close hedge or retry spot.
- **Funding sign reminder.** Negative funding means shorts pay longs. Verify the rate sign before assuming "high APR = good for shorts" — load `/using-hyperliquid-adapter` for the canonical interpretation.
- **HL minimums.** $5 deposit floor (anything less is lost), $10 minimum order notional. Hard-fail under these.
- **Drawdown halt.** If any open pair exceeds `max_drawdown_pct` mark-to-market, surface and require explicit confirmation to continue.
- **Leverage cap enforcement on every update.** Recompute notional vs collateral every cycle; reduce hedge size if drift pushed leverage above cap.

## Acceptance criteria

1. `discover` returns a ranked table that matches Delta Lab `screen/perp funding_now` ranking (within rounding) and excludes anything below `min_funding_apr_bps`.
2. `quote` produces an estimate with explicit decomposition: gross funding APR, borrow/financing cost, slippage, fees, expected net APR, break-even days.
3. `deposit` opens both legs and writes a paired ledger entry; if the spot leg reverts after the hedge opens, the script halts and prints unwind instructions.
4. `update` rebalances when |delta| > band; doesn't churn (hysteresis: don't rebalance again within 1h unless |delta| > 2× band).
5. `unwind` closes both legs and reconciles realized funding vs ledger.
6. Smoke test exercises discover → deposit → update → unwind on HL testnet (or HL paper account if available); spot leg can be HL spot in v0.1 to keep the smoke test on-exchange.
7. Backtest fixture confirms historical carry on KAITO matches the `kaito-pt-delta-neutral` published numbers (regression check for the generalization).

## Build & publish

```
wayfinder path fmt --path .
wayfinder path doctor --path .
wayfinder path eval --path .
wayfinder path build --path .
wayfinder path publish --path .
```

## Out of scope (v0.1)

- Cross-venue hedge (Aster, Bybit) — HL only first, generalize later.
- Auto-allocate across multiple pairs in one transaction — start with one-pair-at-a-time UX.
- LP-as-spot-leg (Aerodrome / Uniswap CLM) — separate `concentrated-lp-manager` path covers that.
- Background monitor — ship after controller stabilizes.
