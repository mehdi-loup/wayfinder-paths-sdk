## When to use

The user wants to test whether academic signal research actually works in this SDK's data domain. Examples:

- "Find papers on funding-rate regime signals and test them"
- "Replicate time-series momentum research on crypto"
- "See if realized-skewness predicts crypto returns"

**Do NOT use for:** designing new signals, practitioner blog replication, strategy construction, or broad "research X phenomenon" asks.

## Pipeline

```
Phase 1  Discovery          (paper-finder agent, arxiv/SSRN/NBER)
Phase 2  Methodology        (paper-reader agent, formula extraction)
Phase 3  Skeptic             (isolated agent, fatal-flaw filter only)
Phase 4  Implementation     (Python signal_fn per paper)
Phase 5  Replication         (3-phase harness: quartile → backtest → walk-forward)
Phase 5b Remedial iteration  (fix Phase-2 near-misses: A/B/C/D/E templates)
Phase 5c Experimental iter.  (push winners: F1-F5 composition templates)
Phase 5d Generalization     (multi-symbol + multi-year CCXT + walk-forward)
Phase 6  Synthesis           (report + memory entry)
```

## Scope guarantees (locked)

- Topic must be a **specific signal family**, not a general phenomenon. See `references/topic-scope.md`.
- **Max 10 candidate papers** per run.
- **Academic sources only** (arxiv, SSRN, NBER, top journals, BIS/Fed/ECB). See `references/paper-sources.md`.
- **Forward horizon 24h**, fixed. Primary universe `["BTC", "ETH"]` at Phases 5-5c; Phase 5d extends to ~11 majors.
- Uses the **3-phase evaluation harness** at `scripts/evaluate_signal.py`. Do not build a new evaluator.
- Phase 5d MUST use CCXT Binance for multi-year data; HL candle API caps at 5000 bars (~200 days) which is too short for honest walk-forward.

## Expectations for the user

Before running, tell the user:
- Expected reproduction rate is low. Of ~10 papers found, 2-4 will be implementable, 0-1 will pass Phase 5 cleanly, and Phase 5d often reveals even passing signals are regime-dependent.
- **An honest "none replicated" report is a successful run.** The skill's value is honest failure analysis, not signal generation.
- End-to-end takes 15-25 minutes: discovery (2min) + extraction (3min) + skeptic (1min) + implementation (5-10min) + Phase 5 harness (~2min per signal) + Phase 5b/5c/5d on winners.

## Workflow (each phase produces one artifact)

Artifacts go under `$WAYFINDER_SCRATCH_DIR/paper_replication/<topic-slug>/`.

### Phase 1 — Paper discovery

Spawn an Agent (`subagent_type: general-purpose`) with this brief:

> Search academic sources for papers on [TOPIC]. Allowed sources: arxiv.org, papers.ssrn.com, nber.org, journal sites for JFE/RFS/JFQA/QJE/JoF. **Rejected sources**: Medium, Substack, Seeking Alpha, Twitter, practitioner blogs, Reddit.
> Return up to 10 papers as JSON: `[{title, authors, year, venue, abstract, url, relevance_score(0-1)}, ...]`.
> Prefer papers with published methodology and explicit signal formulas. Prefer recent (2010+) but include seminal older work if obviously relevant.

Save to `phase1_candidates.json`. Stop at 10 or after 2 minutes.

### Phase 2 — Methodology extraction

For each candidate, spawn an Agent to fetch abstract + intro + methodology section (**not the full paper** — token budget), extract:

- Signal formula (mathematical or pseudo-code form)
- Required parameters and their paper values
- Claimed in-sample Sharpe / return / t-stat
- Sample period + sample size
- Out-of-sample protocol (if any)
- Transaction-cost treatment
- Data requirements (asset class, frequency, auxiliary data)

Save to `phase2_methodologies.json`. Reject papers where the formula is not reconstructible from abstract + intro (do not guess).

### Phase 3 — Skeptic pruning

Spawn an Agent (`subagent_type: general-purpose`) with the **exact brief from `references/skeptic-brief.md`**. The subagent sees ONLY the methodologies, not Phase 1/2 excitement — this isolation is load-bearing.

Skeptic must return `{verdict: PASS/HOLD/REJECT, reasons: [string], red_flags: [taxonomy_codes]}` per paper.

**The skeptic filters fatal flaws only** (non-implementable, underspecified, lookahead-biased). It does NOT reject papers for merely weak methodology — the replicator (Phase 5) is the empirical gate. There is no rejection floor.

