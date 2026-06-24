# Iteration catalog (Phase 5b)

After the harness runs Phase 5 on each paper signal, signals that had real Phase 1 edge (≥50 bps spread) but failed Phase 2 deserve targeted iteration. This is not "tweak until it works" — it is a **structured mapping from diagnosed failure mode to pre-committed fix templates**.

## Trigger

Iterate when:
- Phase 1 passed (spread ≥ 50 bps, tail-consistent or monotone)
- Phase 2 failed for a diagnosable reason (see taxonomy below)
- Not all paper signals in the current run already failed with the same mode

Do NOT iterate when:
- Phase 1 already failed (no effect in data — nothing to improve on)
- Phase 2 trades count is < 5 (signal just never activated — no diagnosis possible)
- All variants within a fix template have been tried already for this signal

## Failure-mode → fix-template mapping

Phase 5 emits `phase2.metrics.checks` which reveals *which* check failed. Use that to pick the template:

### 1. Excessive turnover (`trades_ok: False`, trades > 250)

Signal has real edge but the rolling-rank gate flips too often. Turnover eats the Sharpe.

**Fix templates:**
- **A1 EWMA smoothing** — apply `.ewm(halflife=N*24, min_periods=24).mean()` to the signal before ranking. Smooths high-frequency noise.
- **A2 Hysteresis bands** — stateful gate with ASYMMETRIC entry/exit thresholds (enter at z<-0.5, exit at z>+0.5). Reduces thrashing near threshold.
- **A3 Absolute thresholds** — replace rolling-rank with a fixed threshold on the signal value. Removes rank-drift churn.

### 2. Rare extreme activation (trades < 15, Sharpe negative despite few trades)

Signal fires only at tail extremes, and those extremes are exactly where we shouldn't trade (crash catches, rally shorts).

**Fix templates:**
- **B1 Softer quantile** — relax gate from q=0.25 to q=0.50 (take half the quartiles, not just the extreme). Increases activation rate away from tails.
- **B2 Direction gate** — combine with an INDEPENDENT directional filter (momentum, trend, funding). Only activate when direction aligns with regime context.
- **B3 Vol-regime wrapping** — only activate during low-vol periods (vol_zscore rank < 0.25). Avoids crash-catching.

### 3. Negative Sharpe despite passing Phase 1 monotone

Signal's edge exists in cross-section but flips direction when deployed as a time-series gate. Often indicates the "edge" is about which bucket *avoids* losses, not which *produces* gains.

**Fix templates:**
- **C1 Reverse direction** — sanity check with reversed `favor_high`. Should worsen if original was right; improve if wrong.
- **C2 Long-only variant** — take only the winning-side trades, skip the other direction. Good for asymmetric edges.
- **C3 Use as filter, not strategy** — if standalone fails, apply as a FILTER on a working strategy with bidirectional mechanics (e.g., MR-in-low-vol + this signal as direction gate).

### 4. Whipsaw (trades moderate 30-250, Sharpe -0.5 to -1.0, strategy flips direction frequently near regime shifts)

Signal direction changes during turbulent transitions; strategy enters on false starts.

**Fix templates:**
- **D1 Multi-horizon confirmation** — require two horizons (e.g., 7d AND 30d) to agree before acting.
- **D2 Sustained-signal requirement** — require signal persistence (e.g., same sign for 24h) before triggering.
- **D3 Magnitude threshold** — require absolute signal value above a minimum (e.g., |30d return| > 2%).

### 5. Just-under Phase 1 threshold (40-50 bps spread)

Signal is almost there. A small specification change may push it over.

**Fix templates:**
- **E1 Different lookback** — try the formation window at 2x and 0.5x original.
- **E2 EWMA formation** — replace fixed-window with exponentially-weighted; recent-biased.
- **E3 Orthogonalize** — remove a secondary effect (e.g., subtract cross-sectional mean, remove long-term drift).

## Cross-cutting rule: data-window caveat

If **all** iterations on a signal family fail with similar profile (standalone = sit long, lose money in drawdown, win in recovery), the window is regime-homogeneous (all bear or all bull) and no standalone signal will outperform without a short-side mechanism.

In that case, the meaningful iteration is **C3 (use as filter on a bidirectional strategy).** Do not continue trying standalone fixes. Instead, add the signal as a FILTER on the best-known bidirectional strategy (e.g., MR-in-low-vol, V17, etc. from memory). Report honestly that the paper's signal only produces value as an overlay.

## Iteration budget

- Maximum 3 fix templates per failure mode per signal
- Maximum 2 failure modes per signal (stop after 6 variants)
- If no iteration produces PASS, report the signal as REJECTED_AFTER_ITERATION with diagnosis + fix templates tried + why each failed

This prevents runaway exploration. If 6 variants can't rescue a signal, the paper's claim likely doesn't hold in this data at this frequency.

## Record-keeping

For each iteration attempt, record in `phase5b_iterations.json`:

```json
{
  "parent_signal": "<paper slug>",
  "fix_template": "A1" | "A2" | ...,
  "variant_file": "signals/<paper_slug>__A1.py",
  "failure_mode_diagnosed": "excessive_turnover" | ...,
  "verdict": "PASS" | "HOLD" | "REJECT",
  "metrics": {...},
  "interpretation": "..."
}
```

Surface the best-performing iteration in the final synthesis report as a distinct entry (not just a paper label).
