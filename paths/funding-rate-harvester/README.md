# Funding Rate Harvester

Delta-neutral funding harvester with a **triple carry stack**:

1. **Funding carry** — short Hyperliquid perp collects funding (positive funding = shorts receive).
2. **Spot-leg yield** — the long hedge itself earns: Pendle PT fixed yield, weETH staking, sUSDe vault APY, or plain HL spot (zero-yield fallback that keeps smoke tests on-exchange).
3. **Rate lock (optional)** — when the floating funding EMA exceeds the Boros implied fixed by a premium threshold, short YU on the matching Boros market to convert the floating stream to fixed for the tenor.

Rotation is the fourth edge: the scorer continuously ranks alternatives (other assets, other spot legs) and migrates only when the spread beats total migration cost within a breakeven window — gated by threshold AND breakeven AND dwell.

## Quick start

```bash
python scripts/wf_run.py -- --action discover --top 10
python scripts/wf_run.py -- --action quote --symbol ETH --size 1000
python scripts/wf_run.py -- --action deposit --symbol ETH --amount 1000          # plan only
python scripts/wf_run.py -- --action deposit --symbol ETH --amount 1000 --confirm
python scripts/wf_run.py -- --action update --confirm                             # core loop
python scripts/wf_run.py -- --action status
python scripts/wf_run.py -- --action unwind --confirm && python scripts/wf_run.py -- --action exit --confirm
```

In the SDK repo, substitute `poetry run python paths/funding-rate-harvester/scripts/main.py` for `python scripts/wf_run.py --`.

## Configuration (`inputs/config.yaml`)

| Key | Meaning |
|---|---|
| `wallet` | blank = session-connected wallet; label pins a local wallet |
| `strategy_wallet` | optional dedicated wallet; `exit` transfers settled USDC back to the main wallet |
| `mode` | `paper` (virtual fills at live prices ± slippage) or `live` |
| `paused` | kill switch: `update` runs only delta + liquidation checks |
| `hedge.target_delta_band_pct` | rebalance hedge when \|delta\|/notional exceeds this (churn guard: no re-rebalance within 1h unless 2× band) |
| `hedge.leverage_cap` / `margin_buffer_pct` | short sizing + margin kept free to survive funding flips |
| `spot_legs` | allowed legs, priority order; the scorer picks the highest-yield supported leg |
| `scoring.funding_ema_hours` | funding is scored on a rolling EMA, not the spot rate |
| `scoring.min_net_carry_apr_bps` | refuse to open below this net stacked APR |
| `scoring.unwind_carry_floor_bps` + `grace_hours` | negative-carry exit: unwind and sit in stables |
| `rotation.threshold_apr_bps` / `max_breakeven_hours` / `min_dwell_hours` | all three must pass to migrate |
| `risk.liq_buffer_pct` | liquidation guard: add margin first, reduce second, never skip |
| `risk.max_drawdown_pct` | mark-to-market halt; `update --confirm --resume` to continue |
| `risk.max_position_usd` / `max_total_notional_usd` | hard caps enforced at deposit |
| `risk.stale_data_intervals` | funding data older than N intervals → freeze rotation, alert |
| `rate_lock.enabled` + `lock_premium_threshold_apr_bps` | Boros lock gate (see below) |
| `costs.*` | per-side fee + expected-slippage estimates (bps) folded into the APR score |
| `slippage_bps` | protective execution tolerance per fill; also the paper-mode fill model (scoring uses `costs.slippage_cost_bps_per_fill` instead) |

`inputs/universe.yaml` holds the symbol whitelist, OI/funding/volatility filters, and `allow_dynamic_discovery` (adds top Delta Lab funding markets beyond the whitelist).

## Architecture

```
scripts/
├── main.py       # action surface + orchestration only — no math inline
├── scoring.py    # pure, deterministic: normalization, EMA, net carry, rotation/breakeven, rails
├── legs.py       # hedge-venue + spot-leg abstractions (HL now, CCXT v1.1) + paper wrappers
└── rate_lock.py  # Boros lock selection/sizing (pure) + thin execution glue
```

- **Hedge venue is an abstraction from day one.** v1.0 ships Hyperliquid (1h funding); scoring already normalizes per-venue funding intervals, and ledger/idempotency keys are `{path}:{venue}:{asset}:{action}:{epoch_bucket}` — the v1.1 cross-venue saga foundation.
- **Spot legs** declare which symbols they hedge: `pendle_pt` matches PT markets whose price tracks the shorted asset (weETH→ETH, sUSDe→USDE, sKAITO→KAITO), `etherfi` hedges ETH, `ethena` hedges USDE, `hl_spot` hedges anything with an HL spot pair (UBTC/UETH wrap BTC/ETH). Legs with unavailable yield data are excluded from scoring rather than treated as 0%.
- **Entry/exit for weETH and sUSDe goes through BRAP market swaps**, not stake/cooldown flows — ether.fi withdrawals are async NFTs and sUSDe unstaking has a ~7d cooldown, either of which would strand a rotation mid-flight.
- Expired Pendle PTs are redeemed via `execute_convert` (PT → SY underlying), then swept to USDC.

