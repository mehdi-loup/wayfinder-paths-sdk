# funding-rate-harvester — TODO

Research pass 2026-07-16 (Delta Lab perp screen + Kinetiq docs + landscape scan).
Registry state at time of writing: 0.2.2 live + bonded + active.

## RECOMMENDED next scope (research 2026-08-12)

From the Delta Lab perp screen (256 markets, sorted by 30d mean funding) plus an
HL spot-availability check on every candidate.

**0. Fix KAITO first — it is a live bleed, not an extension.** KAITO funding is
**−274% 30d APR** (−1185% 7d) with only 66% positive days. Short-perp carry on it
loses money continuously. It sits in the `universe.yaml` whitelist, and whitelisted
symbols get seed priority, so confirm the `min_funding_apr_bps` filter actually
excludes it — if the whitelist bypasses filtering, drop KAITO outright. This is a
config/guard change and should land before any capability work.

**Context: the current universe barely clears its own `min_net_carry_apr_bps:
1000` gate.** 30d funding APR / positive-day %: EIGEN 9.9/96, ETHFI 9.3/95,
ENA 9.3/94, HYPE 8.8/91, ETH 8.2/93, BTC 7.5/92, SOL 4.7/77. The gross carry only
clears 10% once a yield leg is stacked on top, which is why the asset axis matters.

**1. Add PURR — the one free asset win.** Far better persistent carry exists on
vanilla Hyperliquid (30d APR / pos%): VINE 34.3/100, XMR 22.6/86, STBL 21.5/100,
ZRO 19.3/96, **PURR 18.3/100**, VVV 15.9/98, GRASS 14.5/100, GRIFFAIN 13.8/100,
FARTCOIN 13.3/97, AZTEC 11.7/100, GOAT 11.2/100, BRETT 11/100. But the long leg
gates all of it: an HL spot check found **only PURR has a spot market**
(`PURR/USDC`), so PURR is the sole addition that works with the existing
`hl_spot` leg as-is. (CASHCAT at 129% and SAGA at 98% are almost certainly
illiquidity traps — see the sizing caveat below.)

**2. Highest-leverage capability: a generic BRAP on-chain spot leg.** That is
precisely what unlocks the rest of the list above, and it just got cheaper —
upstream merged Solana BRAP + transfer execution, and FARTCOIN/GRASS/GOAT/BRETT
are Solana/Base assets. This supersedes the kHYPE leg as the top capability pick
(kHYPE remains valid and is a strict subset of the same "swap in, swap out" leg
shape — do it as the first instance of the generic leg rather than a bespoke one).

**kHYPE verified 2026-08-12:** `Kinetiq Staked HYPE` / `KHYPE` at
`hyperevm_0xfd739d4e423301ce9385c1fb8850539d657c296d` (~$56.41). Kinetiq now also
ships `VKHYPE` (Earn Vault) and `KMHYPE` (Markets HYPE) — three wrappers to pick
from. Entry/exit by open-market swap only, never native unstake (8–9 day delay).

**3. Boros rate lock is already wired and merely `enabled: false`** in
`inputs/config.yaml` — turning it on is a config decision, not development work.
See the "no work needed" section below.

**Sizing caveat that applies to everything above:** Delta Lab perp rows carry no
OI or volume (`oi_now` / `volume_24h` are null), so none of these rates are
size-screened. Pull OI from the HL API before admitting any symbol — the
`min_oi_usd: 10_000_000` filter in `universe.yaml` cannot be enforced from the
Delta Lab screen alone.

**Free input for the carry-quality work below:** the same screen responses already
carry `funding_pos_pct_30d`, `funding_std_7d/30d`, and `funding_z_30d/90d`, so the
persistence/volatility scoring needs no new data dependency.

**HIP-3 keeps expanding:** the screen now shows `hyperliquid-xyz`, `-para`,
`-hyna`, and `-mkts` dexes. Dynamic discovery still pins `venue="hyperliquid"`, so
none are visible — and the blocker is unchanged (no on-chain spot hedge for
equities), see the HIP-3 section below.

## kHYPE spot leg for the HYPE pair — cheap, fits the model
<!-- Reframed 2026-08-12: no longer the top pick on its own — build it as the first
     instance of the generic BRAP spot leg (see recommended scope above). -->


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
