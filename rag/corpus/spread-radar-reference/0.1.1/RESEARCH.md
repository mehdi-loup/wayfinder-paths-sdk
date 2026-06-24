# Spread Radar — Research Guide

## Latest validated result (2026-04-13)

**Combined velocity spread** — two baskets with velocity-filtered z-score entry, 50/50 weight split.

| Component | Drift basket | Stable basket |
|---|---|---|
| Pairs | NEAR/AVAX, WLD/TAO, TRB/ETH | TRB/AVAX, SOL/SUI, JUP/ETH |
| Lookback | 200h | 96h |
| Entry z | 0.8 | 2.0 |
| Mechanism | Catches pair divergence/drift | Mean-reversion on cointegrated pairs |

Common: exit_z=0.0, velocity_bars=6, leverage=1.5x, 3.5bps+1bp fees, 2% rebalance threshold.

**OOS results** (walk-forward 60/40, 76d train / 50d test): Sharpe **7.67**, return **+54.25%**, max DD **-10.49%**.

**Robustness**: 243 configs tested — 64% achieve OOS Sharpe > 3, median 4.60. The stable basket alone: 144 configs, 90% profitable OOS, median Sharpe 2.54.

### Why it works

1. **Drift basket** captures regime-break divergences. Low entry_z (0.8) catches moves early.
2. **Stable basket** trades mean-reversion on short half-life pairs (TRB/AVAX: 34h, SOL/SUI: 27h). Higher entry_z (2.0) waits for extreme dislocations.
3. **Velocity filter** (only enter when z reverting toward zero) prevents trend-continuation traps. Without it, baseline z-score overfits 5x+ on train then collapses OOS.
4. **Uncorrelated alpha sources** smooth the equity curve.

## Reproduce / extend

Run the validated backtest: `scripts/reproduce.py`. It fetches data, generates signals for both baskets, and runs the backtest. The script also exports `gen_velocity()`, `score_pair()`, and `select_pairs()` for use in your own research.

To find your own pairs, call `select_pairs(prices, n_pairs=3)` on any price DataFrame. For stable pairs, verify cointegration holds across multiple time windows (not just the full sample).

## How to build a live strategy

1. **Scaffold**: `just create-strategy "Spread Radar"` — creates `strategies/spread_radar/` with its own dedicated signing account.
2. **Implement**: Extend `Strategy` with these methods — `deposit` (move USDC onto Hyperliquid), `update` (read prices, compute the signal, rebalance perps), `status`, and `withdraw` (close positions).
3. **Adapters**: HyperliquidAdapter (`/using-hyperliquid-adapter`) for perp trading, BalanceAdapter for balance tracking, BRAPAdapter for cross-chain transfers.
4. **Signal**: Import `gen_velocity` from `scripts/reproduce.py`, or use the simplified single-bar version:

```python
def compute_targets(prices, pairs, lb, entry_z, vb=6):
    """Single-bar signal: returns target weights for current bar."""
    w = 1.0 / len(pairs)
    pos = pd.Series(0.0, index=prices.columns)
    for a, b in pairs:
        lr = np.log(prices[a].values / prices[b].values)
        rm, rs = pd.Series(lr).rolling(lb).mean(), pd.Series(lr).rolling(lb).std()
        z = float((lr[-1] - rm.iloc[-1]) / rs.iloc[-1]) if rs.iloc[-1] > 0 else 0.0
        z_prev = float((lr[-1-vb] - rm.iloc[-1]) / rs.iloc[-1]) if rs.iloc[-1] > 0 else 0.0
        dz = z - z_prev
        if z < -entry_z and dz > 0: pos[a] += w; pos[b] -= w
        elif z > entry_z and dz < 0: pos[a] -= w; pos[b] += w
    return pos
```

5. **Automate**: `wayfinder runner add-job --strategy spread_radar --action update --interval 21600`

Key notes: min Hyperliquid order $10 notional, min deposit $5. Re-screen pairs monthly. Store position state between updates in `.wf-state/`.

## Pipeline overview

8-phase pipeline: `intake → universe_builder → series_clusterer → spread_enumerator → field_research → scorer → skeptic → finalize`. The pipeline discovers spreads; the research above validates and trades them.

## Backtesting framework

Use `wayfinder_paths.core.backtesting`. Target positions are a DataFrame of weights in [-1, 1]. The framework handles normalization, leverage, fees, funding, liquidation. All data is hourly (`periods_per_year=8760`), ~7 month retention. Load `/using-delta-lab` before fetching data.

Walk-forward validation: split 60/40, optimize on train, freeze params for test. If OOS Sharpe decays > 50%, the signal is overfit.

## Pair selection

Score all pairwise combinations by half-life (OU, target 24-150h) and cointegration (Engle-Granger, p < 0.15). Scoring: `hl_score * 3 + coint_score * 2`. See `score_pair()` and `select_pairs()` in `scripts/reproduce.py`.

## Signal ideas to explore

- **Adaptive threshold**: scale entry_z with recent spread volatility
- **Dual-timeframe**: long window (168h) for direction, short (48h) for confirmation
- **Regime filter**: only trade in low-vol regimes (spread vol below median)
- **Kalman z-score**: EMA-based mean/var instead of rolling window
- **Dynamic pair rotation**: re-screen pairs every 2 weeks
- **Cross-sector pairs**: constrain to cross-sector for structural divergence
- **Multi-pair momentum**: only enter when multiple spreads are extreme simultaneously
- **Funding-weighted entry**: lower threshold when carry favors the direction (note: funding has no predictive power for direction, only affects P&L)

## Universe candidates

| Category | Symbols |
|---|---|
| Perp DEX | HYPE, DYDX, GMX, AERO, JUP |
| AI | RENDER, FET, WLD, TAO, NEAR |
| Oracle / Info | PENDLE, SNX, PYTH, TRB, UMA |
| L1 | ETH, SOL, AVAX, SUI, APT |

## Key gotchas

- **Return decomposition**: check how much comes from price P&L vs funding. If mostly funding, it's a carry trade.
- **Fee sensitivity**: test at 0/4.5/15/30 bps. If Sharpe halves at 15bps, the strategy won't scale.
- **Pair selection bias**: screening N pairs and reporting the best is multiple-testing. Walk-forward is the only honest evaluation.
- **Load protocol skills first**: `/using-delta-lab`, `/using-hyperliquid-adapter`, `/using-pool-token-balance-data`.
