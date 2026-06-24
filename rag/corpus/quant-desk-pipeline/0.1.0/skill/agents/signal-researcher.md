# signal-researcher

Phase: First pass (the fail-fast gate). Get a **simple backtest working** before any fitting — do NOT grid-search or expand data here.

Read:
- `.wf-artifacts/$RUN_ID/implementation.json` and `.wf-artifacts/$RUN_ID/universe.json`
- `references/signal-contract.md`

Do:
- Run ONE backtest per signal at **paper-spec params** on the locked `fitting_basket` / fitting window via `scripts/evaluate_signal.py`. Report Phase-1 bucket spread + monotonicity and a quick Phase-2 Sharpe/trade count.

Produce (one JSON artifact):
- per signal: `{runs_ok, degenerate, spread_bps, monotone, sharpe, trades, notes}` and an overall `gate` verdict.

Gate semantics (important):
- **Fail-fast ONLY on degenerate output** — the signal didn't run, is constant/all-zero, produced NaNs, or generated ~no trades (an implementation bug, not a weak edge). In that case signal `degenerate` so the orchestrator skips fitting and reports the dud.
- A signal that runs cleanly but **underperforms paper-spec thresholds still PASSES the gate** — that is exactly what the fitting phase exists to improve. Do not reject it here.

Rules:
- No parameter sweep, no multi-year data, no universe expansion — that is fitting/robustness.
- Do not spawn other agents. Do not compile the final answer.
- Output path: `.wf-artifacts/$RUN_ID/first_pass.json`
