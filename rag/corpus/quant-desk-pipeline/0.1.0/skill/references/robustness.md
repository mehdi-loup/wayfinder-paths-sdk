# Robustness

Optional final phase. Take the **fitted config unchanged** and test it on data fitting never saw. This is the honest out-of-sample check — re-tuning here defeats the purpose. Its job is to tell real alpha apart from a regime/cost-specific fluke.

## Three stress axes (held out from fitting)

1. **Window extension** — extend backward to include at least one bear regime not in the fitting window. 60/40 walk-forward. Pass: TRAIN Sharpe > 0, TEST Sharpe ≥ 0.5 and ≥ 50% of TRAIN.
2. **Universe extension** — run the full ticker pool, not just the fitting basket. Report per-symbol return/Sharpe/MDD/trades and % beating buy-and-hold. Pass: beats B&H on **≥ 80%** of symbols, median Sharpe ≥ 1.0. Also run an equal-weight **portfolio** over the pool and flag turnover > ~10k trades/window (needs a rebalance threshold in production).
3. **Fee-tier sweep** — re-run at a few realistic maker/taker tiers; report how the edge degrades with cost. An edge that only survives at zero/low fees is not deployable.

## Verdict

- **PASS** — holds on the held-out window and tickers, and survives realistic fees.
- **REGIME_DEPENDENT** — works in one regime/window only (e.g. TRAIN Sharpe ≤ 0 but TEST positive, or beats B&H on a minority of symbols). Deployable as a tactical overlay gated by a regime classifier, never as a continuous strategy. This phase has historically reversed fitted "winners" — e.g. a Hurst signal at Sharpe 3.5 on 7 months collapsed to ~0.5 median over 2.5 years.
- **REJECT** — does not hold out of sample or dies under fees.

## Skipped runs

When robustness is not authorized, emit `{"status": "skipped", "reason": "..."}`. A fitted signal without a robustness report is `PROVISIONAL` — never claimed as validated alpha in the final report.
