# Feasibility prune

Mark each candidate KEEP / PRUNE on five axes from **title + abstract + URL only** (no methodology yet). You are judging whether a paper is worth implementing, not whether the signal works — that is the harness's job downstream.

## Axis 1 — Data availability

The harness CAN access:
- **Crypto OHLCV** — CCXT Binance hourly (~2017+ for majors), Hyperliquid candles (~200d), CoinGecko snapshots.
- **Funding** — Hyperliquid perp funding history, Delta Lab basis APYs.
- **On-chain** — lending APYs (Aave, Moonwell, Morpho), DEX TVL/volume (Uniswap V3, Aerodrome), token transfers.
- **Orderbook** — Hyperliquid L2 (live only, not historical).

The harness CANNOT access:
- Equity-market data (CRSP, Compustat, TAQ); options/implied-vol surfaces; non-free macro/news/sentiment datasets; historical tick-level orderbook; survey or central-bank micro-data; regulatory filings.

Tag: `data: ok` (crypto-native, or asset-class-agnostic price/volume/funding/microstructure inputs) · `proxy_possible` (equity-focused but mechanically portable — cross-sectional momentum, vol-of-vol, autocorrelation) · `missing` (depends on equity-specific inputs — earnings, dividends, accruals, ownership, options surface, CRSP/Compustat — with no clean crypto analogue → **PRUNE**). Keep the pool heavily crypto/general; equity-only papers tolerated only as a small minority.

**Universe-mismatch flag (KEEP + warn, do NOT prune):** if the headline result depends on a universe ≥ ~5× our ~11-major pool, set `universe_mismatch: true` and note which layers are universe-dependent (cross-sectional ranking, top-K selection, Sharpe filtering). Implementation then either strips the cross-sectional wrapper and runs the per-asset entry signal, or builds a per-symbol fallback — decided *before* implementation, not after a misleading PASS/FAIL.

## Axis 2 — Compute tractability

PRUNE: heavy ML training (deep nets, GBM on large feature sets, RL); MCMC / Bayesian sampling over many params; cross-sectional regressions over thousands of assets; in-loop LP/QP/MILP; datasets that take >1h to assemble. KEEP: closed-form transforms, rolling statistics, regime switches, simple regressions that fit a vectorized pandas pipeline (~2 min/eval on CPU).

## Axis 3 — Claimed value

PRUNE if the in-sample edge is too small to survive crypto noise: Sharpe < 0.5, OR t-stat < 2.0, OR annualized return < 3% with no risk discount. Tiny equity factor-zoo effects rarely transfer. No numeric effect reported → `value: unknown`, lean KEEP only if data + compute are clearly strong.

## Axis 4 — Monetizable frequency

Native horizon ≥ 15m or PRUNE (sub-15m is unmonetizable on this SDK). Weekly/monthly → `low_freq_warn` (KEEP, flagged). `native_horizon` ∈ {15m, 1h, 4h, 1d} is required on every KEEP row — it drives the harness `bar_interval`.

## Axis 5 — Novelty

Does it add something over an existing memory entry on the same topic, or over another candidate in the pool? If memory already rejected this exact signal, PRUNE and note it.

**Reloop:** if fewer than 4 KEEP papers survive, signal `thin_pool`.
