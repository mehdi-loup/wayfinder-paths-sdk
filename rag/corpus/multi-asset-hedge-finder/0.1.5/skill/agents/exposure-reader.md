# Exposure Reader

Resolve portfolio symbols, fetch hourly time series, and build the portfolio return series. Handles mixed-history portfolios by separating assets with sufficient data from those without.

## Inputs
- `inputs/assets.yaml` (portfolio holdings)
- `policy/default.yaml` (signals block for lookback and resolution)

## Output
- `.wf-artifacts/$RUN_ID/exposure_reader.json`
