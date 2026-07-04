# Path: funding-rate-harvester

**Slug:** `funding-rate-harvester`
**Primary kind:** strategy
**Risk tier:** execution
**One-liner:** Delta-neutral funding harvester with a **triple carry stack**: Delta Lab-discovered HL perp shorts hedged by yield-bearing spot legs (Pendle PT / weETH / sUSDe / HL spot), an optional Boros fixed-rate lock on the harvested funding, and breakeven-gated rotation — across assets and spot legs in v1.0, across venues (CCXT CEXs) in v1.1.

---

## Differentiation (why this wins)

No existing strategy, path, or SDK default combines all four layers:

| Layer | basis_trading_strategy | boros_hype_strategy | kaito-pt-delta-neutral | stablecoin-yield-rotator | **This path** |
|---|---|---|---|---|---|
| Universe discovery (Delta Lab rank) | ✗ | ✗ (HYPE only) | ✗ (KAITO only) | lending only | ✓ |
| Yield-bearing spot leg (PT/LST/sUSDe) | ✗ (plain spot) | ✗ | PT only | n/a | ✓ pluggable |
| Boros fixed-rate lock | ✗ | ✓ | ✗ | ✗ | ✓ optional |
| Rotation w/ breakeven math | ✗ | ✗ | ✗ | ✓ (lending) | ✓ assets → venues |
| Packaged as installable path | ✗ | ✗ | ✓ | ✓ | ✓ |

Three stacked carry sources per position:

1. **Funding carry** — short HL perp collects funding.
2. **Spot-leg yield** — the long hedge itself earns: PT fixed yield, weETH staking, sUSDe, or plain HL spot (zero-yield fallback that keeps smoke tests on-exchange).
3. **Rate lock (optional)** — when floating funding APR exceeds Boros implied fixed by a premium threshold, lock the tenor; floating→fixed conversion no competitor path offers.

Rotation is the fourth edge: positions aren't held statically until carry collapses — the scorer continuously ranks alternatives (other assets, other spot legs, and in v1.1 other short venues) and migrates only when the spread beats total migration cost within a breakeven window.

## Versioned scope

