# skeptic

Validate the best signal config with quantitative rigor. Reject if it fails any check.

Read:
- `.wf-artifacts/$RUN_ID/data.pkl` (prices and funding)
- `.wf-artifacts/$RUN_ID/signal_research.json` (best config from signal-researcher)

## Task

Write and run a script that takes the `best_config` and runs four rejection tests:

### 1. Hidden beta check

Regress the strategy's OOS returns against the equal-weight universe return. If R-squared > 0.50, the strategy is disguised directional beta — reject or flag.

```python
from numpy.linalg import lstsq
strategy_rets = equity_curve.pct_change().dropna()
market_rets = prices.mean(axis=1).pct_change().dropna()  # equal-weight universe
X = np.column_stack([np.ones(len(market_rets)), market_rets.values])
beta_coefs = lstsq(X, strategy_rets.values, rcond=None)[0]
residuals = strategy_rets.values - X @ beta_coefs
r_squared = 1 - np.var(residuals) / np.var(strategy_rets.values)
```

### 2. Fee sensitivity

Re-run `run_walk_forward()` at fee rates [0, 0.00045, 0.0015, 0.003] (0, 4.5, 15, 30 bps). If OOS Sharpe halves between 0bps and 15bps, the edge is too thin to survive real execution costs.

### 3. Parameter robustness

Perturb the best config's lookback and entry_z by ±20% and re-run. If OOS Sharpe drops below 1.0 for any neighbor, the config is a narrow overfit peak.

### 4. Concentration check

From the P&L decomposition in the signal research artifact: if any single pair contributes >70% of OOS P&L, flag as concentrated. Not an automatic rejection, but a risk the orchestrator must disclose.

## Output

Write `.wf-artifacts/$RUN_ID/skeptic.json` containing:

- `hidden_beta`: {r_squared, beta, verdict: "pass"|"fail"}
- `fee_sensitivity`: {results_by_fee_rate, verdict: "pass"|"fail"}
- `parameter_robustness`: {neighbor_results, min_oos_sharpe, verdict: "pass"|"fail"}
- `concentration`: {max_pair_pct, pair_name, verdict: "pass"|"warn"|"fail"}
- `null_state_comparison`: {strategy_oos_sharpe, do_nothing_return, verdict}
- `final_verdict`: "pass"|"fail"|"pass_with_warnings"
- `rejection_reasons`: list of strings (empty if pass)

## Rules

- Do not spawn other agents.
- Use `run_walk_forward()` from `scripts/lib.py` for all backtests.
- If any of checks 1-3 fail, final verdict must be "fail".
- If only check 4 warns, final verdict can be "pass_with_warnings".
- Always compare against the null state (doing nothing = 0% return, 0 Sharpe).
