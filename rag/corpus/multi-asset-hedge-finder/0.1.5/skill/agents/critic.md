# Critic

Validate the quant's hedge recommendations against policy gates and the null baseline. Issues a verdict: armed (ready to execute), draft (needs manual review), null (do nothing), or retry (broader search needed). Always compares hedging to doing nothing.

## Inputs
- `.wf-artifacts/$RUN_ID/quant_results.json` (top combos and null baseline)
- `.wf-artifacts/$RUN_ID/test_results.json`
- `.wf-artifacts/$RUN_ID/exposure_reader.json`
- `inputs/constraints.yaml`
- `policy/default.yaml` (decision, risk, null_state blocks)

## Output
- `.wf-artifacts/$RUN_ID/critic_verdict.json`
