# universe-builder

Resolve the asset universe from the user's theme and fetch price/funding data.

Read:
- `inputs/theme.md`
- `inputs/universe.yaml` when present
- `policy/default.yaml` (universe section)

## Task

Write and run a script that:

1. Resolves symbols for the theme. If `inputs/universe.yaml` has explicit symbols, use those. Otherwise expand the theme to candidate symbols from this confirmed universe:

   | Category | Symbols |
   |---|---|
   | Perp DEX | HYPE, DYDX, GMX, AERO, JUP |
   | AI | RENDER, FET, WLD, TAO, NEAR |
   | Oracle / Info | PENDLE, SNX, PYTH, TRB, UMA |
   | L1 | ETH, SOL, AVAX, SUI, APT |

2. Fetches hourly price and funding data using `fetch_universe()` from `scripts/lib.py`.
3. Drops symbols with insufficient data (< 500 bars).
4. Writes results to `.wf-artifacts/$RUN_ID/universe.json` containing:
   - `symbols`: list of available symbols
   - `bars`: number of aligned hourly bars
   - `days`: number of days of data
   - `date_range`: [start, end] ISO timestamps
   - `dropped`: list of {symbol, reason} for symbols that were excluded

The script must also save the aligned prices and funding DataFrames to a pickle file at `.wf-artifacts/$RUN_ID/data.pkl` for downstream agents.

## Rules

- Do not spawn other agents.
- Do not score or select pairs — only resolve the universe and fetch data.
- Require at least `min_assets` symbols (from policy) to proceed.
