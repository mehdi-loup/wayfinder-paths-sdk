# Spread Radar Reference

Use this skill when the user names a theme or asset universe and wants to discover a relative-value spread trade (not a directional bet).

Read `RESEARCH.md` for validated results and methodology. Read `references/signals.md` for the signal formulas. Read `references/risk.md` for the skeptic's quantitative checks.

All quantitative work (pair scoring, signal generation, backtesting) MUST use the library functions in `scripts/lib.py` and the backtesting framework at `wayfinder_paths.core.backtesting`. Do not reinvent these formulas — import them.

## Prerequisites

Before writing any scripts, load these data-source skills:
- `/using-delta-lab`
- `/backtest-strategy`

## Script setup (all scripts must follow this)

Write Python scripts under `.wayfinder_runs/.scratch/$SESSION_ID/` and run via `mcp__wayfinder__run_script`. Every script must start with:

```python
import sys
sys.path.insert(0, "examples/paths/spread-radar-reference")
from scripts.lib import (  # import what you need
    fetch_universe, score_pair, select_pairs, check_pair_stability,
    gen_velocity, backtest, run_walk_forward,
)
from wayfinder_paths.core.config import load_config
load_config("config.json")
```

## Pipeline

You are the orchestrator. Run these steps **sequentially, showing results to the user at each step**. Do not bury all work inside a single agent call.

### Step 1: Build universe
Spawn `universe-builder` with the user's theme. Show the user which symbols were found and how many days of data are available.

### Step 2: Screen pairs
Spawn `pair-screener` with the universe data. Show the user:
- Top 10 pairs ranked by score (with half-life and cointegration p-value)
- Which pairs are classified as stable vs drift
- The selected baskets

### Step 3: Quick baseline
Write and run a short script yourself (do not spawn an agent) that tests the top stable basket with a single config: `run_walk_forward(prices, funding, stable_pairs, lb=120, ez=1.5, leverage=1.5)`. Show the user the OOS Sharpe and return. This gives a fast first signal before the full sweep.

### Step 4: Signal sweep
Spawn `signal-researcher` with the pair baskets. This runs the full parameter grid. Show the user the top 5 results per basket and the best combined config.

### Step 5: Validate
Spawn `skeptic` with the best config. Show the user each check result:
- Hidden beta: R² = X.XX → pass/fail
- Fee sensitivity: Sharpe at [0, 4.5, 15, 30] bps → pass/fail
- Parameter robustness: neighbor Sharpes → pass/fail  
- Concentration: top pair % → pass/warn/fail

### Step 6: Synthesize
Present the final result with:
- The winning config (pairs, parameters, leverage)
- OOS Sharpe, return, max drawdown
- Skeptic verdict and any warnings
- How to reproduce: "Run `scripts/reproduce.py` or build a live strategy (see RESEARCH.md)"
- If skeptic failed: null state with rejection reasons

## Rules

1. You are the orchestrator. Show intermediate results to the user — don't hide everything in one agent.
2. Workers (universe-builder, pair-screener, signal-researcher, skeptic) are leaf agents that write and run scripts. They must not spawn more agents.
3. Every script imports from `scripts/lib.py`. Never re-implement signal or pair scoring functions.
4. Scoring is by Sharpe ratio from the backtesting framework, not heuristic weights.
5. Walk-forward validation (60/40 split) is required. Train-only results are not valid.
6. If the skeptic rejects all candidates, the output is null state. Never force a trade.
