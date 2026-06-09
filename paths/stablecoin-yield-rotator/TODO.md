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

## Avantis (added) — follow-ups

avUSDC is wired in as an executable venue, but it's the junior tranche of a perp
DEX LP (not a lending market).

- [x] **Drawdown guard** — `_scan_avantis` freezes the row (`is_frozen=True`,
  `AVANTIS_MIN_TRAILING_APY`) when the trailing junior return is negative (NAV
  decline). Frozen rows stay visible in scans but are excluded as deposit /
  rotation targets. Note: this guards *entry*; an existing position in a
  drawing-down vault exits via normal rotation, not the freeze.
- [ ] Surface `maxRedeem` headroom in scan so the rotator knows when an exit would
  be **capped/locked** rather than discovering it at withdraw time.
