# stablecoin-yield-rotator — TODO

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
re-routes to human review (execution tier). Don't start until 0.4.0 clears review.

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

## Yield-bearing stable wrappers (sUSDe / sDAI) — 0.5.0 Tier 2 venue candidate

ERC-4626 / rebasing stable wrappers. Unlike Pendle these actually **fit** the
lend/unlend rotation model — deposit → share token → redeem near principal anytime —
so principal isn't locked and atomic withdraw→redeposit still holds. A more natural
first venue expansion than Pendle. Currently a stated README limitation ("no
yield-bearing stable wrappers"). Medium effort: a new venue scanner + share/redeem leg,
ranked on the wrapper's own yield. Note sUSDe carries Ethena protocol risk on top of
venue risk (same caveat already flagged for USDe-as-lend-asset). Needs tests + a
README/limitations update, re-routes to human review (execution tier).

## Borrow legs / leverage loops — out of scope (separate strategy)

Considered during the 0.4.0 review and explicitly deferred. Adding borrow legs turns the
rotator into a leveraged strategy with a different risk profile (liquidation, funding),
which is a much larger scope and doesn't belong in a floating-APY *rotation* path. If
pursued, it should be its own strategy, not a rotator feature.
