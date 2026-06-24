# Reporting format

## ⚠️ NON-NEGOTIABLE: every report ends with a "Next steps" section

**This is the most important rule in the skill.** A report that ends at "nothing passed" without a Next steps section is a failure of the run, not just of the signals.

The Next steps section is **MANDATORY** in every report — no exceptions:
- Even when every signal failed Phase 1.
- Even when the skeptic rejected every paper.
- Even when Phase 1b pruned the entire pool.
- **Especially when no results were found** — that's exactly when the user needs direction on what to do next.

A 0-reproduction run is possible but **should be strongly avoided**. Before reporting it, confirm: Phase 5a grid search was run for every tunable paper, Phase 5b exhausted its 6-variant budget for every diagnosable failure, and the agent-judgement override was used where the standard templates didn't fit the failure mode. Thoroughness in 5a/5b is the protection against false-negative reports.

If the report still lands at 0 reproductions after that, the Next-steps section must be **exceptionally strong**: (1) which signal got closest (Phase 1 spread, Phase 2 Sharpe, Phase 3 train/test); (2) what specifically to try next — named grid axes that weren't tested, named templates not yet applied, custom variant hypotheses; (3) what would change the answer (re-run triggers: alt-heavy universe, longer bear-inclusive window, different topic scoping). Without those three, the run is wasted work and you should iterate further before writing the report.

## Flags to surface in the report

- **Inverted edge** — if Phase 1 reports `inverted_edge: true` for any signal (i.e. `-signal` carries the spread instead of `+signal`), call it out in the signal's row of the scoreboard. This is a *finding*, not a failure: the paper's direction is wrong for crypto, but the magnitude is real. Recommend iteration template C2 (direction inversion) for that signal.
- **Low-frequency warning** — if Phase 1b tagged a paper with `frequency: low_freq_warn` (weekly/monthly horizon), prefix that signal's row with `⚠️ low-freq` so the reader knows to discount the result regardless of pass/fail.

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

## Universe (Phase 4b)
For each signal: pool considered, pre-screen spread per symbol, selected
universe, mode (A per-symbol / B basket / C paper-specified). Reader must
be able to verify the universe was not cherry-picked.

## Per-phase scoreboard
Table — one row per paper, one column per phase. Even failing signals get
a row so the reader can scan partial progress at a glance:

| paper | variant | bar_interval | universe | P1 spread (bps) | P1 monotone | P2 sharpe | P2 trades | P3 train/test | verdict |
|-------|---------|--------------|----------|-----------------|-------------|-----------|-----------|---------------|---------|
| ...   | paper-spec | 1h        | BTC,ETH  | 38.2            | False       | n/a       | n/a       | n/a           | P1 FAIL |
| ...   | 5a-tuned (lookback=60) | 1h | BTC,ETH | 71.4 | True | 0.82 | 188 | 0.61/0.55 | PASS |

The `variant` column distinguishes `paper-spec` (paper's verbatim parameters), `5a-tuned` (grid-search winner, include the deviation in parens), and `5b-<template>` (e.g. `5b-A1`, `5b-custom:fat-tail-threshold`). Always include the paper-spec row even when a 5a/5b variant beat it — the contrast is the finding.

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

## Next steps (MANDATORY — STOP AND WRITE THIS SECTION)

If you are about to end the report without this section, **stop**. This is the most important section in the document. The user reads the report top-down; they need to leave with concrete actions, not a dead end. Write this section *especially* when nothing passed — that is the case where the user most needs guidance.

This section runs regardless of outcome. Sub-sections:

### Closest-to-passing signal(s)
For each failure bucket, name the single closest signal and the specific
metric that gated it. Examples:
- "Best Phase-1 failure: `paper_xyz` — spread = 42 bps (threshold 50), monotone=True.
  Within ~16% of passing; would likely clear on a longer pre-screen window or
  with an alt-heavy universe."
- "Best Phase-2 failure: `paper_abc` — sharpe = 0.41 (threshold 0.5), beat
  baseline by 0.6 (above threshold). Trade count 312 over budget of 250."

### Recommended iterations
For each closest-to-passing signal, name the iteration template(s) from
`fitting.md` (remedial A–E / experimental F1–F5) that match the
failure mode. Do not stop at "could iterate" — name the template code:
- "Apply template **A2** (trade-count cap via signal smoothing) — paper_abc had
  excess turnover, A2 historically halves trade count without killing Sharpe."
- "Apply template **C2** (direction inversion) — paper_def had Phase 1 monotone
  but Phase 2 sharpe negative; the gate is fighting the actual edge."

### Re-run triggers
Conditions that would change the answer enough to warrant a fresh run:
- Universe — "test window was pre-bull; re-run on a regime-mixed window."
- Data — "Phase 4b alt pool was thin; if [coin] gets listed on Binance, retry."
- Methodology — "skeptic rejected 4 papers for lookahead; if a corrected
  formulation surfaces, retest."

### Per-paper post-mortem
One paragraph per paper covering: what we hoped, what happened, what's the
single sentence you'd say to the paper's author. Keeps the memory entry
informative even for the papers that didn't work.
```

Keep the summary at the top so the user can read the topline without scrolling. The Next steps section is non-negotiable — even an empty pipeline emits a "Next steps" with what the discovery agent tried and what topic narrowing would help.

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
