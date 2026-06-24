# Experimental iteration (Phase 5c)

After Phase 5 and the structured Phase 5b iteration, any signal that has PASSED Phase 2 (full-window backtest) gets ONE more round of exploratory composition. This is distinct from Phase 5b:

- **Phase 5b** is remedial — fix-templates for signals that FAILED Phase 2.
- **Phase 5c** is aspirational — take signals that ALREADY PASSED and see if composition can push them further.

## Trigger

Run Phase 5c when any signal (from Phase 5 raw OR from Phase 5b iteration) has PASSED Phase 2 checks. HOLD verdicts at Phase 3 (due to walk-forward data constraints) still qualify — the Phase 2 evidence is enough.

Do NOT run Phase 5c when no signal passed Phase 2. There is nothing to build on.

## Fix templates

### F1 — Add short side (long/short)

**When:** any long-only signal that passed Phase 2.

**What:** mirror the long-entry gate on the opposite end of the signal distribution for shorts. If long when rank < 0.25, also short when rank > 0.75.

**Why it often helps:** most regime signals have informative tails on both ends. Paper implementations often only test long-only due to cross-sectional context (e.g., cannot short individual equities easily in some settings). Crypto perps can short freely.

**Record:** the M3 Hurst long/short case study — adding shorts took Hurst 30d from Sharpe 2.32 / +30% to Sharpe 3.54 / +79% in the BTC/ETH × 7-month window. Same drawdown envelope.

### F2 — Multi-window ensemble

**When:** signal computed from a rolling window where window length is a free parameter.

**What:** compute the signal at 2-3 different window lengths (e.g., 14d + 30d + 60d for Hurst), average them, and re-run the gate on the composite.

**Why it helps:** smooths out window-specific artifacts. If the edge is real, it should survive multiple lookbacks.

**Record:** M1 Hurst ensemble produced +0.3 Sharpe improvement over single 30d Hurst with lower MDD — modest but real.

### F3 — Cross-signal agreement gate

**When:** ≥ 2 Phase-5/5b PASS signals exist in the same run.

**What:** require BOTH signals to agree (both in their activation region) before entering a trade.

**Why it helps/hurts:** dramatically reduces false positives. In practice reduces exposure too far (M2 case: 1.7% exposure, 51 trades, low Sharpe despite +0 drawdown). Useful for risk-constrained capital allocation, not for Sharpe maximization.

### F4 — Direction-normalized composition (CRITICAL GOTCHA)

**When:** composing ≥ 2 signals of different families into a composite score.

**What:** BEFORE summing / averaging, normalize each signal so "activation region" corresponds to the same numerical direction. If signal A says "long when rank is LOW" and signal B says "long when rank is HIGH," sign-flip one before combining.

**Why it matters:** M4 in the VR/AC run naively summed Hurst (informative at LOW) and VR (informative at HIGH). The two canceled. Direction-normalize first.

**Practical check:** use the `favor_high` field from the harness's Phase 1 output to determine direction per signal.

### F5 — Position hold hysteresis

**When:** any signal producing > 200 trades per 7-month window (high turnover).

**What:** add stateful holding — once entered, require the gate to fully flip (cross quantile 0.5) before exiting, with minimum hold of N bars.

**Why it may help:** reduces churn and fee drag. But doesn't always improve Sharpe — can miss exit opportunities.

**Record:** M5 24h hysteresis in the VR/AC run made things slightly WORSE (Sharpe 1.98 vs base 2.32, more trades). Case-dependent.

## Budget

- Maximum 5 variants per Phase-5-PASS signal
- Stop after budget; no open-ended tweaking
- Only unique composition patterns; don't try F1 at three different quantile widths — that's Phase 5b's E templates

## Record-keeping

`phase5c_experimental.json` — one row per variant:

```json
{
  "parent_signal": "<name>",
  "template": "F1" | "F2" | "F3" | "F4" | "F5",
  "variant_description": "...",
  "metrics": {sharpe, total_return, max_drawdown, exposure, trade_count},
  "interpretation": "why it worked / didn't",
  "is_winner": true | false
}
```

Surface the best variant in the final synthesis report as a distinct entry labeled "Phase 5c experimental winner."
