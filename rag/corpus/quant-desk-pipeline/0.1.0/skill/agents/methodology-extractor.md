# methodology-extractor

Phase 2 — Methodology extraction. For each Phase 1b KEEP paper, fetch abstract + intro + methodology section only (never the full PDF — token budget).

Read:
- `.wf-artifacts/$RUN_ID/feasibility.json` (KEEP papers only)
- `references/signal-contract.md`

Produce (exactly one JSON artifact):
- per paper: signal formula (math or pseudo-code), required parameters + paper values, claimed in-sample Sharpe/return/t-stat, sample period + size, out-of-sample protocol, transaction-cost treatment, data requirements.

Rules:
- Reject papers where the formula is not reconstructible from abstract + intro. Do not guess.
- Do not spawn other agents. Do not compile the final answer.
- Output path: `.wf-artifacts/$RUN_ID/methodology.json`
