# stablecoin-yield-rotator — TODO

## Morpho on HyperEVM (chain 999) — 0.5.0 candidate (research 2026-07-16)

Cheapest high-value extension found in the 0.5.0 research pass. The path already
runs on HyperEVM for Euler and Hyperlend, but `VENUE_CHAIN_SUPPORT` gates the two
Morpho venues to `{1, 137, 8453, 42161}`. The Morpho public API indexes HyperEVM
(55 listed markets, 12 vaults as of 2026-07-16): USDC markets at 4.6–6.3% APY on
$6–16M TVL, feUSDC vault 5.6% on $11M, USD₮0 markets 6.1–6.6%. The SDK's
`MORPHO_BY_CHAIN` already carries the 999 deployment and discovery goes through
the same Morpho API, so the change is plausibly just adding 999 to the two
`morpho_*` entries + tests.

**USDT0 catch:** HyperEVM's dominant USDT is "USD₮0", which the current asset
list won't match. Capturing it needs (a) `USDT0` added to config `assets`, and
(b) a fix to `_matches_asset` in `venues.py` — its first branch compares the
lowercase `normalize_symbol()` output against the UPPERCASE allowed set, so it
never matches and the ₮→T translation is dead code today. Compare normalized
symbols case-insensitively (e.g. `normalize_symbol(symbol).upper() in allowed`).

## APY persistence / anti-churn ranking — deferred to 0.5.0

Ranking is on **instantaneous** `supply_apy`. A transient APY spike on a thin
market can trigger a gas-paying rotation that immediately mean-reverts — the
`min_apy_delta_bps` + payback gates blunt small churn but don't stop this. Rank
on a **persisted edge** instead: a short Delta Lab time-average, or require the
target's advantage to have *held across the last N hours* before rotating.

Highest-value modeling improvement, but a real feature with a new time-series
data dependency (not the reliability/correctness theme of 0.2.x–0.4.0), so it's
held for 0.5.0 — the first release meant to expand capability beyond 0.1.6. Stays
inside "no new venues." Needs tests + an honest README/limitations update, and
re-routes to human review (execution tier). ~~Don't start until 0.4.0 clears
review.~~ **Unblocked 2026-07-16: 0.4.1 is live + bonded.**

**Design note (research 2026-07-16):** Delta Lab `screen_lending` carries
`net_supply_mean_7d/30d`, but its ids only map cleanly to Morpho blue markets
(external_id = uniqueKey) and Euler (vault address) — Morpho vaults are absent
and Aave/Moonwell/Hyperlend rows aren't executable ids. Prefer self-collected
APY history persisted across scan cycles (works for every venue, no new data
dependency) at the cost of an N-cycle warm-up before the gate activates.

## Pendle (PT) — needs a redesign, not just wiring

Pendle is a large stablecoin yield source (PT-sUSDe, PT-USDC, etc.), but it does
**not** fit this path's floating-APY rotation model as-is. Reasons:

1. **No lend/unlend interface.** The Pendle adapter only builds AMM swap/convert
   txs (`build_best_pt_swap_tx`, `sdk_swap_v2`, `execute_swap`/`execute_convert`)
   — no `lend`/`unlend`/`deposit`/`withdraw`/`redeem`. Entry is a swap with price
   impact, not a 1:1 supply.
2. **Fixed-to-maturity yield, not a floating supply rate.** PT carries
   `impliedApy`/`fixedApy` + `expiry`/`daysToExpiry`. The rotator ranks venues by a
   current floating `supply_apy` it re-evaluates each cycle; comparing a
   locked-to-maturity rate against floating lending APYs is apples-to-oranges.
3. **Early exit is mark-to-market.** Selling a PT before maturity realizes PT
   price risk + swap slippage, breaking the "withdraw to ~principal anytime"
   premise that makes free rotation safe. The PT token also isn't the base stable,
   so `(asset_symbol, market)` accounting and atomic withdraw→redeposit don't hold.

**To support it** would require a new leg type (swap-in/swap-out, slippage-gated,
maturity-aware) and a different ranking basis — effectively a hybrid fixed-term
strategy. Out of scope for the floating-APY rotator unless we decide to evolve it.

## Avantis (avUSDC) — held on a branch

Avantis support (avUSDC perp-LP venue + principal-risk opt-in gate) is implemented
but intentionally kept out of this release. It lives on the `feat/avantis-venue`
branch pending a decision to ship principal-risk venues.

## Yield-bearing stable wrappers (sUSDe / sDAI) — DEMOTED (research 2026-07-16)

ERC-4626 / rebasing stable wrappers. Originally assumed to fit the lend/unlend
rotation model, but research demoted this:

1. **sUSDe exit is NOT "redeem anytime".** The SDK's `ethena_vault_adapter`
   confirms withdraw is a two-step cooldown (`cooldownShares`/`cooldownAssets`,
   ~7 days) then `unstake` — principal is locked and yield-less during cooldown,
   breaking the atomic withdraw→redeposit premise. Exit-by-swap via BRAP is
   possible near 1:1 but reintroduces slippage/price risk (the Pendle problem).
2. **Rates don't justify it.** As of 2026-07-16 sUSDe is ~3.9% and sUSDS ~3.6% —
   below what the rotator already earns on covered Morpho mainnet markets
   (7–13% on $5M+ TVL). No edge to capture at current rates.

sUSDS/sDAI (instant redemption via PSM) remains the only wrapper that truly fits
the model — revisit only if the Sky Savings Rate meaningfully exceeds covered
lending venues. sUSDe carries Ethena protocol risk on top (same caveat already
flagged for USDe-as-lend-asset).

## Landscape watch (research 2026-07-16)

- **Fluid** is now a top-tier lending venue (USDC 4.3–5.5%) but has no SDK
  adapter — an upstream adapter project, not path work.
- **Morpho on Monad (143):** ~7.5% on $17M TVL; SDK has the deployment, but a
  whole new chain (gas token, bridging) — defer.
- **Aave "Stable Vaults"** (launched 2026-07-09): fixed-rate stablecoin product;
  no integration surface yet — watch.

## Borrow legs / leverage loops — out of scope (separate strategy)

Considered during the 0.4.0 review and explicitly deferred. Adding borrow legs turns the
rotator into a leveraged strategy with a different risk profile (liquidation, funding),
which is a much larger scope and doesn't belong in a floating-APY *rotation* path. If
pursued, it should be its own strategy, not a rotator feature.
