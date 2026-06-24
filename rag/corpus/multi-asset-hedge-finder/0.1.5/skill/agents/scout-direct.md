# Scout Direct

Find direct basis matches for each portfolio asset on Hyperliquid. Checks whether a perp exists for each resolved symbol and fetches funding stats for matches.

## Inputs
- `.wf-artifacts/$RUN_ID/exposure_reader.json` (resolved portfolio symbols)
- `inputs/assets.yaml`
- `policy/default.yaml` (signals block)

## Output
- `.wf-artifacts/$RUN_ID/candidates_direct.json`
