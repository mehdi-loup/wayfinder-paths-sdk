# Funding Rate Harvester

Delta-neutral funding harvester with a **triple carry stack**:

1. **Funding carry** — short Hyperliquid perp collects funding (positive funding = shorts receive).
2. **Spot-leg yield** — the long hedge itself earns: Pendle PT fixed yield, weETH staking, sUSDe vault APY, or plain HL spot (zero-yield fallback that keeps smoke tests on-exchange).
3. **Rate lock (optional)** — when the floating funding EMA exceeds the Boros implied fixed by a premium threshold, short YU on the matching Boros market to convert the floating stream to fixed for the tenor.

Rotation is the fourth edge: the scorer continuously ranks alternatives (other assets, other spot legs) and migrates only when the spread beats total migration cost within a breakeven window — gated by threshold AND breakeven AND dwell.

## Applet

The bundled applet (`applet/dist/index.html`) is a **static, read-only** snapshot of a `discover` run — the net-stacked-carry ranking per (asset, spot leg), with the funding / spot-yield / fees / slippage decomposition and each symbol's EMA-maturity state. It is fully self-contained (`externalOrigins: []`, no runtime fetch, no wallet/balance/position data) and embeds a snapshot generated at build time; refresh it by re-running `--action discover` and rebuilding. It never signs, reads balances, or moves funds.

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
| `mode` | `paper` (virtual fills at live prices ± slippage; the shipped default) or `live` |
| `paper_gate_hours` | live deposits refuse until this many paper-update hours are recorded; 0 disables the gate (the `--skip-paper-gate` flag lets an operator who has rehearsed elsewhere proceed deliberately) |
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

1. **Stale-data guard** — funding older than `stale_data_intervals` → rotation frozen + alert. The negative-carry floor clock also freezes: missing or unscoreable data can never age into an unwind.
2. **Liquidation guard** — distance-to-liq < `liq_buffer_pct` → add margin first, reduce second, never skip a cycle. A "reduce" shrinks **both legs proportionally**, hedge first, and is risk-monotone on failure: a refused hedge reduce aborts before the spot leg is touched (pair stays symmetric), and a spot failure after the hedge shrank marks the pair `impaired` (carry actions suspend + alert; recover via `unwind`). A successful reduce scales `entry_notional`/`entry_value` and releases the freed slice from the drawdown reference, so a deliberate de-risk never reads as a loss. Delta rebalance is suppressed for reduced pairs that cycle.
3. **Drawdown halt** — MTM value vs deposited reference beyond `max_drawdown_pct` → halt everything; explicit `--resume` required. When a reduction ran this cycle, positions are re-marked before the check so the shrunk value is compared against the shrunk reference — never pre-reduction value vs post-reduction reference.
4. **Leverage-cap recheck** — price drift pushing effective leverage above cap → proportional pair reduce (same semantics as the liquidation reduce).
5. **Negative-carry exit** — best available net APR below floor for `grace_hours` → unwind to stables. Only evaluated on fresh, scoreable data, and only **mature** combos count as alternatives — an immature EMA spike is not depositable, so it can neither keep the book deployed nor attract a rotation.

Execution ordering is non-negotiable: **hedge first on entry, hedge last on exit**. Every open pre-records the pair in durable state (`status: opening`); if the spot leg fails after the hedge opens — or the process crashes mid-open — the pair persists as `half_open`, `update` suspends carry actions for it and alerts, and `unwind --symbol <SYM> --confirm` closes whatever filled. If an exit closes the spot leg but the hedge close then fails, the pair is marked `impaired` (a naked short is never re-scored as a healthy carry pair) with an alert to retry the unwind. Same-venue HL pairs (hedge + `hl_spot`) fill both legs atomically via the paired filler.

**Lot isolation.** Each pair records its exact fill as a `spot_lot` (units, and for PT legs the precise PT address + chain — resolved *before* opening, so every read binds to it). Closes, reduces, and valuations all operate on the lot — a session wallet already holding UETH/weETH/sUSDe or another PT of the same symbol root never has unrelated holdings sold or counted as pair value. Pendle closes honor partial sizes (fraction of the raw PT balance), so proportional reduces shrink the PT leg correctly instead of dumping the whole position. The invariant holds through every failure mode: the pre-open baseline (`pre_spot_units`) is persisted before execution and a failed pre-read **aborts the deposit**; a half-open recovery closes only the recorded fill delta (possibly zero spot); recovery-state pairs get hedge-only de-risks; a pair without any lot record refuses to close or reduce rather than fall back to a wallet-wide close. Closes persist a `closing` status before touching the legs, so a crash between the spot close and the hedge close resurfaces as a suspended pair instead of a naked short scored as healthy carry.

**Spot-leg funding preflight.** Live deposits verify the leg's own USDC on its actual chain before the hedge opens (mainnet for weETH/sUSDe, the PT market's chain for Pendle — a Base PT needs Base USDC, not mainnet). Rotation preflight uses the same chain-aware requirement and only counts freed proceeds that land on the destination chain.

