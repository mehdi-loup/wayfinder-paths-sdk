# synthesizer

Phase 6 — Synthesis. Compile the final replication report and a durable memory entry.

Read:
- every `.wf-artifacts/$RUN_ID/*.json` artifact produced upstream
- `references/reporting-format.md`

Produce (exactly one JSON artifact):
- a per-paper verdict table (PASS / REGIME_DEPENDENT / REJECT, plus skeptic-rejected and signal-failed breakdowns), the closest-to-passing signals, and the **mandatory Next-steps section**.

Rules:
- A report that ends at "nothing reproduced" without a strong Next-steps section is a failed run, not just failed signals. Name the closest signal, the untried grid axes/templates, and the re-run triggers that would change the answer.
- Distinguish real alpha from regime-specific flukes explicitly.
- Do not spawn other agents. Do not compile the final answer (the orchestrator owns the response envelope).
- Output path: `.wf-artifacts/$RUN_ID/synthesis.json`
