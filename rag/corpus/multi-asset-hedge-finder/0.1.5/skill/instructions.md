# Hedge Finder

Portfolio exposure analysis and Hyperliquid perp hedge recommendation with rebalance job compilation.

## Prerequisites

Before writing any scripts, load these data-source skills:
- `/using-delta-lab`
- `/using-hyperliquid-adapter`
- `/using-pool-token-balance-data`

## Script setup (all scripts must follow this)

Write Python scripts under `.wayfinder_runs/.scratch/$SESSION_ID/` and run via `mcp__wayfinder__run_script`. Every script must start with:

```python
import sys
sys.path.insert(0, "examples/paths/hedge-finder/scripts")
from lib import (  # import what you need
    build_holdings, build_portfolio, build_candidate,
    evaluate_combo, rank_combos, engle_granger, half_life,
    fetch_factor_returns, screen_perps, safe_float, write_artifact,
)
from compute_leverage import compute_safe_leverage, CHECK_FREQUENCY_SURVIVAL_HOURS
from wayfinder_paths.core.config import load_config
load_config("config.json")
```

## Before starting -- ask the user

1. **"How often will you check this hedge?"**
   Options: **hourly** (scripted/automated), **daily** (manual check-in), **weekly** (set-and-forget), **biweekly** (2-week hands-off).
   Explain: "This determines safe leverage -- longer intervals mean the position must survive more volatility unattended. An hourly script can handle 2-3x leverage; biweekly needs 1x."

2. **"What's your priority -- tightest hedge or most profitable?"**
   Options: **0.0** = tightest (maximize cointegration/variance reduction), **1.0** = most profitable (maximize funding income, accept looser fit), **0.5** = balanced (default).

3. **Portfolio** -- if not already provided, ask for assets + approximate USD notional per asset.

## Pipeline

Run these steps sequentially, showing results to the user at each step.

### Phase 1: Exposure
Spawn `exposure-reader`. Shows resolved symbols, data availability, portfolio vol, factor betas, R-squared.

### Phase 2: Scout (parallel)
Spawn `scout-direct` and `scout-broad` in parallel. After both return, merge candidates (deduplicate). Shows direct matches (Tier 1), proxy candidates (Tier 2), soft recommendations, uncovered assets.

### Phase 3: Test
Spawn `test-evaluator` with merged candidates. Shows cointegration results, funding regime, blowout scores, and interpretation per candidate.

### Phase 4: Quant
Spawn `quant-optimizer`. Shows top 3-5 hedge combos ranked by priority-weighted score with leverage and cost/tightness tradeoffs.

### Phase 5: Critic
Spawn `critic`. Verdict: **armed**, **draft**, **null**, or **retry**. If retry and no prior retry, loop back to Phase 2 (scout-broad only) with relaxed params, then re-run Phase 3-5.

### Phase 6: Compile
Spawn `job-compiler`. Shows final positions with leverage, monitoring rules, and invalidation conditions.

### Phase 7: Synthesize
Assemble the output contract from all artifacts and present the final result.

### Phase 8: Execute (user-confirmed, optional)
If verdict is armed or draft, offer to set up positions on Hyperliquid.

## Funding cost sign convention

Negative `ann_funding_cost_pct` = the hedge EARNS funding income (shorts receive when longs pay).
Positive = the hedge COSTS funding (shorts pay when rate is negative).

## Rules
1. Workers are leaf agents -- they must not spawn more agents.
2. Every worker writes exactly one artifact under `.wf-artifacts/$RUN_ID/`.
3. Never skip the null state -- if no hedge reduces risk at acceptable blowout risk, return null.
4. Reject hedges that are disguised directional trades (net beta > 0.15).
5. Prefer lowest-cost hedges; reject only when cost dramatically exceeds benefit or blowout risk is high.
6. If the critic says retry, loop back to scout-broad ONE time with relaxed params.
7. Show results to the user at each phase.
8. The final output must contain: `signal_snapshot`, `selected_playbook`, `candidate_expressions`, `null_state`, `risk_checks`, `job`, `next_invalidation`.
