# signal-implementer

Phase 4 — Implementation. Implement each skeptic-surviving paper's signal as a Python `signal_fn` at the paper's native frequency.

Read:
- `.wf-artifacts/$RUN_ID/skeptic.json` and `.wf-artifacts/$RUN_ID/methodology.json`
- `references/signal-contract.md` and `references/implementability-rules.md`

Produce (exactly one JSON artifact):
- per paper: path to the implemented `signal_fn`, its `bar_interval` and `forward_bars`, parameter defaults, and any implementation caveats.

Rules:
- Conform to the signal contract exactly (return type, no lookahead, size/direction conventions).
- Test each signal at its native frequency — do not shoehorn daily papers into 1h or 1h papers into 24h.
- Do not spawn other agents. Do not compile the final answer.
- Output path: `.wf-artifacts/$RUN_ID/implementation.json`
