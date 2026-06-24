# pair-screener

Screen all pairwise combinations for spread-trading suitability.

Read:
- `.wf-artifacts/$RUN_ID/data.pkl` (prices and funding from universe-builder)
- `policy/default.yaml` (candidate_rules section)

## Task

Write and run a script that:

1. Loads the prices DataFrame from the universe-builder artifact.
2. Splits data 60/40 into train and test periods.
3. For every pairwise combination of symbols, computes on the **train period only**:
   - `ou_half_life()` on the log spread
   - `engle_granger_p()` for cointegration
   - `score_pair()` composite score
4. For the top 15 pairs by score, also computes `check_pair_stability()` to test whether cointegration holds in both halves.
5. Selects two baskets:
   - **Stable basket** (3 pairs): diversified pairs where `stable=True` and lowest drift. These are for mean-reversion signals.
   - **Drift basket** (3 pairs): diversified pairs from top train-only scores that are NOT stable. These are for drift/divergence capture.
6. Writes `.wf-artifacts/$RUN_ID/pair_screen.json` containing:
   - `all_pairs`: full ranked list with score, half_life, coint_p
   - `stable_basket`: selected stable pairs with stability stats
   - `drift_basket`: selected drift pairs
   - `train_period`: [start, end]
   - `test_period`: [start, end]

All pair scoring functions are in `scripts/lib.py` — import `score_pair`, `select_pairs`, `check_pair_stability`.

## Rules

- Do not spawn other agents.
- Do not run backtests — only screen and classify pairs.
- Pair selection must use train data only (no lookahead).
- Stable pairs require cointegration in BOTH halves of the data.
- If fewer than 2 stable pairs exist, note the gap — the signal-researcher will work with what's available.
