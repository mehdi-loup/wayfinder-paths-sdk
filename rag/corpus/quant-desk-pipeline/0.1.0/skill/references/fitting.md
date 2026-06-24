# Fitting

The fitting phase improves a signal and adapts it to crypto. It optimizes — so it MUST be disciplined by a train/test split, or it is just p-hacking.

## Train/test split (mandatory)

Split the locked fitting window chronologically, default ~70% train / ~30% test. Search/iterate freely on TRAIN; **select every config by its TEST-side metrics**, never by in-sample TRAIN performance. Report both. The wider universe and longer/bear history stay HELD OUT for robustness — do not touch them here.

## Grid search

### Calibrate the forward/return horizon FIRST (the most important step)
Before sweeping any other parameter, fit the **forward/return horizon** (`forward_bars` in `evaluate_signal`). This is the single most important grid step: the Phase-1 bucket diagnostic measures forward returns over that horizon, so at the wrong horizon a real signal looks dead and you cannot proceed. A paper's stated horizon was chosen for its source asset class — it almost never transfers to crypto unchanged. So the sequence is:

1. **Sweep `forward_bars` first** (with the signal at its paper params) to find the horizon(s) where the bucket diagnostic actually separates returns. Use ~log-linear steps, e.g. `{2h, 6h, 12h, 24h, 2d, 5d, 10d, 30d}` — not linear — and cut off unreasonable ranges (no 90d forward horizon).
2. **Then sweep the signal's own parameters** (lookback, threshold, vol window, smoothing) at the surviving horizon(s).

Never assume the paper's horizon and never skip this — a "nothing reproduced" verdict reached without a horizon sweep is invalid.

### What else to vary, and how
- **Prioritize the parameters that matter most.** If you must shrink the grid, keep the important axes dense and thin the rest.
- **Pick the right step format per parameter.** A volatility/threshold knob is usually linear or polynomial around a neighbourhood; a σ that can't be negative shouldn't be swept negative.
- **Cut off unreasonable ranges** — infer bounds from what the parameter means.

### Two budget tiers (pick from the first-pass result)
- **Tier A — neighbour check** (first pass already looked viable): ~3 cells around each key parameter. The job is *confirmation* — verify the result sits in a stable region, not on a fragile spike. Not exploration.
- **Tier B — full grid** (first pass underperformed): vary every parameter that could plausibly move the result — lookback/formation window, forward horizon, vol-estimation window, EMA half-life/smoothing, threshold/quantile, any structural knob. **No fixed cap on axes or cells** — scale to per-cell cost, not to a number. Benchmark: the TSMOM family was exposed by a ~224-cell grid across 3 axes (lookback × forward horizon × vol window). Under-exploring a cheap signal is the more common failure than over-exploring.

### Selection rule
- Every cell runs the **full harness** (bucket → backtest → walk-forward / test split) — never shortcut to Phase 1. Sort by test-side Sharpe, then Phase-1 spread.
- Promote **at most one cell**. Among test-passing cells, pick the **smallest deviation from the paper's params** — small moves generalize; large deviations are usually overfit even within the grid. Tag it `tuned` so the report distinguishes it from a paper-spec pass.
- If no cell passes the test split → mark `inconclusive` and go to the remedial templates below. The grid is not a license to crown an overfit cell.

### Anti-p-hacking guardrails
- Run the grid **once**. No re-grid after seeing remedial/experimental results.
- Fix the axis set before running; no adding parameters mid-run; no nudging values after seeing results ("50d was best, let me also try 45/55" → no).
- The selection rule and the test-split gate are non-negotiable regardless of grid size.

### Feasibility / don't stall
- **Dry-run first:** count the cells, time one cell, compute the ETA for the whole grid before launching.
- If it won't finish in budget: **narrow ranges or thin density** (don't drop an axis you believe matters), or break into rounds (coarse → refine).
- **Save partial results** — a bad ETA plus an abrupt timeout otherwise loses everything.
- No single step should run >10 min with no feedback; shorter rounds with progress beat one long opaque run.
- If the full backtest is too slow, screen with vectorized numpy, then re-run the full harness on the top survivors to confirm.

## Crypto adaptation

Academic signals are usually equity-derived. Adapt, don't transplant: incorporate funding rates, 24/7 bars, vol-regime gating, and perp-fee awareness. A signal that ignores funding/fees on crypto perps is usually mis-specified.

## Iteration templates

Diagnose the failure from the harness `phase2.metrics.checks`, then apply the matching template. Budget: max 3 templates per mode, max 2 modes, **6 variants per signal**.

**Remedial (fix near-misses):**
- **Excess turnover** (`trades_ok: False`, trades > 250): A1 EWMA-smooth the signal before ranking · A2 hysteresis bands (asymmetric entry/exit) · A3 absolute thresholds (drop rolling-rank).
- **Rare extreme activation** (trades < 15, Sharpe negative): B1 softer quantile (0.25 → 0.50) · B2 independent direction gate · B3 vol-regime wrap (low-vol only).
- **Direction** (Phase-1 monotone but Phase-2 Sharpe negative): C1 reverse sign · C2 long-only winning side · C3 use as a FILTER on a bidirectional base.
  - **MANDATORY — when Phase-1 spread > 150 bps AND Phase-2 Sharpe < 0, try C1 (sign flip) FIRST.** This combination almost always means the signal inverts from its source domain (equity/gold/FX) to crypto. It is one character to change and skipping it is the single most common iteration mistake.
- **Whipsaw** (30–250 trades, Sharpe −0.5 to −1.0, flips near regime shifts): D1 multi-horizon confirmation · D2 sustained-signal requirement · D3 magnitude threshold.
- **Just under Phase-1** (40–50 bps): E1 lookback at 2× and 0.5× · E2 EWMA formation · E3 orthogonalize (remove a secondary effect).

**Data-window caveat:** if ALL variants fail the same way (sit long, lose in drawdown, win in recovery), the fitting window is regime-homogeneous — stop trying standalone fixes and apply **C3** (filter on a known bidirectional strategy); report the signal as overlay-only.

**Experimental (push test-passing winners):** F1 add short side (highest-leverage default for long-only passers) · F2 multi-window ensemble · F3 cross-signal agreement gate (when ≥2 passers) · F4 direction-normalized composition · F5 position-hold hysteresis.

**Custom override:** one custom variant may replace a template slot when you have a specific hypothesis (record `fix_template: "custom:<name>"` + rationale); same 6-variant budget; not a license to grid-search.

Closed-form signals with no free params are the only grid-exempt case — log the skip. If nothing reaches a viable test-side result within budget, return `not_viable`.
