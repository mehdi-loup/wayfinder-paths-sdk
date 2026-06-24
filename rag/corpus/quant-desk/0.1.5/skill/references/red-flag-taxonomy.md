# Red-flag taxonomy

Codes emitted by the skeptic. Each has a short justification so the reasoning is auditable later.

| Code | Description | Why it matters |
|---|---|---|
| `SHARPE_TOO_HIGH` | Reported Sharpe > 3 | True Sharpes above 3 are extraordinary even at daily scale. In published equity factor work > 3 is almost always overfit or dataset-specific. |
| `NO_OOS` | No out-of-sample / holdout protocol | Without OOS, results are in-sample tuning. Not informative about forward expectation. |
| `SMALL_SAMPLE` | < 1000 observations | Under 1000 points, bucket statistics are noisy. Crypto 7-month hourly has ~5000 — a paper on 3 years of monthly data = 36 points. |
| `WEAK_TSTAT` | t-stat < 3 on reported factor | Below 3 is below modern multiple-testing-corrected significance thresholds. Harvey-Liu-Zhu (2016) argue we should require t > 3 to counteract publication bias. |
| `MULTIPLE_TESTING` | Tests > 5 specs, reports best | Data mining. Reported p-value overstates evidence. Should use Bonferroni or FDR. |
| `UNREPORTED_COSTS` | No transaction cost assumption | Paper ignores friction. For high-turnover signals the real-world return can flip sign. |
| `FUNDAMENTAL_DATA` | Requires earnings / macro / analyst data | Data transfer problem: crypto has no earnings; macro data is monthly lag. |
| `NEEDS_OPTIONS` | Uses IV / skew / variance risk premium | We don't have options data. Non-implementable. |
| `NEEDS_ORDERBOOK` | Uses L2 / bid-ask / microstructure | We don't have order book history. Non-implementable. |
| `SURVIVORSHIP` | Tests on "surviving" asset universe | Omits delisted/dead coins; biases returns upward. |
| `LOOKAHEAD_SMELL` | Signal uses data from t+1 or later in paper spec | Immediate fatal flaw. |
| `UNDERSPECIFIED` | Key parameters omitted or described hand-wavily | Not replicable — don't guess. |
| `HORIZON_MISMATCH` | Paper's horizon (e.g. monthly) far from ours (24h) | Signal decay curves make transfer uncertain. |
| `DATA_DOMAIN_MISMATCH` | Paper: equities / FX / commodities. Ours: crypto | Market microstructure differs materially. Many equity anomalies don't port. |

## How the skeptic should apply these

Red flags stack. 1 flag = probably HOLD. 2 flags = REJECT. `NEEDS_OPTIONS`, `NEEDS_ORDERBOOK`, `UNDERSPECIFIED`, `LOOKAHEAD_SMELL` → REJECT alone regardless of other scores.

Codes appear in the `red_flags` array of the skeptic's verdict JSON.