Save to `phase3_skeptic.json`.

### Phase 4 — Signal implementation

For **each PASS or HOLD paper** (both advance — weaker-methodology papers still get empirically tested), write a Python file under `$WAYFINDER_SCRATCH_DIR/paper_replication/<topic-slug>/signals/<paper_slug>.py` implementing:

```python
def signal_fn(prices: pd.DataFrame, funding: pd.DataFrame, lending: dict) -> pd.DataFrame:
    """Return DataFrame[timestamp × symbol] of signal values."""
```

Match the paper's spec exactly. If a parameter is not reported, mark the file `UNDERSPECIFIED` and skip — do not guess. See `references/signal-contract.md`.

### Phase 5 — Replication backtest

For each implemented signal: run `scripts/evaluate_signal.py::evaluate_signal(signal_fn, name)`. The harness runs the 3-phase gate protocol (quartile diagnostic → rolling-rank gated long-only → walk-forward) and returns `{verdict, metrics}`.

Save to `phase5_replication.json`. The harness takes ~2 minutes per signal.

### Phase 5b — Remedial iteration (fix near-miss signals)

For signals that passed Phase 1 (≥50 bps spread) but failed Phase 2, apply **pre-committed fix templates** from `references/iteration-catalog.md` based on the diagnosed failure mode.

**Diagnosis comes from `phase2.metrics.checks`:**
- `trades_ok: False` + trades > 250 → excessive turnover → templates A1/A2/A3
- trades < 15 + negative Sharpe → rare extreme activation → templates B1/B2/B3
- Phase 1 monotone but Phase 2 sharpe negative → direction inversion → templates C1/C2/C3
- Moderate trades, bouncing near sign flips → whipsaw → templates D1/D2/D3
- Just-under Phase 1 threshold → templates E1/E2/E3

**Budget:** max 3 templates per mode, max 2 modes per signal, max 6 variants per signal.

**Data-window override:** if ALL iterations fail similarly (standalone = sit long, lose in drawdown, win in recovery), the window is regime-homogeneous. Apply template **C3 (use as filter on known bidirectional strategy)**.

Save iteration results to `phase5b_iterations.json`.

### Phase 5c — Experimental iteration (push the winners)

For signals that PASSED Phase 2 (from raw Phase 5 or from Phase 5b remediation), apply **exploratory composition templates** from `references/experimental-iteration.md` to see if the winning signal can be pushed further:

- **F1 Add short side** — default try for any long-only PASS signal. Empirically the highest-leverage template.
- **F2 Multi-window ensemble** — smooth signal across 2-3 window lengths.
- **F3 Cross-signal agreement gate** — when ≥ 2 PASS signals exist, require both to agree.
- **F4 Direction-normalized composition** — standardize informative direction before summing.
- **F5 Position hold hysteresis** — reduce churn (case-dependent).

**Budget:** max 5 variants per PASS signal. Stop at budget.

Save to `phase5c_experimental.json`. The best variant becomes the "Phase 5c winner" and is the default subject of Phase 5d.

### Phase 5d — Universe and time extension (MANDATORY generalization test)

For any Phase 5c winner with Sharpe ≥ 2.0, run three generalization checks per `references/universe-extension.md`. **All three are required.** A Phase 5c winner without Phase 5d data should be logged as PROVISIONAL in memory and not claimed as a real strategy.

**D1 Universe extension:** test the winning config on ~11 liquid crypto perps individually via Hyperliquid candles or CCXT. Pass criteria: beats buy-and-hold on ≥ 80% of symbols, median Sharpe ≥ 1.0.

**D2 Portfolio test:** equal-weight the universe. Flag turnover > 10k trades/7mo (needs rebalance threshold in production).

**D3 Time extension + walk-forward:** primary data source **CCXT Binance** (hourly data back to 2017+ for majors — initialize with empty credentials since `fetch_ohlcv` is public). Minimum 2 years of data, 60/40 split. Use `scripts/fetch_ccxt_history.py`.

Pass criteria (ALL required):
- TRAIN Sharpe > 0 (strategy works in first half, not just second)
- TEST Sharpe ≥ 0.5 AND ≥ 50% of TRAIN Sharpe
- Full-window beats B&H on ≥ 50% of symbols

If TRAIN Sharpe is negative/near-zero but TEST is positive → verdict `REGIME_DEPENDENT`, not PASS. The strategy works in one regime only and is deployable as a tactical overlay gated by a regime classifier — never as a continuous strategy.

