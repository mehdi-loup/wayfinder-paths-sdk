# Phase 4b — Universe selection (run BEFORE Phase 5)

## Purpose

BTC/ETH are the most efficient crypto markets — many published signals exist precisely because the *idiosyncratic* dispersion in alts is where edge lives. Testing only on BTC/ETH biases the harness toward concluding "nothing works." Phase 4b picks a paper-appropriate universe before Phase 5 locks it in.

The universe is decided **once**, deterministically, from a pre-screen — not re-tuned after seeing Phase 5 results. That's the line between universe selection and p-hacking.

## When to run

After Phase 4 (signal implementation), once per signal, before any Phase 5 backtest. The output of Phase 4b is the `symbols` list passed to `evaluate_signal(...)`.

## Procedure

### Step 1 — Build the candidate pool

Start with a fixed pool of 11 liquid crypto perps (CCXT Binance spot tickers, all with multi-year history):

```
BTC, ETH, SOL, AVAX, BNB, XRP, DOGE, LINK, MATIC, LTC, ADA
```

If the paper specifies a universe character (e.g. "high-volatility alts", "L1 tokens", "DeFi tokens"), bias the pool toward that character but always keep BTC and ETH in for baseline comparison.

### Step 2 — Pre-screen via the Phase 1 bucket diagnostic only

For each candidate symbol, run **only Phase 1** of the harness (`_bucket_forward_returns` + `_phase1_verdict`) on a **short pre-screen window** — the first ~25% of the available history. This is the "scout" pass: cheap, no backtest, just bucket spreads.

- Window: e.g. if the planned Phase 5 window is 2024-01-01 → 2026-04-15, the pre-screen runs on 2024-01-01 → 2024-06-30 only.
- Reason: keeps the rest of the window untouched for Phase 5 (no peeking at test data).

Record per-symbol: `mean_abs_spread_bps`, `monotone`, `tail_consistent`, `n_bars`.

### Step 3 — Pick the test universe

Three modes — choose based on the paper's framing:

**Mode A: per-symbol signal (default)** — paper expects the signal to work on each asset individually. Keep symbols whose pre-screen spread is `≥ 0.5 × phase1_min_spread_bps` (i.e. weakly suggestive, not yet passing). Cap at top 5 by spread. Always include BTC and ETH even if they don't make the cut, so the report can show the BTC/ETH-only counterfactual.

**Mode B: cross-sectional / basket** — paper is about ranking assets against each other (momentum cross-section, dispersion, etc.). Keep the full 11-symbol pool. The signal will rank within it; pre-screen is informational only.

**Mode C: paper specifies a universe** — paper explicitly says "test on small-cap" or "test on high-funding tokens". Use the paper's universe literally if possible; if the SDK can't access it, document the substitution and proceed with the closest equivalent.

### Step 4 — Pre-commit the universe

Write the chosen universe to `phase4b_universe.json`:

```json
{
  "signal_slug": "...",
  "mode": "A" | "B" | "C",
  "pool_considered": ["BTC", "ETH", "SOL", ...],
  "prescreen_window": ["2024-01-01", "2024-06-30"],
  "prescreen_results": [
    {"symbol": "BTC", "spread_bps": ..., "monotone": ..., "tail_consistent": ...},
    ...
  ],
  "selected_symbols": ["BTC", "ETH", "SOL", "AVAX", "LINK"],
  "reason": "Mode A: top-5 by pre-screen spread + BTC/ETH for baseline"
}
```

## Hard rules

- **Pre-screen window is held out from Phase 5.** Phase 5 starts at the first bar *after* the pre-screen ends. Walking the window backwards or extending it later is forbidden.
- **No re-running Phase 4b after seeing Phase 5 results.** If Phase 5 fails, the right next move is Phase 5b (iteration templates), not "let me try a different universe."
- **Universe is logged in the Phase 6 report.** The report must show which symbols were considered, which were picked, and the pre-screen spread for each — so a reader can spot whether the universe was cherry-picked.
- **Minimum universe size = 2.** A signal that only works on a single ticker is not a strategy, it's a coincidence.

## Failure modes

- **All symbols fail pre-screen badly.** If no candidate clears even the relaxed `0.5 × min_spread` bar, the signal probably doesn't have edge anywhere — but still run Phase 5 on the top-3 by spread so the final report has data to reason about. Flag as `prescreen_universe_thin` in the report.
- **One symbol dominates by 10×.** That's likely a data artifact (price gap, listing event). Investigate before including; if it stays, note it in the report.
- **Daily signals on alts with short listing history.** If the candidate symbol's CCXT history doesn't cover ≥ 2 years for a daily signal, drop it from the pool. (Mirrors the Phase 1b data-sufficiency rule.)
