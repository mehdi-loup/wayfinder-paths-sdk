# Reporting format

## Final report structure

Produce a markdown report with these sections, in this order:

```
# Paper replication: <topic>

## Summary
- Papers discovered: N
- Skeptic-rejected: X (with code breakdown)
- Non-implementable: Y (with data-need breakdown)
- Replication attempted: Z
  - PASSED all 3 phases: A
  - PASSED Phase 1-2, FAILED walk-forward: B
  - FAILED Phase 1: C
  - FAILED Phase 2: D
- Overall reproduction rate: A/N

## Replicated (PASS)
For each: paper title + authors + year, signal spec summary, Phase 1 metrics,
Phase 2 backtest vs buy-and-hold, walk-forward stats, verdict.

## Iterated winners (Phase 5b — remedial)
For each signal whose 5b variant improved on the raw paper: parent signal,
fix template code (A/B/C/D/E), failure mode diagnosed, improved metrics,
interpretation of why the fix worked.

## Experimental winners (Phase 5c — exploratory)
For each 5c variant that improved on its Phase-5/5b parent: parent signal,
template code (F1/F2/F3/F4/F5), description, metrics, interpretation.
Mark the best single variant as `is_winner: true`.

## Generalization report (Phase 5d)
Only present when a Phase 5c winner had Sharpe ≥ 2.0 and triggered 5d.

### D1 — Per-symbol (universe extension)
Table with all tested symbols: return, Sharpe, MDD, trades, whether beat B&H.
Aggregate: % of symbols beating B&H, median Sharpe, top-3 symbols Sharpe.

### D2 — Portfolio (equal-weight)
Portfolio return/Sharpe/MDD/exposure/turnover, compared to EW buy-and-hold.
Flag turnover > 10k trades per 7 months.

### D3 — Time extension + walk-forward
- Data-availability check results (which sources support >200 days).
- If ≥ 300 days: train/test split Sharpes, pass/fail.
- If blocked: explicit statement of the constraint (e.g., "HL candle API 5000-bar cap").

## In-sample wins, out-of-sample losses (HOLD)
For each: what passed early, what failed at walk-forward. This is the
interesting failure mode — records the overfitting signature.

## Signal-doesn't-segment-returns (Phase 1 REJECT)
List with quartile spread actually measured. Short entries.

## Skeptic rejections
Grouped by primary red-flag code. One line per paper.

## Non-implementable
Grouped by data dependency (options, order book, fundamentals, etc.).
```

Keep the summary at the top so the user can read the topline without scrolling.

## Memory entry

After the run, save a memory entry of type `project` under `topic_<topic-slug>.md`:

```markdown
---
name: "Paper replication: <topic>"
description: "Replication of <N> <topic> papers: <A> passed, <B> overfit, <C> signal-failed, <D> skeptic-rejected."
type: project
---

**Topic:** <topic>
**Date:** <YYYY-MM-DD>
**Data window:** <start> → <end>
**Universe:** <symbols>

**Papers tested:** <list with verdict per paper>

**Non-obvious findings:**
- <anything surprising — e.g. "Moreira-Muir vol management FAILED — crypto funding spikes break the σ² denominator">
- <overfitting signatures spotted — which signals passed Phase 2 but flopped on walk-forward>

**Signals for downstream testing:** <any PASSED signals, with path to signal file>

**Do NOT retest:** <list of REJECTED signals so future runs skip them>
```

Add a pointer in `MEMORY.md`:

```
- [Paper replication: <topic>](topic_<topic-slug>.md) — <one-line summary with hit rate>
```

This is how the skill becomes cumulative — each run adds to a catalog that constrains future search.
