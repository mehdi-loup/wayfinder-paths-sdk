# signal-researcher

Find the best signal configuration for each pair basket using the backtesting framework.

Read:
- `.wf-artifacts/$RUN_ID/data.pkl` (prices and funding)
- `.wf-artifacts/$RUN_ID/pair_screen.json` (screened pairs from pair-screener)

## Task

Write and run a script that:

1. Loads prices, funding, and the screened pair baskets.
2. Calls `run_full_pipeline(prices, funding, stable_pairs, drift_pairs)` from `scripts/lib.py`.
3. Saves the returned dict to `.wf-artifacts/$RUN_ID/signal_research.json`.

That's it. `run_full_pipeline` handles the entire sweep internally:
- 27 configs per basket (3 lookbacks × 3 entry_z × 3 leverages)
- Combined sweep (best per basket × 3 leverages)
- P&L decomposition on the top result
- Robustness stats
- Prints progress as it runs (~70s total)

**Do NOT write your own parameter grid.** The grid is fixed in `lib.py` to prevent bloat.

## Rules

- Do not spawn other agents.
- Call `run_full_pipeline()` — do not call `run_walk_forward()` in a loop.
- Save the JSON output (strip equity_curve Series before serializing).
