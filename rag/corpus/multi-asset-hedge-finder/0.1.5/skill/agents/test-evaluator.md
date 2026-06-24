# Test Evaluator

Statistically test hedge candidates using cointegration (Engle-Granger), half-life of mean reversion, funding analysis, and blowout scoring. Interprets results and explains why each candidate is or is not a good hedge.

## Inputs
- `.wf-artifacts/$RUN_ID/candidates_direct.json`
- `.wf-artifacts/$RUN_ID/candidates_broad.json`
- `.wf-artifacts/$RUN_ID/exposure_reader.json` (portfolio returns and prices)
- `inputs/constraints.yaml` (check_frequency, hedge_priority)
- `policy/default.yaml` (decision block)

## Output
- `.wf-artifacts/$RUN_ID/test_results.json`
