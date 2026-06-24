# Job Compiler

Compile the selected hedge into a draft or armed rebalance job with positions, monitoring rules, rebalance thresholds, and measurable invalidation conditions. Emits a null job if the critic verdict is null.

## Inputs
- `.wf-artifacts/$RUN_ID/critic_verdict.json` (verdict and selected hedge)
- `.wf-artifacts/$RUN_ID/quant_results.json` (leverage and combo details)
- `.wf-artifacts/$RUN_ID/test_results.json` (half-life for monitoring thresholds)
- `inputs/constraints.yaml`
- `policy/default.yaml` (scheduler block)

## Output
- `.wf-artifacts/$RUN_ID/job.json`
