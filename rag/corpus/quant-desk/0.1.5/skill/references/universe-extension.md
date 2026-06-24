# Universe and time extension (Phase 5d)

After a signal has passed Phase 2 (and ideally improved via Phase 5c), test whether it **generalizes**. The 2-symbol Phase 5 test can pass by coincidence; a 10-symbol cross-section is much stronger evidence.

## Trigger

Run Phase 5d when any signal has a Phase 5c-stable configuration with Sharpe ≥ 2.0 on the BTC/ETH × 7-month window. Lower-Sharpe winners are not worth the 5d compute.

## Three checks

### D1 — Universe extension (always run)

**Universe:** test on the full set of liquid crypto perps available via Hyperliquid candles:
`["BTC", "ETH", "SOL", "HYPE", "BNB", "XRP", "DOGE", "AVAX", "LINK", "AAVE", "SUI"]`

**Per-symbol table:** for each symbol run the winning Phase 5c config as a standalone 1-symbol backtest. Record return, Sharpe, MDD, trade count. Compare to per-symbol buy-and-hold.

**Pass criteria (cross-sectional):**
- Beats buy-and-hold on ≥ 80% of symbols
- Median Sharpe ≥ 1.0 across the universe
- Top-3 symbols Sharpe ≥ 2.0

If only 1-2 symbols produce strong Sharpe, the signal is likely symbol-idiosyncratic, not a real alpha.

### D2 — Portfolio test (always run)

Equal-weight all symbols. Run the signal as a cross-sectional portfolio.

Record: portfolio Sharpe, MDD, return, exposure, turnover.

**Note:** portfolio Sharpe is typically LOWER than best-single-symbol Sharpe because diversification averages in weak symbols. This is expected. The meaningful check is **portfolio return > buy-and-hold portfolio return** and **portfolio MDD < individual-symbol MDD**.

Flag high turnover — portfolio M3 in VR/AC run had 11,105 trades over 7 months (~50/day) because each symbol independently generates signals. At 4 bps per leg that's ~8% annualized drag. If turnover > 10k trades, recommend adding a rebalance-threshold overlay in production.

### D3 — Time extension + walk-forward (MANDATORY)

**This is the single most important check in the whole skill.** Multi-year walk-forward is the difference between "found a real signal" and "found a regime-specific fluke." Short-window results (7 months) cannot distinguish these.

**Data source: CCXT Binance (primary).** Supports hourly candles back to 2017+ for BTC/USDT and most majors. Initialize with empty credentials since `fetch_ohlcv` is a public endpoint:

```python
from wayfinder_paths.adapters.ccxt_adapter import CCXTAdapter
adapter = CCXTAdapter(exchanges={"binance": {}})
# paginate fetch_ohlcv with since= cursor, limit=1000 per page
candles = await adapter.binance.fetch_ohlcv("BTC/USDT", "1h", since=start_ms, limit=1000)
```

**Symbols available via CCXT Binance spot (confirmed):**
BTC/USDT, ETH/USDT, SOL/USDT, BNB/USDT, XRP/USDT, LINK/USDT, DOGE/USDT, AVAX/USDT, AAVE/USDT. Perp suffix `:USDT` also supported but spot is usually cleaner for backtests.

**Fallbacks if CCXT unavailable:** HL candles (~200-day cap), Delta Lab daily intervals for some assets. Document if forced to fall back.

**Walk-forward protocol:**
- Minimum 2 years of data required (crypto has enough regime variation that shorter windows are fool's gold)
- 60/40 train/test split
- Run winning Phase 5c config on each half
- Pass criteria:
  - **TRAIN Sharpe > 0** (the strategy actually works in the first half, not just the second)
  - **TEST Sharpe ≥ 0.5** AND **TEST Sharpe ≥ 50% of TRAIN Sharpe**
  - **Full-window M3 beats B&H on ≥ 50% of symbols tested**

**The 4-regime honesty rule:** If train Sharpe is negative or near-zero but test Sharpe is strong, that is NOT a PASS — it means the strategy is regime-dependent, not universal. Report as `REGIME_DEPENDENT` verdict:

> Strategy works in the test window's regime (typically bear/choppy) but fails in the train window's regime (typically bull). Deployable only as a tactical overlay triggered by an independent regime classifier, not as a continuous strategy.

**Historical precedent baked in:** M3 Hurst long/short from the variance-ratio-ac topic run showed Sharpe 3.54 on a 7-month window but Sharpe 0.46 median over 2.5 years, with walk-forward confirming regime-dependence. The 7-month result was timing luck. Always run D3 before memorializing a winner.

## Report structure

Phase 5d output goes into the final synthesis report as its own section:

```
## Phase 5d generalization

### Per-symbol (Universe extension)
| Symbol | Return | Sharpe | MDD | Trades | vs B&H |
... table ...
- Beats B&H on X/N symbols (Y%)
- Median Sharpe: Z

### Portfolio (equal-weight)
| Metric | Value |

### Time extension / walk-forward
<results OR "BLOCKED by <reason>">
```

Also save `phase5d_generalization.json` with the per-symbol and portfolio metrics.

## Interpretation

- **D1 beats B&H on 80%+ with median Sharpe > 1.0 AND D3 PASS on ≥ 2-year data**: strong, generalizable signal — deployable.
- **D1 passes but D3 shows REGIME_DEPENDENT**: signal works in one regime only (e.g., bear). Deployable only as tactical overlay gated by an independent regime classifier. Do NOT run as continuous strategy.
- **D1 beats B&H on 80%+ but median Sharpe < 1.0**: real signal, weak magnitude. Probably needs composition with another strategy to be standalone-tradeable.
- **D1 beats B&H on < 80%**: likely symbol-idiosyncratic. Not a deployable strategy.
- **Walk-forward fails (test sharpe < 0.5)**: overfit to the short window. Degrade the claim fully.

## Mandatory caveat in memory entries

Any memory entry recording a Phase 5c winner MUST include the Phase 5d D3 verdict. Winners without D3 data are provisional only — future skill runs should flag and re-test.

## Memory note

When recording Phase 5d findings in memory, include the full per-symbol table. Future runs checking if a new topic produces a similar-signature winner can compare.
