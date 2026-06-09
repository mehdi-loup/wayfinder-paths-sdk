# stablecoin-yield-rotator — TODO

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
