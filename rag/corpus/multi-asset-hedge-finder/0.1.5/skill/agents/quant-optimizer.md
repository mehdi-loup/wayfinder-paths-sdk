# Quant Optimizer

Build and rank hedge combos (1-leg through max_hedge_legs). Computes hedge ratios via least-squares regression, variance reduction, net beta, blowout scores, and safe leverage per leg. Presents top 3-5 options with cost vs tightness tradeoffs.

## Inputs
- `.wf-artifacts/$RUN_ID/test_results.json` (tested candidates)
- `.wf-artifacts/$RUN_ID/exposure_reader.json` (portfolio returns)
- `inputs/constraints.yaml` (max_hedge_legs, min_leg_notional_usd, check_frequency, hedge_priority)
- `policy/default.yaml` (decision, risk, leverage_backtest blocks)

## Output
- `.wf-artifacts/$RUN_ID/quant_results.json`
