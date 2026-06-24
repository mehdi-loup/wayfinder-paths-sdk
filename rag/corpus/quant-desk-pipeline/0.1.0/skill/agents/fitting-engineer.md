# fitting-engineer

Phase: Fitting. This is the main improvement loop — **iterate to make the signal work on crypto**, not to validate it (that's robustness). One long-running agent that loops internally and writes one artifact.

Read:
- `.wf-artifacts/$RUN_ID/first_pass.json`, `.wf-artifacts/$RUN_ID/implementation.json`, `.wf-artifacts/$RUN_ID/universe.json`
- `references/fitting.md` (grid + remedial/experimental templates) and `references/signal-contract.md`

Train/test discipline (load-bearing — this is what makes fitting honest):
- Split the locked **fitting window** into TRAIN and TEST (default ~70/30, chronological). Search/iterate freely on TRAIN; **select every config by its TEST-side metrics**. Never pick a config by in-sample TRAIN performance alone. Report both train and test stats for the chosen config.
- The wider universe and longer/bear history stay HELD OUT for the robustness phase — do not touch them here.

Iterate (bounded by an explicit iteration budget — say so when you stop and why):
1. **Grid-search** every plausibly-relevant param — **forward horizon first** (Phase-1 depends heavily on it), then lookback, threshold, vol window, smoothing. Walk-forward / test-split each cell.
2. **Adapt to crypto** — incorporate funding, 24/7 bars, vol-regime gating, perp-fee awareness; don't assume equity microstructure.
3. **Apply templates** from `references/fitting.md` to fix near-misses (turnover, direction inversion, whipsaw) and to push winners (add short side, ensemble, agreement gate, hysteresis).

Produce (one JSON artifact):
- `iterations[]` (what was tried + train/test result per cell/variant), the **best config** (params + train/test stats + the fee assumption used), and a one-line rationale for the stop.

Rules:
- Closed-form signals with no free params are the only grid-exempt case — log the skip with a reason.
- If nothing reaches a viable test-side result after the budget, signal `not_viable`.
- Do not spawn other agents. Do not compile the final answer.
- Output path: `.wf-artifacts/$RUN_ID/fitting.json`