## Safety rails (ordered, every `update` cycle)

1. **Stale-data guard** — funding older than `stale_data_intervals` → rotation frozen + alert.
2. **Liquidation guard** — distance-to-liq < `liq_buffer_pct` → add margin first, reduce second, never skip a cycle.
3. **Drawdown halt** — MTM value vs deposited reference beyond `max_drawdown_pct` → halt everything; explicit `--resume` required.
4. **Leverage-cap recheck** — price drift pushing effective leverage above cap → reduce hedge.
5. **Negative-carry exit** — best available net APR below floor for `grace_hours` → unwind to stables.

Execution ordering is non-negotiable: **hedge first on entry, hedge last on exit**. If the spot leg fails after the hedge opens, the run halts loudly with the unhedged short and remediation options — never a silent unhedged position. Same-venue HL pairs (hedge + `hl_spot`) fill both legs atomically via the paired filler.

## Boros rate lock semantics

With the pair short receiving floating funding, a **short YU** position on the matching Boros market receives fixed / pays floating — net effect: the funding stream is fixed for the tenor. The gate (`lock_premium_threshold_apr_bps`) opens the lock when the floating EMA exceeds the implied fixed by the premium (rich floating tends to mean-revert; the premium is what you pay for certainty), and unwinds when the premium inverts. Boros `mid_apr` is total-remaining-tenor yield, not an APR — the annualization (`mid_apr / remaining_days × 365`) is handled in `scoring.boros_fixed_apr`. Lock PnL is reported as a separate line in `status`, never blended into pair carry.

Collateral note: Boros margin is **not** 1:1 with YU size — sizing uses a rate-scaled margin heuristic with a 60% utilization buffer, and the wallet needs the market's collateral token (usually USDT0) on Arbitrum for deposits.

## Paper mode & simulation

- `mode: paper` — identical decision logic, live market data, fills simulated at mark ± `slippage_bps`, virtual balances persisted under the runner state dir (never `/tmp`).
- **Gate:** 48h of paper `update` runs with zero crashed runs and reconciling PnL before a live HL-only pilot. For v1.1 CEX legs: 7 consecutive days.
- EVM-only flows (PT/weETH/sUSDe swaps, Boros txs) can additionally be dry-run on Gorlami vnets. HL legs cannot be forked — paper mode is their only rehearsal.

## Runner integration (unattended updates)

Runner script jobs only run `.py` files inside `.wayfinder_runs/`, so schedule via a thin wrapper:

```python
# .wayfinder_runs/library/funding-rate-harvester/update.py
import asyncio, sys
sys.path.insert(0, "paths/funding-rate-harvester/scripts")
import main as harvester

async def run() -> None:
    config = harvester.load_yaml("config.yaml")
    universe = harvester.load_yaml("universe.yaml")
    state = harvester.load_state()
    ctx = await harvester.build_ctx(config, universe, state)
    harvester.emit(await harvester.action_update(ctx, confirm=True, resume=False))

asyncio.run(run())
```

```bash
poetry run wayfinder runner add-job --name funding-harvester-update \
  --type script --script-path .wayfinder_runs/library/funding-rate-harvester/update.py \
  --interval 900 --config ./config.json
```

15-minute interval is safe: rotation is dwell/breakeven-gated, delta rebalance has a churn guard, and idempotency keys dedupe crash re-runs. **Runner executions bypass the safety-review prompt — treat scheduled `update` as live and fund-moving.**

## Risk disclosures

- **Funding can flip.** Positive funding is not guaranteed; the EMA + negative-carry exit bound but do not eliminate the cost of a regime change.
- **Basis risk on yield legs.** PT prices discount to expiry, weETH/sUSDe can deviate from their underlying; the hedge is sized to the shorted symbol, not the wrapper premium.
- **Liquidation risk.** The short is levered (`leverage_cap`); a violent squeeze can outrun the liquidation guard between runner ticks.
- **Boros margin risk.** Lock positions can be liquidated on mark-APR moves; sizing uses a buffer but monitor `status` after large funding swings.
- **Smart-contract / venue risk** across Hyperliquid, Pendle, ether.fi, Ethena, Boros, and BRAP routes.
- **HL minimums:** $5 deposit floor (below is lost), $10 minimum order notional — enforced, don't work around them.

## v1.1 (designed, not built)

CCXT venues (Binance, Bybit, OKX) as alternative short legs, cross-venue migration sagas (the idempotency-key scheme is already saga-ready), a paper-mode acceptance gate before live CEX capital, and a background `monitor.py` unwind-trigger poller. **CEX API keys** will live in `config.json` under per-venue entries — never in the path bundle; grant trade-only permissions (no withdrawals) and IP-allowlist the runner host.

## Development

```bash
poetry run pytest tests/paths/funding-rate-harvester -v   # pure-logic tests (no network)
poetry run wayfinder path fmt --path paths/funding-rate-harvester
poetry run wayfinder path doctor --path paths/funding-rate-harvester
poetry run wayfinder path build --path paths/funding-rate-harvester
```
