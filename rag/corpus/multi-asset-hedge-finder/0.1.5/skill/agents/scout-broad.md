# Scout Broad

Screen the broad Hyperliquid perp universe for correlated proxy hedges. Filters by liquidity (OI, volume) and spread, excluding symbols already found as direct matches. Caps candidates at 25.

## Inputs
- `.wf-artifacts/$RUN_ID/exposure_reader.json` (portfolio data)
- `.wf-artifacts/$RUN_ID/candidates_direct.json` (to deduplicate)
- `policy/default.yaml` (decision and risk blocks)

## Output
- `.wf-artifacts/$RUN_ID/candidates_broad.json`
