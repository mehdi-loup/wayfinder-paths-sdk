# Spread Radar Reference

Reference path for the `spread-radar` strategy pipeline archetype.

Given a field, theme, or asset universe, this path:

1. Builds a comparison universe from the theme and optional overrides
2. Clusters assets by correlation and carry regime
3. Enumerates spread candidates from the cluster structure
4. Layers in field research (catalysts, narrative context)
5. Scores candidates on edge sharpness, beta residual, stability, liquidity, and catalyst quality
6. Runs a skeptic pass to reject hidden directional beta and weak edges
7. Outputs the chosen spread (or null), invalidation rules, and a monitoring job

## Key evals

- **Clustering stability**: Clusters must be stable over the lookback window (adjusted Rand index above threshold). Unstable clusters force null state.
- **Hidden beta rejection**: Spreads whose return profile is >70% explained by market beta are rejected — they are disguised directional bets.
- **Weak evidence fallback**: If no candidate clears the scoring threshold, the path selects null state. It never forces a trade.
- **Graceful degradation**: Field research retries once on failure, then continues with partial data. Scorer failure skips to skeptic (forces null).

## Usage

```bash
# Validate structure
poetry run wayfinder path doctor --path examples/paths/spread-radar-reference

# Run fixture evals
poetry run wayfinder path eval --path examples/paths/spread-radar-reference

# Render host skill exports
poetry run wayfinder path render-skill --path examples/paths/spread-radar-reference
```
