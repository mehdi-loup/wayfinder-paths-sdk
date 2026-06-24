# Risk

This is a research path — its "risk" is **false-positive alpha**, not capital loss. The protections that keep a run honest:

- The 3-phase harness gates are **locked** (P1 spread ≥ 50 bps + monotone; P2 Sharpe ≥ 0.5 + beats baseline; P3 60/40 walk-forward test Sharpe ≥ 0.5 and ≥ 50% of train). Do not relax them mid-run to force a PASS.
- The skeptic runs in isolated context, filtering methodology before any implementation enthusiasm.
- Phase 5d multi-year, multi-symbol walk-forward is **mandatory** for winners — a signal that only works in one regime window is downgraded to `REGIME_DEPENDENT` (draft), never `armed`.
- Always rank the null-state (honest rejection) lane. A 0-reproduction run is a valid, fully-acceptable outcome and must ship a strong Next-steps section.
- If a winner cannot satisfy generalization, return `draft` or `null`, not `armed`.
