# quant-desk-pipeline

A `spread-radar`-archetype Wayfinder **pipeline path** that takes a **narrow academic signal topic** and returns an **honest replication verdict** — PASS, REGIME_DEPENDENT, or REJECT.

It is deliberately ordered to **get something simple working before spending compute on robustness**: research → simple first pass → fit → (optional) stress-test.

## Flow

1. **discovery** — find ≤10 papers AND feasibility-prune them in one pass (arxiv/NBER/journals; never SSRN)
2. **methodology** — extract the signal formula from abstract + intro
3. **skeptic** — fatal-flaw filter, isolated context
4. **implementation** — Python `signal_fn` at the paper's native frequency
5. **universe_builder** — resolve symbols + lock a small fitting basket (~3 symbols, ~1yr); owns the pre-screen
6. **signal_research (first pass)** — one simple backtest; fail-fast only if the signal is degenerate (broken impl / no trades), not if it merely underperforms
7. **fitting** — the improvement loop: grid-search (forward horizon first), apply remedial/experimental templates, adapt to crypto (funding, fees, 24/7, regimes), **with a train/test split inside the fitting window** so configs are picked by held-out test metrics
8. **robustness (optional)** — take the fitted config unchanged and stress it on **held-out** data: longer window (incl. a bear regime), full ticker universe, and a fee-tier sweep → robustness report
9. **synthesis** — report (PASS / REGIME_DEPENDENT / REJECT) + memory entry, Next-steps mandatory

## Why this shape

- **Simple first** — the first pass is a cheap fail-fast gate, not the full harness. The agent stops reaching straight for multi-year multi-symbol runs.
- **Fitting ≠ robustness** — fitting *optimizes* (grid/experiment/tweak) under a train/test split; robustness *validates* the frozen config on data it never saw. The held-out split is what keeps free iteration from being p-hacking. A fitted signal with robustness skipped is reported `PROVISIONAL`.
- **Lean** — 9 workers (down from 13), `pair_screener` folded into `universe_builder`, the four old iteration/generalization phases collapsed into `fitting` + `robustness`. Agent docs are self-contained so each worker loads little context.

## Structure

```
quant-desk-pipeline/
├── wfpath.yaml              # manifest (archetype, graph, inputs, 9 agents)
├── pipeline/graph.yaml      # the DAG + failure edges
├── policy/default.yaml      # fitting basket/window, train/test, robustness gates
├── inputs/                  # theme.md (required), universe.yaml, notes.md
├── schemas/                 # input slot schemas
├── skill/
│   ├── instructions.md      # lean orchestration spec
│   ├── agents/              # 9 self-contained worker docs
│   ├── references/          # topic-scope, signal-contract, fitting, robustness, …
│   └── scripts/             # evaluate_signal.py (3-phase harness) + fetch_ccxt_history.py
└── tests/{fixtures,evals}/  # output-shape, null-state, regime-gate, host-render
```

## Commands

```bash
wayfinder path doctor --path examples/paths/quant-desk-pipeline
wayfinder path eval   --path examples/paths/quant-desk-pipeline
wayfinder path activate --host opencode --scope project --path examples/paths/quant-desk-pipeline --include-dependencies
```
