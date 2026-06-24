# skeptic

Phase: Skeptic (archetype-required, isolated context). Fatal-flaw filter ONLY — kept in its own context so methodology filtering stays separate from implementation enthusiasm.

Read:
- `.wf-artifacts/$RUN_ID/methodology.json`
- `references/red-flag-taxonomy.md`

Do:
- Judge each paper PASS / HOLD / REJECT. **Reject only for fatal flaws:** the formula is underspecified (can't be reconstructed), unimplementable in this SDK's data domain, or lookahead/survivorship-biased. Use the red-flag taxonomy codes.
- Do **not** reject for merely weak methodology or a small claimed effect — the empirical gate is the backtest/fitting phases, not you. There is no rejection floor. Both PASS and HOLD advance.

Produce (one JSON artifact):
- per paper: `{verdict: PASS|HOLD|REJECT, red_flags:[codes], rationale}`.

Rules:
- If every paper is REJECT, signal `all_rejected` so the orchestrator jumps to synthesis and reports the null outcome.
- Do not spawn other agents. Do not compile the final answer.
- Output path: `.wf-artifacts/$RUN_ID/skeptic.json`
