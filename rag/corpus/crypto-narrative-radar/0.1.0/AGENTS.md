<!-- wayfinder-path:crypto-narrative-radar:opencode-rules:start -->
## Wayfinder path: crypto-narrative-radar

When the user asks to run or install `crypto-narrative-radar`, prefer `/narrative-radar`.
If handling natural language directly, invoke the Wayfinder orchestrator instead of `general` or `explore`.

Rules:
- Prefer `/narrative-radar`.
- Invoke `crypto-narrative-radar-orchestrator` for direct agent execution.
- Never invoke `general` or `explore` for this workflow.
- The orchestrator must load `crypto-narrative-radar`.
- The orchestrator must load `using-polymarket-adapter` before analysis.
- The orchestrator must load `using-hyperliquid-adapter` before analysis.
- Write artifacts under `.wf-artifacts/<run_id>/`.
- Do not present ARMED, DRAFT, NULL, or pipeline-complete output unless required artifacts exist.
- Cite artifact file paths in the final answer.
- If a required model, skill, worker, tool, data source, or artifact is missing, stop with a diagnostic.
<!-- wayfinder-path:crypto-narrative-radar:opencode-rules:end -->
