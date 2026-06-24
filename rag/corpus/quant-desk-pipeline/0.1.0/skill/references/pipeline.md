# Pipeline

Quant-desk paper-replication flow on the `spread-radar` archetype. Ordered to get something simple working before spending compute on robustness.

Phases:
1. `intake` — load the locked topic + scope (orchestrator)
2. `discovery` — find ≤10 papers AND feasibility-prune them in one pass; reloop if fewer than 4 KEEP
3. `methodology` — extract the signal formula from abstract + intro
4. `skeptic` — fatal-flaw filter, isolated context (archetype node)
5. `implementation` — signal_fn per survivor at its native frequency
6. `universe_builder` — resolve symbols + lock the small fitting basket (owns the `pair_screener` pre-screen)
7. `signal_research` — **first pass**: one simple backtest, fail-fast only on degenerate output
8. `fitting` — iterate to improve + adapt to crypto, with a train/test split inside the fitting window
9. `robustness` — **optional**: held-out window/universe/fee stress → report
10. `synthesis` — replication report + memory entry
11. `finalize` — standard response envelope (orchestrator)

Failure policy:
- `discovery` thin pool reloops discovery (max 2)
- `skeptic` rejects everything → jump to `synthesis`
- `signal_research` degenerate → jump to `synthesis` (don't fit a broken signal)
- `fitting` finds no viable config → `synthesis`

Discipline:
- One JSON artifact per worker under `.wf-artifacts/$RUN_ID/`; the orchestrator reads them and owns final synthesis.
- Fitting may optimize freely but selects by a held-out test split; robustness is the unbiased out-of-sample test on data fitting never saw. A fitted signal without a robustness report is `PROVISIONAL`.