- **v1.0 (this build):** HL is the only hedge venue. Assets from `universe.yaml` + Delta Lab dynamic discovery. Spot legs: `pendle_pt`, `etherfi`, `ethena`, `hl_spot`. Boros lock behind config flag. Rotation across assets and spot legs only.
- **v1.1 (design in, don't build):** CCXT venues (Binance, Bybit, OKX) as alternative short-leg venues, cross-venue migration sagas, paper-mode acceptance gate before live CEX capital, `monitor.py` background poller. v1.0 interfaces MUST anticipate this: hedge venue is an abstraction from day one, ledger keys are saga-ready, scoring already normalizes per-venue funding intervals.
- **v2 (out of scope):** on-chain perps without CCXT support (GMX, Paradex, Lighter, Aster), multi-pair portfolio auto-allocation.

## Reference reading (load before implementing)

Skills:

- `/using-hyperliquid-adapter` — perp + spot execution; load **before** the first HL write (required-params rules, funding sign)
- `/using-pendle-adapter` — PT hedge leg
- `/using-ethena-vault-adapter` — sUSDe spot leg
- `/using-etherfi-adapter` — weETH spot leg
- `/using-boros-adapter` — fixed-rate lock leg
- `/using-delta-lab` — screening (APY 0.98 = 98%, not 0.98%)
- `/using-brap-adapter` — funding-token movements between legs/chains
- `/using-ccxt-adapter` — v1.1 venue layer (read-only calls fine in v1.0 for rate comparison)
- `/developing-wayfinder-paths`, `/backtest-strategy`, `/simulation-dry-run`

Existing code to study:

- `wayfinder_paths/strategies/basis_trading_strategy/` — hedging math, universe scan
- `wayfinder_paths/strategies/boros_hype_strategy/` — Boros + HL composition
- Registry path `kaito-pt-delta-neutral` — single-asset proof (regression baseline)
- Delta Lab client: `get_top_apy`, `screen_perp`, `get_delta_neutral`

## Component plan

```
paths/funding-rate-harvester/
├── wfpath.yaml
├── README.md                  # setup, config, risk disclosures, v1.1 CEX-key guidance
├── inputs/
│   ├── config.yaml
│   └── universe.yaml
├── skill/
│   └── instructions.md
└── scripts/
    ├── main.py                # action surface, orchestration ONLY — no math inline
    ├── scoring.py             # funding normalization + EMA + net-carry + rotation/breakeven math (pure, deterministic)
    ├── legs.py                # spot-leg abstraction (pendle_pt/etherfi/ethena/hl_spot) + hedge-venue abstraction (HL now, CCXT later)
    ├── rate_lock.py           # Boros lock open/roll/unwind, lock PnL tracked separately
    └── monitor.py             # v1.1: background unwind-trigger poller (attach pattern from trailing-hl-orders)
tests/paths/funding-rate-harvester/
    ├── test_scoring.py        # normalization across funding intervals, EMA, breakeven, hysteresis
    ├── test_legs.py           # leg selection + hedge-first ordering
    └── test_rate_lock.py      # lock/roll/unwind decision logic
```

`scoring.py` is where money is lost — pure functions, no I/O, fully unit-tested.

## Config slots

`inputs/config.yaml`:

```yaml
wallet: main
strategy_wallet: funding_harvester
mode: live                                # "paper" | "live" — paper required before v1.1 CEX legs go live
paused: false                             # kill switch: update() no-ops except delta + liquidation checks
hedge:
  venue: hyperliquid                      # abstraction slot; CCXT venues land v1.1
  target_delta_band_pct: 1.5              # rebalance hedge if |delta|/notional exceeds this
  leverage_cap: 3
  margin_buffer_pct: 0.25                 # free margin kept on hedge venue to survive funding flips
spot_legs:                                # allowed long-leg venues, priority order; scorer may override by yield
  - pendle_pt
  - etherfi
  - ethena
  - hl_spot
scoring:
  funding_ema_hours: 72                   # score on rolling EMA, not spot rate — don't chase spikes
  min_net_carry_apr_bps: 1000             # don't open below 10% net stacked APR
  unwind_carry_floor_bps: 200             # unwind when net carry < 2%…
  grace_hours: 12                         # …persisting this long (negative-carry exit, don't pay to stay deployed)
rotation:
  threshold_apr_bps: 400                  # migrate only if candidate − current > 4% APR
  max_breakeven_hours: 48                 # AND migration cost / incremental hourly carry < this
  min_dwell_hours: 24                     # hysteresis: minimum hold per position
risk:
  liq_buffer_pct: 0.15                    # if distance-to-liquidation < 15%: add margin first, reduce second, never skip
  max_drawdown_pct: 8                     # mark-to-market halt, explicit confirmation to continue
  max_position_usd: 5000
  max_total_notional_usd: 10000
  stale_data_intervals: 2                 # funding data older than 2 intervals → freeze rotation, alert
rate_lock:
  enabled: false
  lock_premium_threshold_apr_bps: 200     # lock via Boros when floating > implied fixed by 2% for the tenor
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
allow_dynamic_discovery: true             # also pull top-N from Delta Lab beyond whitelist
```

## Action surface

| Action | Args | Behavior |
|---|---|---|
| `discover` | `--top N` (default 10) | Read-only. Delta Lab `delta-neutral` + `screen/perp` + HL predicted funding; ranks by **net stacked carry** (funding EMA + spot-leg yield − fees − slippage − financing). Table shows each leg's economics and best spot leg per asset. |
| `quote` | `--symbol`, `--size` | Full decomposition: gross funding APR, spot-leg yield, borrow/financing, slippage both legs, fees, net APR, break-even days. Includes Boros lock quote when `rate_lock.enabled`. |
| `deposit` | `--symbol`, `--amount`, `--gas` | Open pair: hedge (HL short) first, then spot leg — minimizes leg-risk window. Paired ledger entry with idempotency key. |
| `update` | — | Core loop (below). Runner calls on interval. |
| `rotate` | `--force?` | Evaluate rotation candidates now; `--force` bypasses dwell (never bypasses breakeven math). Without `--force`, identical to the rotation step inside `update`. |
| `lock` / `unlock` | `--symbol`, `--tenor?` | Manually open/unwind a Boros fixed-rate position sized to the short leg. |
| `status` | — | Per pair: notional, current stacked carry, realized/accrued funding, spot-leg yield accrued, lock PnL (separate line), MTM PnL, days held, leg drift, next rotation eval, liquidation distance. |
| `unwind` | `--symbol?` | Close one/all pairs symmetrically — hedge last on exit (spot first if losing, hedge first only if profitable and delta allows). |
| `exit` | — | Settle everything to USDC, transfer remaining balance to main wallet. |

## Core loop (`update`)

1. **Kill switch** — if `paused`, run only delta + liquidation-distance checks, then return.
2. **Collect** — Delta Lab screen for the universe; HL predicted + historical funding via adapter for open positions (execution-grade). Normalize to annualized APR accounting for per-venue funding intervals (1h HL, 8h most CEXs — matters from v1.1 but normalize now). Maintain `funding_ema_hours` EMA per (venue, asset).
3. **Score** — `net_apr = funding_ema + spot_leg_yield − fees − est_slippage − financing_cost`, per (asset, spot_leg, venue) combo.
4. **Safety rails first** — stale-data guard, liquidation guard, drawdown check, leverage-cap re-check (reduce hedge if drift pushed above cap). These run before any execution step, every cycle.
5. **Negative-carry exit** — if best available net APR across ALL combos < `unwind_carry_floor_bps` for `grace_hours`, unwind and sit in stables.
6. **Rotation decision** — for each open pair, best candidate migrates only if `net_apr(candidate) − net_apr(current) > threshold` AND breakeven (total migration cost / incremental hourly carry) < `max_breakeven_hours` AND dwell ≥ `min_dwell_hours`. Migration cost includes close + open + swap/bridge + slippage; v1.1 adds CEX transfer latency penalty (in-flight capital earns nothing).
7. **Delta check** — rebalance hedge if |delta|/notional > band. Churn guard: no re-rebalance within 1h unless |delta| > 2× band.
8. **Boros decision** — if enabled and floating funding EMA > implied fixed + premium threshold for the tenor: open/roll lock sized to the short leg. Unwind lock when the premium inverts.
9. **Ledger** — every action recorded via LedgerAdapter with idempotency keys `{path}:{venue}:{asset}:{action}:{epoch_bucket}` so crash re-runs never double-execute. This key scheme IS the v1.1 saga foundation.

## Safety rules (non-negotiable)

- **Never open without quoting.** `deposit` internally runs `quote` and displays it before execution.
- **Hedge first on entry, hedge last on exit.** If the spot leg fails after the hedge opens, halt loudly, print state, and ask: close hedge or retry spot. Never leave a silent unhedged short.
- **Funding sign.** Negative funding = shorts pay longs. Verify sign before assuming high APR is harvestable — `/using-hyperliquid-adapter` has the canonical interpretation.
- **HL minimums.** $5 deposit floor (below is lost), $10 min order notional. Hard-fail under these.
- **Liquidation guard ordering.** Add margin first, reduce size second, never skip a cycle.
- **Caps.** Refuse deposits above `max_position_usd` / `max_total_notional_usd`.
- **Gas.** Check native gas on every EVM chain touched before executing; top up per repo gas rules.

## Paper mode & simulation

- `mode: paper` — real market data, fills simulated at book prices ± modeled slippage, virtual balances in local state under the runner state dir (never `/tmp`). Decision logic identical to live.
- EVM-only flows (PT/LST/sUSDe swaps, Boros txs) additionally dry-runnable on Gorlami vnets. HL and CEX legs cannot be forked — paper mode is the only rehearsal for them.
- **Gate:** 7 consecutive days of paper mode under the runner with zero crashed runs and reconciling PnL before any live CEX capital (v1.1). For v1.0 HL-only, a 48h paper run suffices before live pilot.

## Runner integration

```
poetry run wayfinder runner add-job \
  --name funding-harvester-update \
  --type script --interval 900 --config ./config.json
```

15-min interval; rotation is dwell/breakeven-gated so frequent runs are safe. Runner executions bypass the safety-review prompt — treat `update` as live and fund-moving.

## Acceptance criteria

1. `discover` ranking matches Delta Lab `screen/perp funding_now` within rounding, excludes below `min_funding_apr_bps`, and correctly adds spot-leg yield to the stacked score (verifiable per-component in output).
2. `quote` shows the full decomposition, including break-even days and (when enabled) the Boros lock delta.
3. `deposit` opens hedge-then-spot with paired ledger entries; spot-leg failure after hedge → halt + unwind instructions, never silent.
4. `update` respects all rails: no rebalance churn inside 1h/2× band, no rotation inside dwell, no action when stale-data guard trips, negative-carry exit after grace.
5. Rotation executes only when threshold AND breakeven AND dwell all pass — property-tested in `test_scoring.py` including edge cases (zero incremental carry, cost > spread).
6. `rate_lock` open/roll/unwind verified in paper + Gorlami dry-run; lock PnL reported separately in `status`.
7. `unwind` closes both legs and reconciles realized funding + spot yield vs ledger.
8. Smoke test: discover → deposit → update → unwind with `hl_spot` leg (keeps it on-exchange).
9. Regression: KAITO backtest fixture matches `kaito-pt-delta-neutral` published numbers.
10. `scoring.py` functions are pure (no I/O) — enforced by test importing it with adapters mocked out entirely.

## Build & publish

```
wayfinder path fmt --path .
wayfinder path doctor --path .
wayfinder path eval --path .
wayfinder path build --path .
wayfinder path publish --path .
```

## Out of scope (v1.0)

- CCXT short legs, cross-venue migration sagas, CEX API keys — v1.1 (interfaces prepared, not built).
- On-chain perps without CCXT support (GMX, Paradex, Lighter, Aster) — v2.
- Multi-pair auto-allocation in one transaction — one pair at a time.
- LP-as-spot-leg — covered by `concentrated-lp-manager`.
- Leverage beyond 1x-hedged notional — no directional exposure, no looping.
- Background `monitor.py` — ships v1.1 after the controller stabilizes.
