# Signal Reference

All signal functions are implemented in `scripts/lib.py`. This document explains the formulas.

## Velocity-filtered z-score (`gen_velocity`)

The core signal. Computes a rolling z-score of the log price ratio, then only enters when the z-score is both extreme AND reverting.

### Formula

For pair (A, B) at bar i with lookback lb:

```
log_ratio[i] = log(price_A[i] / price_B[i])
mean = rolling_mean(log_ratio, window=lb)
std  = rolling_std(log_ratio, window=lb)
z[i] = (log_ratio[i] - mean) / std
dz   = z[i] - z[i - vb]    # velocity over vb bars
```

### Entry rules

- **Long A / Short B**: z < -entry_z AND dz > 0 (z is negative extreme AND moving up)
- **Short A / Long B**: z > +entry_z AND dz < 0 (z is positive extreme AND moving down)
- **Exit**: z crosses zero

The velocity filter (`dz > 0` / `dz < 0`) confirms mean-reversion has started before entering. Without it, entries catch trend continuations and the signal overfits massively on train data.

### Parameter ranges (from validated research)

| Parameter | Range | Best values found |
|---|---|---|
| Lookback (lb) | 72-240h | 96h (stable), 200h (drift) |
| Entry z (ez) | 0.5-2.5 | 2.0 (stable), 0.8 (drift) |
| Velocity bars (vb) | 3-12 | 6 (universal) |
| Leverage | 1.0-2.5 | 1.5 (combined) |

### Why two baskets need different parameters

- **Stable pairs** (short half-life, persistent cointegration): higher entry_z (2.0) waits for extreme dislocations that reliably revert. Shorter lookback (96h) matches the fast mean-reversion.
- **Drift pairs** (cointegrated on train, may diverge): lower entry_z (0.8) catches moves early. Longer lookback (200h) provides a smoother z-score that adapts to drift.

## Pair scoring formula (`score_pair`)

```
half_life = OU_half_life(log_spread)     # Ornstein-Uhlenbeck
coint_p   = Engle_Granger_test(log_A, log_B)

hl_score    = max(0, 1 - |half_life - 72| / 500) * 3    # peak at ~3 days
coint_score = (1.0 if p ≤ 0.05 else 0.5 if p ≤ 0.15 else 0.0) * 2

total_score = hl_score + coint_score     # range [0, 5]
```

Reject pairs with half_life < 12h (noise) or infinite (no mean reversion).

## Stability classification (`check_pair_stability`)

A pair is "stable" if it passes cointegration (HL < 300, p < 0.15) in BOTH the first 60% and last 40% of the data. Stable pairs are candidates for mean-reversion. Non-stable pairs with good train scores are drift candidates.