Save to `phase5d_generalization.json`. This phase has historically reversed "Phase 5c winners" — the M3 Hurst case showed Sharpe 3.54 on 7 months but 0.46 median on 2.5 years, revealing it as a bear-regime alpha, not universal.

### Phase 6 — Synthesis

Produce the final report per `references/reporting-format.md`. Seven groups to cover:

1. **Replicated (Phase 5 + 5d PASS)** — signals that passed Phase 5 AND survived multi-year Phase 5d walk-forward. Rare and significant.
2. **Regime-dependent (Phase 5d REGIME_DEPENDENT)** — Phase 5c winners whose multi-year test revealed they only work in one regime. Deployable as tactical overlay only.
3. **Iterated winners (Phase 5b)** — raw-paper failures rescued by structured fix templates.
4. **Experimental winners (Phase 5c)** — Phase 5 passes pushed further by composition templates.
5. **In-sample only** — passed Phase 1-2 but failed walk-forward (overfit signature).
6. **Signal-doesn't-segment** — failed Phase 1 quartile diagnostic.
7. **Skeptic-rejected / Non-implementable** — bucketed by red-flag code or data gap.

Save the memory entry per `references/reporting-format.md`. Any winner claim MUST include its Phase 5d verdict (or explicit PROVISIONAL flag if 5d not run).

## References

- [references/topic-scope.md](references/topic-scope.md) — what makes a topic narrow enough to accept
- [references/paper-sources.md](references/paper-sources.md) — allowed vs rejected sources
- [references/skeptic-brief.md](references/skeptic-brief.md) — **EXACT text** for the Phase 3 subagent
- [references/red-flag-taxonomy.md](references/red-flag-taxonomy.md) — methodology red flags + reasoning
- [references/implementability-rules.md](references/implementability-rules.md) — data-transfer rules (equity → crypto, etc.)
- [references/signal-contract.md](references/signal-contract.md) — Python signature and semantics
- [references/iteration-catalog.md](references/iteration-catalog.md) — Phase 5b: failure-mode → fix-template mapping + budget rules
- [references/experimental-iteration.md](references/experimental-iteration.md) — Phase 5c: composition templates (F1 add short, F2 ensemble, F3 agreement, F4 direction-normalize, F5 hysteresis)
- [references/universe-extension.md](references/universe-extension.md) — Phase 5d: universe test + portfolio + walk-forward protocol
- [references/reporting-format.md](references/reporting-format.md) — final report structure + memory entry format

## Scripts (shipped with the bundle)

- [scripts/evaluate_signal.py](scripts/evaluate_signal.py) — the 3-phase evaluation harness. Call `evaluate_signal(signal_fn, name)` to run a candidate signal through Phase 1 (quartile diagnostic) + Phase 2 (gated backtest) + Phase 3 (walk-forward). Thresholds are locked to prevent p-hacking.
- [scripts/fetch_ccxt_history.py](scripts/fetch_ccxt_history.py) — Binance multi-year OHLCV fetcher via CCXT. Use for Phase 5d time extension (HL candle API is capped at ~5000 bars so cannot support honest walk-forward). Returns DataFrame aligned across symbols, ready to pass into custom backtest code.

## Quickstart for the invoking agent

```
1. User gives a narrow topic (check references/topic-scope.md first; reject if too broad).
2. Spawn paper-finder Agent → phase1_candidates.json.
3. Spawn methodology-extractor Agent → phase2_methodologies.json.
4. Spawn skeptic Agent (EXACT brief from references/skeptic-brief.md) → phase3_skeptic.json.
5. For each PASS or HOLD paper, write signals/<slug>.py matching references/signal-contract.md.
6. Run scripts/evaluate_signal.py::evaluate_signal on each → phase5_replication.json.
7. For Phase-2 failures: apply references/iteration-catalog.md → phase5b_iterations.json.
8. For Phase-2 passes: apply references/experimental-iteration.md → phase5c_experimental.json.
9. For 5c winners (Sharpe ≥ 2.0): use scripts/fetch_ccxt_history.py for multi-year data,
   run references/universe-extension.md → phase5d_generalization.json. MANDATORY.
10. Write final report + memory entry per references/reporting-format.md.
```

## Memory interaction

Each run writes a memory entry under `topic_<slug>.md`. Future runs on similar topics should:
- Check memory first for do-not-retest signals.
- Check memory for known-working scaffolds/filters from prior runs.
- Update memory with the current run's findings, including Phase 5d verdict (or PROVISIONAL flag).