Rotations **pre-flight the destination before closing anything**: candidate carry vs the deposit floor, gas on the target leg's chains, and funding feasibility with the capital the close will actually free (proceeds stay on the venue where the closing leg lived — cross-venue routing is v1.1). A preflight failure skips the migration and leaves the current pair untouched; if the re-open still refuses after the close, the run alerts loudly and reports funds settled in stables rather than reading like a routine exit.

## Confirmation gates (every fund-moving action)

Every action that can move funds — `deposit`, `update`, `rotate`, `lock`, `unlock`, `unwind`, `exit` — requires an explicit `--confirm`. Run without it and the action returns a `requires_confirmation` plan and broadcasts nothing; the caller is expected to review the plan and only re-run with `--confirm` after the user approves.

**Main-wallet transfer (`exit`).** `exit` is a two-part settlement and each part is gated:

1. Without `--confirm`, `exit` returns a plan showing the estimated HL withdrawal amount and the destination main-wallet label — no funds move.
2. With `--confirm`, it withdraws the strategy wallet's Hyperliquid USDC to Arbitrum, then — **only when a dedicated `strategy_wallet` is configured** (distinct from the main wallet) — transfers the remaining USDC to the main wallet's on-chain address, which is resolved from the configured wallet, never from an address supplied at the command line. If the operating wallet *is* the main wallet, no transfer step runs. `exit` refuses to run while any pair is still open, so funds are always fully settled first.

## Boros rate lock semantics

With the pair short receiving floating funding, a **short YU** position on the matching Boros market receives fixed / pays floating — net effect: the funding stream is fixed for the tenor. The gate (`lock_premium_threshold_apr_bps`) opens the lock when the floating EMA exceeds the implied fixed by the premium (rich floating tends to mean-revert; the premium is what you pay for certainty), and unwinds when the premium inverts. Boros `mid_apr` is total-remaining-tenor yield, not an APR — the annualization (`mid_apr / remaining_days × 365`) is handled in `scoring.boros_fixed_apr`. Lock PnL is reported as a separate line in `status`, never blended into pair carry.

Collateral note: Boros margin is **not** 1:1 with YU size — sizing uses a rate-scaled margin heuristic with a 60% utilization buffer, and the wallet needs the market's collateral token (usually USDT0) on Arbitrum for deposits.

## Paper mode & simulation

- `mode: paper` — identical decision logic, live market data, fills simulated at mark ± `slippage_bps`, virtual balances persisted under the runner state dir (never `/tmp`). This is the shipped default.
- **Gate (enforced):** live deposits refuse until `paper_gate_hours` (default 48h) of paper rehearsal is recorded in state. Hours only accrue on `--confirm` update cycles **with an open paper pair** (idle or dry-run ticks don't count), capped at 1h per cycle so one long gap can't satisfy it. The `--skip-paper-gate` flag is a deliberate operator escape for someone who has already rehearsed the flow elsewhere. For v1.1 CEX legs: 7 consecutive days.
- First sight of a symbol seeds its funding EMA from realized funding history over the EMA window (not the instantaneous rate). If history seeding fails, the EMA is flagged **immature** and deposits/rotations into that symbol refuse until a full EMA window of live samples has accumulated — the spike protection is enforced, not best-effort.
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

15-minute interval is safe: rotation is dwell/breakeven-gated, delta rebalance has a churn guard, and idempotency keys dedupe crash re-runs. **Scheduled runner executions run unattended and are not individually confirmed by an operator — treat every scheduled `update` as a live, fund-moving action and size limits (`max_position_usd`, `max_total_notional_usd`, `leverage_cap`) accordingly before enabling a schedule.** The paper/live mode-consistency guard runs inside `action_update` itself, so it also protects wrapper invocations like this one.

## Risk disclosures

- **Funding can flip.** Positive funding is not guaranteed; the EMA + negative-carry exit bound but do not eliminate the cost of a regime change.
- **Basis risk on yield legs.** PT prices discount to expiry, weETH/sUSDe can deviate from their underlying; the hedge is sized to the shorted symbol, not the wrapper premium.
- **Liquidation risk.** The short is levered (`leverage_cap`); a violent squeeze can outrun the liquidation guard between runner ticks.
- **Boros margin risk.** Lock positions can be liquidated on mark-APR moves; sizing uses a buffer but monitor `status` after large funding swings.
- **Smart-contract / venue risk** across Hyperliquid, Pendle, ether.fi, Ethena, Boros, and BRAP routes.
- **HL minimums:** $5 deposit floor (below is lost), $10 minimum order notional — enforced, don't work around them.

## v1.1 (designed, not built)

CCXT venues (Binance, Bybit, OKX) as alternative short legs, cross-venue migration sagas (the idempotency-key scheme is already saga-ready), a paper-mode acceptance gate before live CEX capital, and a background `monitor.py` unwind-trigger poller. **CEX API keys** will live in `config.json` under per-venue entries — never in the path bundle; grant trade-only permissions (no withdrawals) and IP-allowlist the runner host.

## Tests

```bash
poetry run pytest tests/paths/funding-rate-harvester -v   # pure-logic tests (no network)
```
