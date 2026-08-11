# funding-rate-harvester — TODO

Research pass 2026-07-16 (Delta Lab perp screen + Kinetiq docs + landscape scan).
Registry state at time of writing: 0.2.2 live + bonded + active.

## kHYPE spot leg for the HYPE pair — top candidate (cheap, fits the model)

HYPE is whitelisted and its funding is the most *persistent* carry in the
universe right now: 7d-mean ≈ 9.6% APR, positive 95% of the last 30 days
(Delta Lab, 2026-07-16). But the HYPE pair's long leg is `hl_spot` —
zero-yield. Kinetiq kHYPE (~1.9–2% staking APY, ~$0.9B TVL, 8 audits) is the
missing wrapper leg: buy kHYPE on HyperEVM via BRAP, short HYPE perp, collect
funding + staking.

Fits the existing yield-leg pattern exactly: enter/exit via open-market swap
(like weETH/sUSDe), never via native unstake — Kinetiq's native path is an
8–9 day delay + 0.10% fee, which would strand a rotation mid-flight (same
reason `legs.py` avoids ether.fi withdraw NFTs and the sUSDe cooldown).
Needed: a new `kinetiq` leg in `legs.py` (mirroring the `etherfi` leg shape),
kHYPE BRAP token id + route-liquidity check on HyperEVM, a yield feed
(Kinetiq API or DeFiLlama pool `9f25a954-db87-4bb2-a8b2-4be0b843a44c`), and a
`k` entry in `_WRAPPER_PREFIXES` (currently absent, so Pendle PT-kHYPE markets
wouldn't map to HYPE either), tests, README update.

## Carry-quality scoring (persistence + volatility) — modeling upgrade

The scorer ranks on a funding EMA (level) but ignores *stability*. Delta Lab's
`screen_perp` rows — already fetched during dynamic discovery — carry
`funding_pos_pct_30d`, `funding_std_7d/30d`, and `funding_z_30d/90d` for free.
Fold them in: require `funding_pos_pct_30d ≥ threshold` to open, and/or scale
ranked carry by a std penalty (Sharpe-like carry quality). Near-zero extra
request cost; catches the "high EMA from one violent week" failure mode the
EMA alone can't. Whitelist-only symbols not in the screen response fall back
to EMA-only (don't block on missing quality stats).

## HIP-3 (hyperliquid-xyz) stock/commodity perps — research spike, not a quick win

The xyz dex dominates the funding leaderboard (2026-07-16 7d-means): SKHX
(SK Hynix) 120% APR, KIOXIA 67%, HYUNDAI 83%, SMSN 38%, GME/HOOD/ORCL 16–19%,
PLATINUM 17% — with 72–83% positive-funding days. The SDK already trades HIP-3
(`xyz:` asset names), but the harvester excludes them twice: dynamic discovery
pins `venue="hyperliquid"`, and there is **no on-chain spot hedge for
equities**. The only candidate hedge is Robinhood Chain tokenized stocks
(live 2026-07-01, chain 4663, gas-sponsored, BRAP-listed chain) — a novel
"stock basis trade" (short xyz perp, long the stock token). Open problems:
ticker coverage (Korean/Japanese names like SKHX/KIOXIA are unlikely on
Robinhood; US names GME/HOOD/ORCL maybe), stock-token liquidity/spread,
wrapper-claim (not shares) risk, and market-hours vs 24/7 basis behavior.
Delta Lab xyz rows also lack OI/volume — universe filters would need HL API
data instead. Treat as a separate research spike or its own path; the funding
is rich enough to justify the look.

## CCXT venues as alternative short legs — v1.1 plan, one correction

The README v1.1 plan (Binance/Bybit/OKX short legs, cross-venue migration
sagas, CEX paper gate) stands, with one data correction from this research:
**Delta Lab's perp coverage is hyperliquid + hyperliquid-xyz only** — funding
screening for CEX venues must come from the CCXT venues' own funding
endpoints, not `screen_perp`. Cross-venue capital movement (CEX
deposit/withdraw legs) remains the heavy part; the idempotency-key scheme is
already saga-ready.

## Boros rate lock — no work needed

`rate_lock.py` discovers lock markets dynamically by underlying symbol
(`underlying_symbol=..., platform=hyperliquid`), so new Boros listings are
picked up automatically. Nothing to extend.
