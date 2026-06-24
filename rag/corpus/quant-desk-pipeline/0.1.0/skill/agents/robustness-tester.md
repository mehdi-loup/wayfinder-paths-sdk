# robustness-tester

Phase: Robustness (OPTIONAL, last). Take the **fitted config unchanged** and stress it on data the fitting phase never saw. This is validation — do NOT re-tune here.

Read:
- `.wf-artifacts/$RUN_ID/fitting.json` (the locked best config) and `.wf-artifacts/$RUN_ID/universe.json`
- `references/robustness.md`

Do (only when the run is authorized to include robustness):
1. **Expand the window** — extend backward to include at least one bear regime not in the fitting window; 60/40 walk-forward.
2. **Expand tickers** — run the full universe, not just the fitting basket. Report per-symbol return/Sharpe/MDD and % beating buy-and-hold.
3. **Sweep fee tiers** — re-run at a few realistic maker/taker tiers; report how the edge degrades with cost.
4. Emit a **robustness report** + verdict: `PASS` (holds on held-out window/tickers and survives fees), `REGIME_DEPENDENT` (works in one regime only — tactical overlay, not universal), or `REJECT`.

If robustness is NOT authorized for this run:
- Emit a stub: `{"status": "skipped", "reason": "..."}` so the artifact exists and the pipeline can finalize. Do not run any heavy work.

Produce (one JSON artifact):
- the report (per-symbol table, fee-tier table, walk-forward train/test) + verdict, OR the skipped stub.

Rules:
- Changing the config here defeats the held-out test — if the fitted config fails, that is a finding, not a cue to re-tune.
- Do not spawn other agents. Do not compile the final answer.
- Output path: `.wf-artifacts/$RUN_ID/robustness.json`
