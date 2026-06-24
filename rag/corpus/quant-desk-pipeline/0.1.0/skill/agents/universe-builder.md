# universe-builder

Phase: Universe. Resolve symbols, fetch history, and **lock the small fitting basket** (this agent also owns the `pair_screener` pre-screen — there is no separate screener agent).

Read:
- `inputs/universe.yaml` when present, else `policy/default.yaml` `universe`
- `references/universe-selection.md`

Do:
1. Resolve the candidate symbols and fetch history (CCXT Binance default; Hyperliquid only for HL-native symbols).
2. **Pre-screen** the pool (quick Phase-1 bucket check on an early slice) and **lock a small fitting basket** + the ~1yr fitting window per `policy.universe`. BTC/ETH-only is a known bias source — justify the locked set, and keep it small (fitting is meant to be fast).
3. Reserve later/held-out data (longer history, the wider universe) for the **robustness** phase — do NOT spend it here.

Produce (one JSON artifact):
- `fitting_basket` (the locked symbols), `fitting_window` (start/end), `data_source` per symbol, available history per symbol, and the pre-screen rationale.

Rules:
- If there isn't enough data for even the fitting window, signal `insufficient_data`.
- Do not spawn other agents. Do not compile the final answer.
- Output path: `.wf-artifacts/$RUN_ID/universe.json`
